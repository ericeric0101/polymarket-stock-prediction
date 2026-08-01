"""Compact terminal reporting for the long-running shadow supervisor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import select
import sys
import time
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .fees import estimate_taker_fee_usdc
from .journal import PaperPosition, ShadowJournal
from .logging import log_event
from .probability_calibration import SizingReadiness, sizing_readiness


def make_event_sink(log_path: Path, output_format: str):
    def sink(event_type: str, payload: Mapping[str, object]) -> None:
        log_event(log_path, event_type, payload)
        if output_format == "json":
            print(json.dumps({"event_type": event_type, **payload}, sort_keys=True, default=str))
            return
        print(_human_event(event_type, payload))
    return sink


def render_dashboard(
    rows: tuple[Mapping[str, object], ...], open_positions: int, settled_positions: int, *,
    positions: Iterable[PaperPosition] = (), signal_performance: Mapping[str, object] | None = None,
    sizing: SizingReadiness | None = None, daily_entry_limit: int = 3,
) -> str:
    now = datetime.now(UTC).astimezone(NEW_YORK)
    lines = [
        f"Shadow dashboard | NY {now:%Y-%m-%d %H:%M:%S} | active markets: {len(rows)} | "
        f"paper positions: {open_positions} open / {settled_positions} settled"
    ]
    lines.append("SYMBOL  12:00 EDT                 14:00 EDT                 15:30 EDT                 LATEST ACTION")
    for row in rows:
        checkpoints = _row_checkpoints(row)
        cells = [_plain_checkpoint_cell(checkpoints.get(name), now=now) for name in CHECKPOINT_NAMES]
        lines.append(
            f"{str(row.get('symbol', '-'))[:6]:<7} {cells[0]:<25} {cells[1]:<25} {cells[2]:<25} "
            f"{_plain_latest_recommendation(checkpoints, now=now)}"
        )
    lines.extend(_plain_daily_portfolio_summary(
        positions, signal_performance or {}, sizing=sizing, daily_entry_limit=daily_entry_limit,
    ))
    return "\n".join(lines)


def run_live_dashboard(
    journal: ShadowJournal, *, refresh_seconds: float, limit: int, daily_entry_limit: int = 3
) -> None:
    if refresh_seconds <= 0 or limit < 1 or daily_entry_limit < 1:
        raise ValueError("dashboard refresh_seconds, limit, and daily_entry_limit must be positive")
    console = Console()
    with _DashboardInput() as keyboard:
        try:
            with Live(console=console, screen=True, auto_refresh=False) as live:
                while True:
                    positions = journal.list_paper_positions()
                    rows = journal.dashboard_rows(limit)
                    signal_performance = journal.first_signal_performance()
                    sizing = sizing_readiness(journal.list_first_signal_calibration_observations())
                    live.update(
                        _rich_dashboard(
                            rows, positions, signal_performance=signal_performance, sizing=sizing,
                            refresh_seconds=refresh_seconds, daily_entry_limit=daily_entry_limit,
                        ),
                        refresh=True,
                    )
                    deadline = time.monotonic() + refresh_seconds
                    while time.monotonic() < deadline:
                        if keyboard.poll() == "quit":
                            return
                        time.sleep(0.1)
        except KeyboardInterrupt:
            return


def _rich_dashboard(
    rows: tuple[Mapping[str, object], ...], positions: tuple[PaperPosition, ...], *,
    signal_performance: Mapping[str, object] | None = None, sizing: SizingReadiness | None = None,
    refresh_seconds: float = 3.0, daily_entry_limit: int = 3,
) -> Layout:
    open_positions = sum(getattr(position, "status") == "OPEN" for position in positions)
    settled_positions = sum(getattr(position, "status") == "SETTLED" for position in positions)
    regular = sum(row.get("market_session") == "REGULAR" for row in rows)
    signals = sum(
        any(payload.get("paper_outcome") for payload in _row_checkpoints(row).values()) for row in rows
    )
    maker_quotes = sum(len(row.get("maker_shadow_quotes") or []) for row in rows)
    local_now = datetime.now().astimezone()
    ny_now = datetime.now(UTC).astimezone(NEW_YORK)

    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right", ratio=1)
    left = Text()
    left.append("Mode: ", style="dim")
    left.append("SHADOW", style="bold cyan")
    left.append(f"   Markets: {len(rows)}   Regular: {regular}   Checkpoint entries: {signals}   Maker: {maker_quotes}\n")
    left.append(f"Paper positions: {open_positions} open / {settled_positions} settled", style="green" if open_positions else "dim")
    right = Text()
    right.append("Data source: SQLite shadow journal\n", style="cyan")
    right.append(f"Local: {local_now:%Y-%m-%d %H:%M:%S %Z}\n", style="dim")
    right.append(f"New York: {ny_now:%Y-%m-%d %H:%M:%S %Z}", style="bold cyan")
    header.add_row(left, right)

    table = Table(expand=True, header_style="bold cyan", pad_edge=False)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("12:00 EDT", ratio=2)
    table.add_column("14:00 EDT", ratio=2)
    table.add_column("15:30 EDT", ratio=2)
    table.add_column("Latest recommendation", ratio=3)
    for row in rows:
        checkpoints = _row_checkpoints(row)
        table.add_row(
            str(row.get("symbol", "-")),
            _checkpoint_text("1200_EDT", checkpoints.get("1200_EDT"), now=ny_now),
            _checkpoint_text("1400_EDT", checkpoints.get("1400_EDT"), now=ny_now),
            _checkpoint_text("1530_EDT", checkpoints.get("1530_EDT"), now=ny_now),
            _latest_recommendation_text(checkpoints),
        )
    if not rows:
        table.add_row("-", Text("PENDING", style="dim"), Text("PENDING", style="dim"),
                      Text("PENDING", style="dim"), Text("Waiting for regular-session evaluations", style="yellow"))

    footer = Text()
    footer.append("Direction cells: side / fair / recorded ask / fee+buffer-adjusted edge.  ", style="dim")
    footer.append("Green=entry-eligible  Yellow=skip/blocked  Dim=non-positive edge\n", style="dim")
    footer.append(f"Refresh: journal every {refresh_seconds:g}s  |  q: close  |  Ctrl+C: close  |  Shadow only", style="magenta")
    today_positions = _today_selected_positions(positions)
    portfolio = _daily_portfolio_panel(
        today_positions, signal_performance or {}, sizing=sizing, daily_entry_limit=daily_entry_limit,
    )
    layout = Layout()
    layout.split_column(
        Layout(Panel(header, title="Polymarket Stock Shadow", border_style="blue"), size=5),
        Layout(
            Panel(table, title=f"Checkpoint Decision Matrix - {ny_now:%Y-%m-%d} New York", border_style="green"),
            ratio=1,
        ),
        Layout(
            Panel(portfolio, title="Top Recommendations - Daily Paper Portfolio", border_style="cyan"),
            size=_daily_portfolio_height(len(today_positions), sizing is not None),
        ),
        Layout(Panel(footer, border_style="magenta"), size=3),
    )
    return layout


NEW_YORK = ZoneInfo("America/New_York")

CHECKPOINT_NAMES = ("1200_EDT", "1400_EDT", "1530_EDT")
CHECKPOINT_CLOCKS = {"1200_EDT": (12, 0), "1400_EDT": (14, 0), "1530_EDT": (15, 30)}
DASHBOARD_MINIMUM_EDGE = 0.02


def _row_checkpoints(row: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    value = row.get("checkpoints")
    if not isinstance(value, Mapping):
        return {}
    return {str(name): payload for name, payload in value.items() if isinstance(payload, Mapping)}


def _checkpoint_has_passed(name: str, now: datetime) -> bool:
    hour, minute = CHECKPOINT_CLOCKS[name]
    return (now.hour, now.minute) >= (hour, minute)


def _checkpoint_direction(payload: Mapping[str, object]) -> tuple[str, float, float | None, float | None]:
    fair_up = float(payload["fair_up_probability"])
    model_outcome = str(payload.get("model_outcome") or "")
    if model_outcome in {"UP", "DOWN"}:
        outcome = model_outcome
    else:
        up_edge = payload.get("up_edge")
        down_edge = payload.get("down_edge")
        if up_edge is not None and down_edge is not None and max(float(up_edge), float(down_edge)) > 0:
            outcome = "UP" if float(up_edge) >= float(down_edge) else "DOWN"
        else:
            outcome = "UP" if fair_up >= 0.5 else "DOWN"
    probability = fair_up if outcome == "UP" else 1 - fair_up
    ask_value = payload.get("up_ask" if outcome == "UP" else "down_ask")
    edge_value = payload.get("up_edge" if outcome == "UP" else "down_edge")
    return (
        outcome, probability,
        float(ask_value) if ask_value is not None else None,
        float(edge_value) if edge_value is not None else None,
    )


def _recommended_limit(payload: Mapping[str, object], outcome: str) -> float | None:
    fair_up = float(payload["fair_up_probability"])
    raw_probability = fair_up if outcome == "UP" else 1 - fair_up
    conservative_probability = max(0.0, raw_probability - float(payload.get("model_error_buffer") or 0.02))
    fee_rate_value = payload.get("up_fee_rate" if outcome == "UP" else "down_fee_rate")
    fee_rate = float(fee_rate_value) if fee_rate_value is not None else 0.0
    minimum_edge = float(payload.get("minimum_edge") or DASHBOARD_MINIMUM_EDGE)
    eligible = []
    for mills in range(1, 1000):
        price = mills / 1000
        fee = estimate_taker_fee_usdc(shares=1, price=price, fee_rate=fee_rate)
        if conservative_probability - price - fee + 1e-12 >= minimum_edge:
            eligible.append(price)
    return max(eligible) if eligible else None


def _format_contract_price(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}" if value < 0.01 else f"{value:.2f}"


def _checkpoint_text(name: str, payload: Mapping[str, object] | None, *, now: datetime) -> Text:
    if payload is None:
        passed = _checkpoint_has_passed(name, now)
        return Text("MISSED" if passed else "PENDING", style="red" if passed else "dim")
    outcome, probability, ask, edge = _checkpoint_direction(payload)
    edge_text = "-" if edge is None else f"{edge:+.1%}"
    text = f"{outcome} {probability:.0%}  a{_format_contract_price(ask)}  e{edge_text}"
    if payload.get("paper_outcome") == outcome:
        return Text(text, style="bold green")
    if payload.get("paper_entry_block_reasons"):
        return Text(text, style="yellow")
    return Text(text, style="cyan" if edge is not None and edge > 0 else "dim")


def _plain_checkpoint_cell(payload: Mapping[str, object] | None, *, now: datetime) -> str:
    if payload is None:
        return "PENDING"
    outcome, probability, ask, edge = _checkpoint_direction(payload)
    edge_text = "-" if edge is None else f"{edge:+.1%}"
    return f"{outcome} {probability:.0%} a{_format_contract_price(ask)} e{edge_text}"


def _latest_checkpoint(checkpoints: Mapping[str, Mapping[str, object]]) -> tuple[str, Mapping[str, object]] | None:
    for name in reversed(CHECKPOINT_NAMES):
        payload = checkpoints.get(name)
        if payload is not None:
            return name, payload
    return None


def _latest_recommendation(checkpoints: Mapping[str, Mapping[str, object]]) -> tuple[str, str]:
    latest = _latest_checkpoint(checkpoints)
    if latest is None:
        return "WAIT", "Waiting for 12:00 EDT"
    name, payload = latest
    outcome, _probability, ask, edge = _checkpoint_direction(payload)
    limit = _recommended_limit(payload, outcome)
    blocks = payload.get("paper_entry_block_reasons") or []
    detail = f"{outcome} now {_format_contract_price(ask)} / <= {_format_contract_price(limit)}"
    if payload.get("paper_outcome") == outcome:
        suffix = f"  edge {edge:+.1%}" if edge is not None else ""
        return "ENTER", f"{name[:4]} {detail}{suffix}"
    if blocks:
        return "SKIP", f"{name[:4]} {outcome} blocked: {blocks[0]}"
    return "SKIP", f"{name[:4]} wait {detail}"


def _latest_recommendation_text(checkpoints: Mapping[str, Mapping[str, object]]) -> Text:
    action, detail = _latest_recommendation(checkpoints)
    style = "bold green" if action == "ENTER" else "yellow" if action == "SKIP" else "dim"
    return Text(f"{action}  {detail}", style=style)


def _plain_latest_recommendation(checkpoints: Mapping[str, Mapping[str, object]], *, now: datetime) -> str:
    action, detail = _latest_recommendation(checkpoints)
    return f"{action} {detail}"


def _plain_daily_portfolio_summary(
    positions: Iterable[PaperPosition], signal_performance: Mapping[str, object], *,
    sizing: SizingReadiness | None, daily_entry_limit: int,
) -> list[str]:
    now = datetime.now(UTC).astimezone(NEW_YORK)
    today = _today_selected_positions(positions, now=now)
    settled = tuple(position for position in today if position.status == "SETTLED")
    wins = sum(position.outcome == position.settlement_outcome for position in settled)
    total_settled = int(signal_performance.get("settled_markets") or 0)
    total_wins = int(signal_performance.get("wins") or 0)
    today_rate = f"{wins / len(settled):.1%}" if settled else "pending"
    all_rate = f"{total_wins / total_settled:.1%}" if total_settled else "pending"
    lines = [
        "Daily Paper Portfolio | "
        f"{now.date().isoformat()} selected: {len(today)} / {daily_entry_limit} | "
        f"settled W/L: {wins}/{len(settled) - wins} | win rate: {today_rate}",
        f"All first signals | {total_wins}/{total_settled} | win rate: {all_rate}",
    ]
    for position in sorted(today, key=lambda item: item.opened_at):
        result = "OPEN" if position.status != "SETTLED" else "WIN" if position.outcome == position.settlement_outcome else "LOSS"
        pnl = "-" if position.realized_pnl is None else f"{position.realized_pnl:+.4f}"
        lines.append(
            f"  {position.opened_at.astimezone(NEW_YORK):%H:%M} {position.symbol:<6} "
            f"{position.outcome:<4} ask {position.entry_ask:.2f} "
            f"fair {position.fair_probability:.1%} {result:<4} pnl {pnl}"
        )
    if not today:
        lines.append("  No selected paper entries")
    if sizing is not None:
        lines.append(_sizing_summary(sizing))
    return lines


def _today_selected_positions(
    positions: Iterable[PaperPosition], *, now: datetime | None = None,
) -> tuple[PaperPosition, ...]:
    local_now = now or datetime.now(UTC).astimezone(NEW_YORK)
    return tuple(
        position for position in positions
        if position.included_in_calibration and position.opened_at.astimezone(NEW_YORK).date() == local_now.date()
    )


def _daily_portfolio_height(selected_count: int, has_sizing_summary: bool) -> int:
    """Reserve a visible table row for every selected entry."""
    return max(8, 6 + max(1, selected_count) + int(has_sizing_summary))


def _daily_portfolio_panel(
    today: Iterable[PaperPosition], signal_performance: Mapping[str, object], *,
    sizing: SizingReadiness | None, daily_entry_limit: int,
) -> Table:
    now = datetime.now(UTC).astimezone(NEW_YORK)
    today = tuple(today)
    settled_today = tuple(position for position in today if position.status == "SETTLED")
    today_wins = sum(position.outcome == position.settlement_outcome for position in settled_today)
    total_settled = int(signal_performance.get("settled_markets") or 0)
    total_wins = int(signal_performance.get("wins") or 0)

    panel = Table.grid(expand=True)
    panel.add_column(ratio=1)
    panel.add_column(justify="right", ratio=1)
    today_rate = f"{today_wins / len(settled_today):.1%}" if settled_today else "pending"
    all_rate = f"{total_wins / total_settled:.1%}" if total_settled else "pending"
    panel.add_row(
        Text(
            f"{now.date().isoformat()} selected: {len(today)} / {daily_entry_limit}  "
            f"settled: {len(settled_today)}  W/L: {today_wins}/{len(settled_today) - today_wins}  win rate: {today_rate}",
            style="bold green" if settled_today and today_wins == len(settled_today) else "cyan",
        ),
        Text(f"All first signals: {total_wins}/{total_settled}  win rate: {all_rate}", style="cyan"),
    )
    entries = Table(expand=True, header_style="bold cyan", box=None, pad_edge=False)
    entries.add_column("NY Time", style="dim", no_wrap=True)
    entries.add_column("Symbol", style="bold")
    entries.add_column("Side")
    entries.add_column("Ask", justify="right")
    entries.add_column("Fair", justify="right")
    entries.add_column("Status")
    entries.add_column("PnL", justify="right")
    for position in sorted(today, key=lambda item: item.opened_at):
        settled = position.status == "SETTLED"
        won = settled and position.outcome == position.settlement_outcome
        status = "OPEN" if not settled else f"{position.settlement_outcome} {'WIN' if won else 'LOSS'}"
        entries.add_row(
            position.opened_at.astimezone(NEW_YORK).strftime("%H:%M"),
            position.symbol, position.outcome, f"{position.entry_ask:.2f}", f"{position.fair_probability:.1%}",
            Text(status, style="yellow" if not settled else "bold green" if won else "bold red"),
            Text(
                "-" if position.realized_pnl is None else f"{position.realized_pnl:+.4f}",
                style="dim" if position.realized_pnl is None else "green" if position.realized_pnl >= 0 else "red",
            ),
        )
    if not today:
        entries.add_row("-", "-", "-", "-", "-", Text("No selected paper entries", style="dim"), "-")
    panel.add_row(entries)
    if sizing is not None:
        panel.add_row(Text(_sizing_summary(sizing), style="yellow"), Text("Raw fair probabilities are research-only", style="dim"))
    return panel


def _sizing_summary(sizing: SizingReadiness) -> str:
    cohort_summary = "  ".join(
        f"{cohort.iv_regime}: {cohort.sample_size}/{sizing.kelly_minimum_cohort_samples}"
        for cohort in sizing.cohorts
    )
    return f"Sizing: {sizing.position_sizing}; Kelly disabled. {cohort_summary}"


def _session_text(session: str) -> Text:
    if session == "REGULAR":
        return Text(session, style="green")
    if session.startswith("HOLIDAY"):
        return Text(session, style="bold red")
    return Text(session, style="yellow")


def _price_text(value: object, decimals: int) -> Text:
    return Text(_price(value, decimals), style="cyan" if value is not None else "dim")


def _book_text(bid: object, ask: object) -> Text:
    if bid is None or ask is None:
        return Text("-", style="dim")
    spread = float(ask) - float(bid)
    style = "cyan" if spread < 0.01 else "green" if spread <= 0.02 else "yellow" if spread <= 0.05 else "red"
    return Text(f"{float(bid):.2f}/{float(ask):.2f}", style=style)


def _probability_text(value: object) -> Text:
    return Text("-", style="dim") if value is None else Text(f"{float(value):.1%}", style="bold cyan")


def _iv_text(value: object) -> Text:
    return Text("fallback", style="yellow") if value is None else Text(f"{float(value):.1%}", style="cyan")


def _status_text(row: Mapping[str, object]) -> Text:
    if row.get("paper_outcome"):
        return Text(f"PAPER {row['paper_outcome']}", style="bold green")
    model_outcome = row.get("model_outcome")
    entry_blocks = row.get("paper_entry_block_reasons") or []
    if model_outcome and entry_blocks:
        return Text(f"OBSERVE {model_outcome}: {entry_blocks[0]}", style="yellow")
    maker_quotes = row.get("maker_shadow_quotes") or []
    if isinstance(maker_quotes, list) and maker_quotes:
        summary = "  ".join(
            f"{quote.get('outcome')} @ {float(quote.get('limit_price')):.2f}"
            for quote in maker_quotes if isinstance(quote, Mapping) and quote.get("limit_price") is not None
        )
        if summary:
            return Text(f"MAKER {summary}", style="bold cyan")
    reasons = row.get("skip_reasons") or []
    if not reasons:
        return Text("OBSERVING", style="green")
    reason = str(reasons[0])
    style = "red" if "RISK" in reason or "CROSSED" in reason else "yellow"
    return Text(reason.replace("NON_REGULAR_SESSION:", ""), style=style)


class _DashboardInput:
    def __enter__(self):
        self.enabled = False
        self.fd = None
        self.settings = None
        if not sys.stdin.isatty():
            return self
        try:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.settings = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.enabled = True
        except Exception:
            pass
        return self

    def __exit__(self, *_args: object) -> None:
        if self.enabled and self.fd is not None and self.settings is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.settings)

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            return "quit" if ready and sys.stdin.read(1).lower() == "q" else None
        except Exception:
            return None


def _human_event(event_type: str, payload: Mapping[str, object]) -> str:
    if event_type == "REALTIME_BASELINE_EVALUATED":
        symbol = payload.get("symbol", "?")
        session = payload.get("market_session", "?")
        spot = _price(payload.get("spot"), 2)
        up = f"{_price(payload.get('up_bid'), 2)}/{_price(payload.get('up_ask'), 2)}"
        down = f"{_price(payload.get('down_bid'), 2)}/{_price(payload.get('down_ask'), 2)}"
        reasons = payload.get("skip_reasons") or []
        status = payload.get("signal_status") if not reasons else "SKIP " + ", ".join(str(reason) for reason in reasons)
        return f"[{symbol}] {session} spot={spot} Up={up} Down={down} | {status}"
    if event_type == "SUPERVISOR_UNIVERSE_REFRESHED":
        return f"[scan] {payload.get('candidate_count', 0)} candidates -> {payload.get('active_market_count', 0)} active markets"
    if event_type == "PAPER_POSITION_OPENED":
        return f"[paper] OPEN {payload.get('symbol')} {payload.get('outcome')} @ {_price(payload.get('entry_ask'), 3)}"
    if event_type == "PAPER_POSITION_SETTLED":
        return f"[paper] SETTLED {payload.get('symbol')} pnl={_price(payload.get('realized_pnl'), 4)}"
    return f"[{event_type}] {json.dumps(payload, sort_keys=True, default=str)}"


def _price(value: object, decimals: int) -> str:
    return "-" if value is None else f"{float(value):.{decimals}f}"
