"""Read-only reports joining core checkpoints with independent price-ladder snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .journal import PaperPosition, ShadowJournal, _database_connection
from .price_ladder import (
    CrossMarketDiagnostic, LadderProbabilityPoint, diagnose_cross_market, fit_monotonic_curve, probability_point,
)
from .price_ladder_journal import PriceLadderJournal, StoredLadderSnapshot
from .probability_calibration import sizing_readiness
from .quality import observable_equity_market_date, us_equity_session


NEW_YORK = ZoneInfo("America/New_York")
CHECKPOINTS = ("1200_EDT", "1400_EDT", "1530_EDT")


def cross_market_diagnostics(
    journal_path: Path, *, market_date: str | None = None,
) -> tuple[CrossMarketDiagnostic, ...]:
    ladder = PriceLadderJournal(journal_path)
    ladder.initialize()
    snapshots = ladder.list_snapshots(market_date=market_date, checkpoint_only=True)
    grouped_ladder = _group_checkpoint_points(snapshots)
    query = """SELECT checkpoint_date, checkpoint_name, symbol, payload_json
        FROM checkpoint_observations WHERE eligible_for_calibration = 1
        AND checkpoint_name IN ('1200_EDT', '1400_EDT', '1530_EDT')"""
    parameters: tuple[object, ...] = ()
    if market_date:
        query += " AND checkpoint_date = ?"
        parameters = (market_date,)
    query += " ORDER BY checkpoint_date, checkpoint_name, symbol"
    import json
    with _database_connection(journal_path) as connection:
        rows = connection.execute(query, parameters).fetchall()
    diagnostics = []
    for checkpoint_date, checkpoint_name, symbol, payload_json in rows:
        payload = json.loads(str(payload_json))
        price_to_beat = payload.get("price_to_beat")
        fair_up = payload.get("fair_up_probability")
        if price_to_beat is None or fair_up is None:
            continue
        points = grouped_ladder.get((str(checkpoint_date), str(checkpoint_name), str(symbol).upper()), ())
        diagnostics.append(diagnose_cross_market(
            symbol=str(symbol), market_date=str(checkpoint_date), checkpoint_name=str(checkpoint_name),
            price_to_beat=float(price_to_beat), model_up_probability=float(fair_up),
            up_down_market_probability=_up_down_market_probability(payload), points=points,
        ))
    return tuple(diagnostics)


def cross_market_report(journal_path: Path, *, market_date: str | None = None) -> Mapping[str, object]:
    diagnostics = cross_market_diagnostics(journal_path, market_date=market_date)
    counts = {status: sum(item.status == status for item in diagnostics) for status in ("CONFIRM", "MIXED", "DISAGREE", "UNRELIABLE")}
    return {
        "market_date": market_date,
        "diagnostics": [item.as_payload() for item in diagnostics],
        "status_counts": counts,
        "note": "Research only. Price-ladder data never changes paper entries or supervisor thresholds.",
    }


def research_dashboard_state(
    journal_path: Path, *, now: datetime | None = None, limit: int = 18, daily_entry_limit: int = 5,
) -> Mapping[str, object]:
    if limit < 1 or daily_entry_limit < 1:
        raise ValueError("research dashboard limits must be positive")
    timestamp = now or datetime.now(UTC)
    market_date = observable_equity_market_date(timestamp).isoformat()
    core = ShadowJournal(journal_path)
    core_rows = list(core.dashboard_rows(limit, now=timestamp))
    ladder = PriceLadderJournal(journal_path)
    ladder.initialize()
    latest = ladder.latest_snapshot_rows(market_date)
    curves = []
    for symbol in sorted({item.symbol for item in latest}):
        symbol_rows = tuple(item for item in latest if item.symbol == symbol)
        points = tuple(point for point in (_snapshot_point(item) for item in symbol_rows) if point is not None)
        curve = fit_monotonic_curve(points)
        curves.append({
            "symbol": symbol,
            "market_date": market_date,
            "observed_at": max((item.observed_at for item in symbol_rows), default=timestamp).isoformat(),
            "violations": curve.violations,
            "points": [
                {
                    **asdict(point), "adjusted_probability": curve.adjusted_probabilities[index],
                }
                for index, point in enumerate(curve.points)
            ],
        })
    diagnostics = cross_market_diagnostics(journal_path, market_date=market_date)
    latest_diagnostic: dict[str, CrossMarketDiagnostic] = {}
    for item in diagnostics:
        current = latest_diagnostic.get(item.symbol)
        if current is None or CHECKPOINTS.index(item.checkpoint_name) > CHECKPOINTS.index(current.checkpoint_name):
            latest_diagnostic[item.symbol] = item
    return {
        "generated_at": timestamp.isoformat(), "market_date": market_date,
        "core_rows": core_rows,
        "live_markets": _live_market_payload(core_rows, timestamp),
        "market_status": _market_status_payload(timestamp, market_date),
        "paper_portfolio": _paper_portfolio_payload(
            core.list_paper_positions(), core.first_signal_performance(),
            sizing_readiness(core.list_first_signal_calibration_observations()).as_payload(),
            timestamp=timestamp, daily_entry_limit=daily_entry_limit,
        ),
        "ladder_curves": curves,
        "cross_market": [latest_diagnostic[symbol].as_payload() for symbol in sorted(latest_diagnostic)],
        "isolation": {"affects_entries": False, "affects_sizing": False, "research_only": True},
    }


def _market_status_payload(timestamp: datetime, market_date: str) -> Mapping[str, object]:
    session = us_equity_session(timestamp)
    decision_enabled = session == "REGULAR"
    return {
        "equity_session": session,
        "observation_market_date": market_date,
        "decision_enabled": decision_enabled,
        "message": (
            "Regular session: model decisions may be evaluated."
            if decision_enabled
            else "US equities closed: Polymarket observation continues; entries are disabled."
        ),
    }


def _live_market_payload(
    rows: Iterable[Mapping[str, object]], timestamp: datetime,
) -> list[Mapping[str, object]]:
    live_rows = []
    for row in rows:
        observed_at = _optional_datetime(row.get("evaluated_at"))
        book_age = _optional_float(row.get("book_age_seconds"))
        if observed_at is not None:
            book_age = max(book_age or 0.0, (timestamp - observed_at).total_seconds())
        up_bid, up_ask = _optional_float(row.get("up_bid")), _optional_float(row.get("up_ask"))
        down_bid, down_ask = _optional_float(row.get("down_bid")), _optional_float(row.get("down_ask"))
        complete = all(value is not None for value in (up_bid, up_ask, down_bid, down_ask))
        state = (
            "LIVE" if complete and book_age is not None and book_age <= 30
            else "STALE" if complete else "INCOMPLETE"
        )
        live_rows.append({
            "market_id": row.get("market_id"), "symbol": row.get("symbol"),
            "observed_at": observed_at.isoformat() if observed_at else None,
            "book_age_seconds": book_age, "state": state,
            "up_bid": up_bid, "up_ask": up_ask, "down_bid": down_bid, "down_ask": down_ask,
            "up_spread": up_ask - up_bid if up_bid is not None and up_ask is not None else None,
            "down_spread": down_ask - down_bid if down_bid is not None and down_ask is not None else None,
            "market_up_probability": _up_down_market_probability(row),
            "model_up_probability": _optional_float(row.get("fair_up_probability")),
            "spot": _optional_float(row.get("spot")),
            "price_to_beat": _optional_float(row.get("price_to_beat")),
            "up_book": row.get("up_book") if isinstance(row.get("up_book"), Mapping) else {},
            "down_book": row.get("down_book") if isinstance(row.get("down_book"), Mapping) else {},
            "skip_reasons": list(row.get("skip_reasons") or ()),
        })
    return live_rows


def _paper_portfolio_payload(
    positions: Iterable[PaperPosition], signal_performance: Mapping[str, object],
    sizing: Mapping[str, object], *, timestamp: datetime, daily_entry_limit: int,
) -> Mapping[str, object]:
    market_date = timestamp.astimezone(NEW_YORK).date()
    all_positions = tuple(positions)
    selected = tuple(sorted((
        position for position in all_positions
        if position.included_in_calibration
        and position.opened_at.astimezone(NEW_YORK).date() == market_date
    ), key=lambda item: item.opened_at))
    settled = tuple(position for position in selected if position.status == "SETTLED")
    wins = sum(position.outcome == position.settlement_outcome for position in settled)
    entries = []
    for position in selected:
        won = position.status == "SETTLED" and position.outcome == position.settlement_outcome
        status = "OPEN" if position.status != "SETTLED" else f"{position.settlement_outcome} {'WIN' if won else 'LOSS'}"
        entries.append({
            "position_id": position.position_id, "opened_at": position.opened_at.isoformat(),
            "opened_at_ny": position.opened_at.astimezone(NEW_YORK).isoformat(),
            "symbol": position.symbol, "side": position.outcome, "contracts": position.contracts,
            "entry_ask": position.entry_ask, "entry_fee": position.entry_fee,
            "fair_probability": position.fair_probability, "status": status,
            "settlement_outcome": position.settlement_outcome, "realized_pnl": position.realized_pnl,
        })
    return {
        "market_date": market_date.isoformat(), "daily_entry_limit": daily_entry_limit,
        "selected_count": len(selected), "settled_count": len(settled),
        "wins": wins, "losses": len(settled) - wins,
        "win_rate": wins / len(settled) if settled else None,
        "open_positions": sum(position.status == "OPEN" for position in all_positions),
        "settled_positions": sum(position.status == "SETTLED" for position in all_positions),
        "entries": entries, "first_signal_performance": dict(signal_performance), "sizing": dict(sizing),
    }


def _group_checkpoint_points(
    snapshots: Iterable[StoredLadderSnapshot],
) -> dict[tuple[str, str, str], tuple[LadderProbabilityPoint, ...]]:
    earliest: dict[tuple[str, str, str, str], StoredLadderSnapshot] = {}
    for item in snapshots:
        if item.checkpoint_name not in CHECKPOINTS:
            continue
        key = (item.market_date, item.checkpoint_name, item.symbol, item.market_id)
        if key not in earliest or item.observed_at < earliest[key].observed_at:
            earliest[key] = item
    grouped: dict[tuple[str, str, str], list[LadderProbabilityPoint]] = {}
    for item in earliest.values():
        point = _snapshot_point(item)
        if point is not None:
            grouped.setdefault((item.market_date, str(item.checkpoint_name), item.symbol), []).append(point)
    return {key: tuple(value) for key, value in grouped.items()}


def _snapshot_point(item: StoredLadderSnapshot) -> LadderProbabilityPoint | None:
    return probability_point(
        strike=item.strike, market_id=item.market_id,
        yes_bid=item.yes_bid, yes_ask=item.yes_ask, no_bid=item.no_bid, no_ask=item.no_ask,
        yes_depth=item.yes_bid_depth + item.yes_ask_depth,
        no_depth=item.no_bid_depth + item.no_ask_depth,
    )


def _up_down_market_probability(payload: Mapping[str, object]) -> float | None:
    up_bid = _optional_float(payload.get("up_bid"))
    up_ask = _optional_float(payload.get("up_ask"))
    down_bid = _optional_float(payload.get("down_bid"))
    down_ask = _optional_float(payload.get("down_ask"))
    lower = [value for value in (up_bid, 1 - down_ask if down_ask is not None else None) if value is not None]
    upper = [value for value in (up_ask, 1 - down_bid if down_bid is not None else None) if value is not None]
    if not lower or not upper:
        return None
    lower_bound, upper_bound = max(lower), min(upper)
    if lower_bound > upper_bound:
        lower_bound, upper_bound = upper_bound, lower_bound
    return (lower_bound + upper_bound) / 2


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None
