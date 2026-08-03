"""Pure row and payload conversion helpers for journal repositories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .journal_models import MakerShadowQuote, PaperPosition


def _payload_execution_fee(payload: Mapping[str, object], outcome_prefix: str) -> float | None:
    """Use the fee frozen with the checkpoint, never a recalculated current fee."""

    value = payload.get(f"{outcome_prefix}_taker_fee")
    if value is None:
        return None
    try:
        fee = float(value)
    except (TypeError, ValueError):
        return None
    return fee if fee >= 0 else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_position_from_row(row: tuple[object, ...]) -> PaperPosition:
    return PaperPosition(
        position_id=str(row[0]),
        opened_at=datetime.fromisoformat(str(row[1])),
        market_id=str(row[2]),
        symbol=str(row[3]),
        outcome=str(row[4]),
        status=str(row[5]),
        contracts=float(row[6]),
        entry_ask=float(row[7]),
        entry_fee=float(row[8]),
        entry_slippage=float(row[9]),
        fair_probability=float(row[10]),
        model_version=str(row[11]),
        settled_at=datetime.fromisoformat(str(row[12])) if row[12] else None,
        settlement_outcome=str(row[13]) if row[13] else None,
        payout=float(row[14]) if row[14] is not None else None,
        realized_pnl=float(row[15]) if row[15] is not None else None,
        included_in_calibration=bool(row[16]),
        exclusion_reason=str(row[17]) if row[17] else None,
    )


def _maker_shadow_quote_from_row(row: tuple[object, ...]) -> MakerShadowQuote:
    return MakerShadowQuote(
        quote_id=str(row[0]),
        created_at=datetime.fromisoformat(str(row[1])),
        last_observed_at=datetime.fromisoformat(str(row[2])),
        market_id=str(row[3]),
        symbol=str(row[4]),
        outcome=str(row[5]),
        status=str(row[6]),
        limit_price=float(row[7]),
        fair_probability=float(row[8]),
        theoretical_edge=float(row[9]),
        best_bid=float(row[10]),
        best_ask=float(row[11]),
        touch_count=int(row[12]),
        last_touched_at=datetime.fromisoformat(str(row[13])) if row[13] else None,
        cancelled_at=datetime.fromisoformat(str(row[14])) if row[14] else None,
        cancel_reason=str(row[15]) if row[15] else None,
    )
