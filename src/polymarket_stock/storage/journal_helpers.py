"""Pure row and payload conversion helpers for journal repositories."""

from __future__ import annotations

import sqlite3
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


def _paper_position_from_row(row: sqlite3.Row) -> PaperPosition:
    return PaperPosition(
        position_id=str(row["position_id"]),
        opened_at=datetime.fromisoformat(str(row["opened_at"])),
        market_id=str(row["market_id"]),
        symbol=str(row["symbol"]),
        outcome=str(row["outcome"]),
        status=str(row["status"]),
        contracts=float(row["contracts"]),
        entry_ask=float(row["entry_ask"]),
        entry_fee=float(row["entry_fee"]),
        entry_slippage=float(row["entry_slippage"]),
        fair_probability=float(row["fair_probability"]),
        model_version=str(row["model_version"]),
        settled_at=datetime.fromisoformat(str(row["settled_at"])) if row["settled_at"] else None,
        settlement_outcome=str(row["settlement_outcome"]) if row["settlement_outcome"] else None,
        payout=float(row["payout"]) if row["payout"] is not None else None,
        realized_pnl=float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
        included_in_calibration=bool(row["included_in_calibration"]),
        exclusion_reason=str(row["exclusion_reason"]) if row["exclusion_reason"] else None,
    )


def _maker_shadow_quote_from_row(row: sqlite3.Row) -> MakerShadowQuote:
    return MakerShadowQuote(
        quote_id=str(row["quote_id"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        last_observed_at=datetime.fromisoformat(str(row["last_observed_at"])),
        market_id=str(row["market_id"]),
        symbol=str(row["symbol"]),
        outcome=str(row["outcome"]),
        status=str(row["status"]),
        limit_price=float(row["limit_price"]),
        fair_probability=float(row["fair_probability"]),
        theoretical_edge=float(row["theoretical_edge"]),
        best_bid=float(row["best_bid"]),
        best_ask=float(row["best_ask"]),
        touch_count=int(row["touch_count"]),
        last_touched_at=datetime.fromisoformat(str(row["last_touched_at"])) if row["last_touched_at"] else None,
        cancelled_at=datetime.fromisoformat(str(row["cancelled_at"])) if row["cancelled_at"] else None,
        cancel_reason=str(row["cancel_reason"]) if row["cancel_reason"] else None,
    )
