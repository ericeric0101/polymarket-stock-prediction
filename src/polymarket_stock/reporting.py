"""Compact terminal reporting for the long-running shadow supervisor."""

from __future__ import annotations

import json
from pathlib import Path
import select
import sys
import time
from typing import Mapping

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .journal import ShadowJournal
from .logging import log_event


def make_event_sink(log_path: Path, output_format: str):
    def sink(event_type: str, payload: Mapping[str, object]) -> None:
        log_event(log_path, event_type, payload)
        if output_format == "json":
            print(json.dumps({"event_type": event_type, **payload}, sort_keys=True, default=str))
            return
        print(_human_event(event_type, payload))
    return sink


def render_dashboard(rows: tuple[Mapping[str, object], ...], open_positions: int, settled_positions: int) -> str:
    lines = [f"Shadow dashboard | active markets: {len(rows)} | paper positions: {open_positions} open / {settled_positions} settled"]
    lines.append("SYMBOL  MARKET    SESSION       SPOT       UP bid/ask   DOWN bid/ask  STATUS")
    for row in rows:
        symbol = str(row.get("symbol", "-"))[:6]
        market = str(row.get("market_id", "-"))[-6:]
        spot = _price(row.get("spot"), 2)
        up = f"{_price(row.get('up_bid'), 2)}/{_price(row.get('up_ask'), 2)}"
        down = f"{_price(row.get('down_bid'), 2)}/{_price(row.get('down_ask'), 2)}"
        reasons = row.get("skip_reasons") or []
        status = "PAPER " + str(row.get("paper_outcome")) if row.get("paper_outcome") else (str(reasons[0]) if reasons else "OBSERVING")
        lines.append(f"{symbol:<7} {market:<9} {str(row.get('market_session', '-')):<13} {spot:<10} {up:<12} {down:<14} {status}")
    return "\n".join(lines)


def run_live_dashboard(journal: ShadowJournal, *, refresh_seconds: float, limit: int) -> None:
    if refresh_seconds <= 0 or limit < 1:
        raise ValueError("dashboard refresh_seconds and limit must be positive")
    console = Console()
    with _DashboardInput() as keyboard:
        try:
            with Live(console=console, screen=True, auto_refresh=False) as live:
                while True:
                    positions = journal.list_paper_positions()
                    rows = journal.dashboard_rows(limit)
                    live.update(_rich_dashboard(rows, positions, refresh_seconds=refresh_seconds), refresh=True)
                    deadline = time.monotonic() + refresh_seconds
                    while time.monotonic() < deadline:
                        if keyboard.poll() == "quit":
                            return
                        time.sleep(0.1)
        except KeyboardInterrupt:
            return


def _rich_dashboard(
    rows: tuple[Mapping[str, object], ...], positions: tuple[object, ...], *, refresh_seconds: float = 3.0
) -> Layout:
    open_positions = sum(getattr(position, "status") == "OPEN" for position in positions)
    settled_positions = sum(getattr(position, "status") == "SETTLED" for position in positions)
    regular = sum(row.get("market_session") == "REGULAR" for row in rows)
    signals = sum(row.get("paper_outcome") is not None for row in rows)
    maker_quotes = sum(len(row.get("maker_shadow_quotes") or []) for row in rows)
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right", ratio=1)
    left = Text()
    left.append("Mode: ", style="dim")
    left.append("SHADOW", style="bold cyan")
    left.append(f"   Markets: {len(rows)}   Regular: {regular}   Taker: {signals}   Maker: {maker_quotes}\n")
    left.append(f"Paper positions: {open_positions} open / {settled_positions} settled", style="green" if open_positions else "dim")
    right = Text()
    right.append("Data source: ", style="dim")
    right.append("SQLite shadow journal\n", style="cyan")
    right.append(f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}", style="dim")
    header.add_row(left, right)

    table = Table(expand=True, header_style="bold cyan")
    table.add_column("Symbol", style="bold")
    table.add_column("Market", style="dim")
    table.add_column("Session")
    table.add_column("Spot", justify="right")
    table.add_column("Up B/A", justify="right")
    table.add_column("Down B/A", justify="right")
    table.add_column("Fair Up", justify="right")
    table.add_column("IV", justify="right")
    table.add_column("Status", ratio=2)
    for row in rows:
        table.add_row(
            str(row.get("symbol", "-")), str(row.get("market_id", "-"))[-6:],
            _session_text(str(row.get("market_session", "-"))), _price_text(row.get("spot"), 2),
            _book_text(row.get("up_bid"), row.get("up_ask")), _book_text(row.get("down_bid"), row.get("down_ask")),
            _probability_text(row.get("fair_up_probability")), _iv_text(row.get("option_iv")), _status_text(row),
        )
    if not rows:
        table.add_row("-", "-", "-", "-", "-", "-", "-", "-", Text("Waiting for evaluations", style="yellow"))

    footer = Text()
    footer.append("Shadow only: no wallet, signing, or order submission.  ", style="dim")
    footer.append("Green=ready/paper  Yellow=data/session gate  Red=risk/data failure\n", style="dim")
    footer.append(f"Refresh: journal every {refresh_seconds:g}s  |  q: close  |  Ctrl+C: close", style="magenta")
    layout = Layout()
    layout.split_column(Layout(Panel(header, title="Polymarket Stock Shadow", border_style="blue"), size=7), Layout(Panel(table, title="Market Monitor", border_style="green"), ratio=1), Layout(Panel(footer, border_style="magenta"), size=4))
    return layout


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
