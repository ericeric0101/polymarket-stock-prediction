"""Read-only reports joining core checkpoints with independent price-ladder snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .journal import ShadowJournal, _database_connection
from .price_ladder import (
    CrossMarketDiagnostic, LadderProbabilityPoint, diagnose_cross_market, fit_monotonic_curve, probability_point,
)
from .price_ladder_journal import PriceLadderJournal, StoredLadderSnapshot


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


def research_dashboard_state(journal_path: Path, *, now: datetime | None = None, limit: int = 18) -> Mapping[str, object]:
    timestamp = now or datetime.now(UTC)
    market_date = timestamp.astimezone(NEW_YORK).date().isoformat()
    core = ShadowJournal(journal_path)
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
        "core_rows": list(core.dashboard_rows(limit, now=timestamp)),
        "ladder_curves": curves,
        "cross_market": [latest_diagnostic[symbol].as_payload() for symbol in sorted(latest_diagnostic)],
        "isolation": {"affects_entries": False, "affects_sizing": False, "research_only": True},
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
