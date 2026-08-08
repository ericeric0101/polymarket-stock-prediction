"""Read-only reports joining core checkpoints with independent price-ladder snapshots."""

from __future__ import annotations

from dataclasses import asdict
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .journal import PaperPosition, ShadowJournal
from .above_x_research import above_x_coverage_report
from .storage.sqlite import database_connection
from .price_ladder import (
    CrossMarketDiagnostic,
    LadderProbabilityPoint,
    diagnose_cross_market,
    fit_monotonic_curve,
    probability_point,
)
from .price_ladder_journal import PriceLadderJournal, StoredLadderSnapshot
from .probability_calibration import sizing_readiness
from .evaluation_payload import read_entry_diagnostic_flags, read_entry_policy_category
from .quality import observable_equity_market_date, us_equity_session


NEW_YORK = ZoneInfo("America/New_York")
CHECKPOINTS = ("1200_EDT", "1400_EDT", "1530_EDT")


def cross_market_diagnostics(
    journal_path: Path,
    *,
    market_date: str | None = None,
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

    with database_connection(journal_path) as connection:
        rows = connection.execute(query, parameters).fetchall()
    diagnostics = []
    for checkpoint_date, checkpoint_name, symbol, payload_json in rows:
        payload = json.loads(str(payload_json))
        price_to_beat = payload.get("price_to_beat")
        fair_up = payload.get("fair_up_probability")
        if price_to_beat is None or fair_up is None:
            continue
        points = grouped_ladder.get((str(checkpoint_date), str(checkpoint_name), str(symbol).upper()), ())
        # This view is intentionally limited to symbols for which a ladder was
        # actually collected. Producing an unreliable row for every core symbol
        # without a ladder obscures the useful TSLA/NVDA comparison.
        if not points:
            continue
        diagnostics.append(
            diagnose_cross_market(
                symbol=str(symbol),
                market_date=str(checkpoint_date),
                checkpoint_name=str(checkpoint_name),
                price_to_beat=float(price_to_beat),
                model_up_probability=float(fair_up),
                up_down_market_probability=_up_down_market_probability(payload),
                points=points,
            )
        )
    return tuple(diagnostics)


def cross_market_report(journal_path: Path, *, market_date: str | None = None) -> Mapping[str, object]:
    diagnostics = cross_market_diagnostics(journal_path, market_date=market_date)
    counts = {
        status: sum(item.status == status for item in diagnostics)
        for status in ("CONFIRM", "MIXED", "DISAGREE", "UNRELIABLE")
    }
    return {
        "market_date": market_date,
        "diagnostics": [item.as_payload() for item in diagnostics],
        "status_counts": counts,
        "note": "Research only. Price-ladder data never changes paper entries or supervisor thresholds.",
    }


def research_dashboard_state(
    journal_path: Path,
    *,
    now: datetime | None = None,
    limit: int = 18,
    daily_entry_limit: int = 5,
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
    candidate_rows = []
    for symbol in sorted({item.symbol for item in latest}):
        symbol_rows = tuple(item for item in latest if item.symbol == symbol)
        points = tuple(point for point in (_snapshot_point(item) for item in symbol_rows) if point is not None)
        curve = fit_monotonic_curve(points)
        snapshots_by_market = {item.market_id: item for item in symbol_rows}
        for index, point in enumerate(curve.points):
            candidate_rows.append(
                _ladder_research_candidate(
                    symbol=symbol,
                    point=point,
                    adjusted_probability=curve.adjusted_probabilities[index],
                    snapshot=snapshots_by_market[point.market_id],
                    monotonic_violations=curve.violations,
                )
            )
        curves.append(
            {
                "symbol": symbol,
                "market_date": market_date,
                "observed_at": max((item.observed_at for item in symbol_rows), default=timestamp).isoformat(),
                "violations": curve.violations,
                "points": [
                    {
                        **asdict(point),
                        "adjusted_probability": curve.adjusted_probabilities[index],
                    }
                    for index, point in enumerate(curve.points)
                ],
            }
        )
    diagnostics = cross_market_diagnostics(journal_path, market_date=market_date)
    latest_diagnostic: dict[str, CrossMarketDiagnostic] = {}
    for item in diagnostics:
        current = latest_diagnostic.get(item.symbol)
        if current is None or CHECKPOINTS.index(item.checkpoint_name) > CHECKPOINTS.index(current.checkpoint_name):
            latest_diagnostic[item.symbol] = item
    diagnostics_by_symbol = {item.symbol: item.as_payload() for item in latest_diagnostic.values()}
    ladder_symbols = tuple(sorted({item["symbol"] for item in curves}))
    return {
        "generated_at": timestamp.isoformat(),
        "market_date": market_date,
        "core_rows": core_rows,
        "live_markets": _live_market_payload(core_rows, timestamp),
        "market_status": _market_status_payload(timestamp, market_date),
        "paper_portfolio": _paper_portfolio_payload(
            core.list_paper_positions(),
            core.first_signal_performance(),
            sizing_readiness(core.list_first_signal_calibration_observations()).as_payload(),
            timestamp=timestamp,
            daily_entry_limit=daily_entry_limit,
        ),
        "ladder_curves": curves,
        "ladder_candidates": sorted(candidate_rows, key=lambda item: (item["symbol"], item["strike"])),
        "cross_market": [diagnostics_by_symbol[symbol] for symbol in sorted(diagnostics_by_symbol)],
        "cross_market_readiness": _cross_market_readiness(
            timestamp=timestamp,
            ladder_symbols=ladder_symbols,
            diagnostics=tuple(latest_diagnostic.values()),
        ),
        "above_x": _above_x_dashboard_payload(),
        "above_x_veto": ladder.list_veto_observations(market_date=market_date),
        "isolation": {"affects_entries": False, "affects_sizing": False, "research_only": True},
    }


def _ladder_research_candidate(
    *,
    symbol: str,
    point: LadderProbabilityPoint,
    adjusted_probability: float,
    snapshot: StoredLadderSnapshot,
    monotonic_violations: int,
) -> Mapping[str, object]:
    """Return a transparent, non-executing single-strike research candidate.

    Price-ladder snapshots do not persist per-token fee rates. ``gross_edge``
    deliberately excludes fees, and the web UI must not call it a net edge.
    """

    yes_edge = adjusted_probability - snapshot.yes_ask if snapshot.yes_ask is not None else None
    no_probability = 1.0 - adjusted_probability
    no_edge = no_probability - snapshot.no_ask if snapshot.no_ask is not None else None
    available = tuple(
        (side, probability, ask, edge)
        for side, probability, ask, edge in (
            ("YES", adjusted_probability, snapshot.yes_ask, yes_edge),
            ("NO", no_probability, snapshot.no_ask, no_edge),
        )
        if ask is not None and edge is not None
    )
    if available:
        side, probability, ask, gross_edge = max(available, key=lambda item: item[3])
        action = f"RESEARCH_{side}" if gross_edge > 0 else "NO_GROSS_EDGE"
    else:
        side, probability, ask, gross_edge, action = "-", None, None, None, "MISSING_EXECUTABLE_ASK"
    warnings = []
    if point.spread > 0.15:
        warnings.append("WIDE_EXECUTABLE_RANGE")
    if monotonic_violations:
        warnings.append("MONOTONICALLY_ADJUSTED")
    if action == "MISSING_EXECUTABLE_ASK":
        warnings.append("MISSING_EXECUTABLE_ASK")
    return {
        "symbol": symbol,
        "market_id": point.market_id,
        "strike": point.strike,
        "side": side,
        "action": action,
        "limit_price": ask,
        "model_probability": probability,
        "gross_edge": gross_edge,
        "yes_ask": snapshot.yes_ask,
        "yes_probability": adjusted_probability,
        "yes_gross_edge": yes_edge,
        "no_ask": snapshot.no_ask,
        "no_probability": no_probability,
        "no_gross_edge": no_edge,
        "lower_bound": point.lower_bound,
        "upper_bound": point.upper_bound,
        "executable_width": point.spread,
        "fee_status": "NOT_CAPTURED_SUBTRACT_CURRENT_TAKER_FEE",
        "quality_warnings": warnings,
    }


def _cross_market_readiness(
    *,
    timestamp: datetime,
    ladder_symbols: tuple[str, ...],
    diagnostics: tuple[CrossMarketDiagnostic, ...],
) -> Mapping[str, object]:
    """Explain an empty cross-market panel without inventing a comparison."""

    session = us_equity_session(timestamp)
    if not ladder_symbols:
        return {
            "status": "WAITING_FOR_LADDER_SNAPSHOTS",
            "message": "Start collect-price-ladders; no TSLA/NVDA ladder snapshots exist for this contract date.",
            "symbols": [],
        }
    if not diagnostics:
        return {
            "status": "WAITING_FOR_MATCHING_CHECKPOINT",
            "message": (
                "Ladder data is live, but a cross-market comparison appears only after the same "
                "12:00, 14:00, or 15:30 EDT Core checkpoint is recorded."
                if session == "REGULAR"
                else "Ladder data is available; the next comparison waits for a regular-session Core checkpoint."
            ),
            "symbols": list(ladder_symbols),
        }
    return {
        "status": "READY",
        "message": "Latest matching checkpoint comparison for collected ladder symbols.",
        "symbols": list(ladder_symbols),
    }


def _above_x_dashboard_payload() -> Mapping[str, object]:
    discovery = Path("data/historical/above_x_discovery.json")
    if not discovery.is_file():
        return {"status": "NO_DISCOVERY", "coverage": None, "replay": None}
    try:
        coverage = above_x_coverage_report(
            discovery_path=discovery,
            output_dir=Path("data/historical/above_x"),
            spot_data_dir=Path("data/historical/90d"),
        ).as_payload()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "ERROR", "error": str(error), "coverage": None, "replay": None}
    replay_path = Path("data/historical/above_x_replay.json")
    replay = None
    if replay_path.is_file():
        try:
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            replay = None
    return {"status": "READY", "coverage": coverage, "replay": replay}


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
    rows: Iterable[Mapping[str, object]],
    timestamp: datetime,
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
            "LIVE" if complete and book_age is not None and book_age <= 30 else "STALE" if complete else "INCOMPLETE"
        )
        live_rows.append(
            {
                "market_id": row.get("market_id"),
                "symbol": row.get("symbol"),
                "observed_at": observed_at.isoformat() if observed_at else None,
                "book_age_seconds": book_age,
                "state": state,
                "up_bid": up_bid,
                "up_ask": up_ask,
                "down_bid": down_bid,
                "down_ask": down_ask,
                "up_spread": up_ask - up_bid if up_bid is not None and up_ask is not None else None,
                "down_spread": down_ask - down_bid if down_bid is not None and down_ask is not None else None,
                "market_up_probability": _up_down_market_probability(row),
                "model_up_probability": _optional_float(row.get("fair_up_probability")),
                "spot": _optional_float(row.get("spot")),
                "price_to_beat": _optional_float(row.get("price_to_beat")),
                "threshold_quality": str(row.get("threshold_quality") or "UNKNOWN"),
                "threshold_warning": row.get("threshold_warning"),
                "threshold_source_count": row.get("source_count"),
                "threshold_calibration_samples": row.get("calibration_samples"),
                "threshold_estimated_error_bps": row.get("estimated_error_bps"),
                "entry_diagnostic_flags": list(read_entry_diagnostic_flags(row)),
                "entry_policy_category": read_entry_policy_category(row),
                "up_book": row.get("up_book") if isinstance(row.get("up_book"), Mapping) else {},
                "down_book": row.get("down_book") if isinstance(row.get("down_book"), Mapping) else {},
                "skip_reasons": list(row.get("skip_reasons") or ()),
            }
        )
    return live_rows


def _paper_portfolio_payload(
    positions: Iterable[PaperPosition],
    signal_performance: Mapping[str, object],
    sizing: Mapping[str, object],
    *,
    timestamp: datetime,
    daily_entry_limit: int,
) -> Mapping[str, object]:
    market_date = timestamp.astimezone(NEW_YORK).date()
    all_positions = tuple(positions)
    selected = tuple(
        sorted(
            (
                position
                for position in all_positions
                if position.included_in_calibration and position.opened_at.astimezone(NEW_YORK).date() == market_date
            ),
            key=lambda item: item.opened_at,
        )
    )
    settled = tuple(position for position in selected if position.status == "SETTLED")
    wins = sum(position.outcome == position.settlement_outcome for position in settled)
    entries = []
    for position in selected:
        won = position.status == "SETTLED" and position.outcome == position.settlement_outcome
        status = "OPEN" if position.status != "SETTLED" else f"{position.settlement_outcome} {'WIN' if won else 'LOSS'}"
        entries.append(
            {
                "position_id": position.position_id,
                "opened_at": position.opened_at.isoformat(),
                "opened_at_ny": position.opened_at.astimezone(NEW_YORK).isoformat(),
                "symbol": position.symbol,
                "side": position.outcome,
                "contracts": position.contracts,
                "entry_ask": position.entry_ask,
                "entry_fee": position.entry_fee,
                "fair_probability": position.fair_probability,
                "status": status,
                "settlement_outcome": position.settlement_outcome,
                "realized_pnl": position.realized_pnl,
            }
        )
    return {
        "market_date": market_date.isoformat(),
        "daily_entry_limit": daily_entry_limit,
        "selected_count": len(selected),
        "settled_count": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": wins / len(settled) if settled else None,
        "open_positions": sum(position.status == "OPEN" for position in all_positions),
        "settled_positions": sum(position.status == "SETTLED" for position in all_positions),
        "entries": entries,
        "first_signal_performance": dict(signal_performance),
        "sizing": dict(sizing),
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
        strike=item.strike,
        market_id=item.market_id,
        yes_bid=item.yes_bid,
        yes_ask=item.yes_ask,
        no_bid=item.no_bid,
        no_ask=item.no_ask,
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
