"""Passive-maker quote proposals for observation, never order submission."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Literal


MakerOutcome = Literal["UP", "DOWN"]


@dataclass(frozen=True)
class MakerQuoteProposal:
    outcome: MakerOutcome
    limit_price: float
    fair_probability: float
    theoretical_edge: float
    best_bid: float
    best_ask: float


def propose_maker_buy_quote(
    *,
    outcome: MakerOutcome,
    fair_probability: float,
    best_bid: float | None,
    best_ask: float | None,
    minimum_edge: float = 0.005,
    tick_size: float = 0.01,
) -> MakerQuoteProposal | None:
    """Propose a passive buy below fair value without assuming a fill or rebate."""

    if best_bid is None or best_ask is None:
        return None
    values = (fair_probability, best_bid, best_ask, minimum_edge, tick_size)
    if not 0 <= fair_probability <= 1 or not 0 <= best_bid <= best_ask <= 1 or minimum_edge < 0 or tick_size <= 0:
        raise ValueError("invalid maker quote inputs")

    tick = Decimal(str(tick_size))
    target = Decimal(str(fair_probability - minimum_edge))
    passive_ceiling = Decimal(str(best_ask)) - tick
    limit = min(target, passive_ceiling)
    if limit <= 0:
        return None
    limit = (limit / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    limit_price = float(limit)
    if limit_price <= 0 or limit_price >= best_ask:
        return None
    return MakerQuoteProposal(
        outcome=outcome,
        limit_price=limit_price,
        fair_probability=fair_probability,
        theoretical_edge=fair_probability - limit_price,
        best_bid=best_bid,
        best_ask=best_ask,
    )
