"""Official settlement reconciliation collaborator for the shadow supervisor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Protocol


class SettlementRecord(Protocol):
    position_id: str
    market_id: str
    outcome: str


class SettlementOwner(Protocol):
    journal: object
    gamma: object
    event_sink: Callable[[str, Mapping[str, object]], None]


async def settle_open_positions(owner: SettlementOwner) -> None:
    open_positions = await asyncio.to_thread(owner.journal.list_paper_positions, "OPEN")
    for position in open_positions:
        try:
            settlement = await asyncio.to_thread(owner.gamma.get_market_settlement, position.market_id)
        except Exception as error:  # Public data failures should not stop the supervisor.
            owner.event_sink(
                "PAPER_SETTLEMENT_CHECK_FAILED",
                {"position_id": position.position_id, "market_id": position.market_id, "error": str(error)},
            )
            continue
        outcome = settlement.winning_outcome.upper() if settlement.winning_outcome else None
        if not settlement.closed or outcome not in {"UP", "DOWN"}:
            continue
        settled = await asyncio.to_thread(
            owner.journal.settle_paper_position,
            position.position_id,
            settlement_outcome=outcome,
            settlement_payload=settlement.raw_payload,
        )
        await asyncio.to_thread(owner.journal.cancel_maker_shadow_quotes, position.market_id, "MARKET_SETTLED")
        owner.event_sink(
            "PAPER_POSITION_SETTLED",
            {
                "position_id": settled.position_id,
                "market_id": settled.market_id,
                "outcome": settled.outcome,
                "settlement_outcome": outcome,
                "realized_pnl": settled.realized_pnl,
            },
        )


async def reconcile_evaluation_settlements(owner: SettlementOwner) -> None:
    """Attach official outcomes to all valid observations, not only paper entries."""

    pending_market_ids = await asyncio.to_thread(owner.journal.pending_evaluation_market_ids)
    for market_id in pending_market_ids:
        try:
            settlement = await asyncio.to_thread(owner.gamma.get_market_settlement, market_id)
        except Exception as error:
            owner.event_sink("EVALUATION_SETTLEMENT_CHECK_FAILED", {"market_id": market_id, "error": str(error)})
            continue
        outcome = settlement.winning_outcome.upper() if settlement.winning_outcome else None
        if settlement.closed and outcome in {"UP", "DOWN"}:
            await asyncio.to_thread(owner.journal.record_market_settlement, market_id, outcome, settlement.raw_payload)
            await asyncio.to_thread(owner.journal.cancel_maker_shadow_quotes, market_id, "MARKET_SETTLED")
            owner.event_sink("EVALUATION_MARKET_SETTLED", {"market_id": market_id, "settlement_outcome": outcome})


async def reconcile_all(owner: SettlementOwner) -> None:
    await reconcile(owner)


async def reconcile(owner: SettlementOwner) -> None:
    await settle_open_positions(owner)
    await reconcile_evaluation_settlements(owner)
