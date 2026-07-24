"""Conservative edge calculation for hypothetical Yes or No purchases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .fees import estimate_taker_fee_usdc


Outcome = Literal["YES", "NO"]
EDGE_COMPARISON_EPSILON = 1e-12


@dataclass(frozen=True)
class EdgeAssessment:
    outcome: Outcome
    conservative_fair_probability: float
    executable_ask: float
    estimated_taker_fee: float
    edge: float
    minimum_edge: float

    @property
    def estimated_cost(self) -> float:
        """Compatibility alias: the only modeled execution cost is official taker fee."""

        return self.estimated_taker_fee

    @property
    def should_record_paper_trade(self) -> bool:
        return self.edge + EDGE_COMPARISON_EPSILON >= self.minimum_edge


def assess_buy_edge(
    *,
    fair_yes_probability: float,
    outcome: Outcome,
    executable_ask: float,
    fee_rate: float,
    model_error_buffer: float,
    minimum_edge: float,
) -> EdgeAssessment:
    """Calculate edge after every stated cost and a one-sided model-error buffer."""

    values = {
        "fair_yes_probability": fair_yes_probability,
        "executable_ask": executable_ask,
        "fee_rate": fee_rate,
        "model_error_buffer": model_error_buffer,
        "minimum_edge": minimum_edge,
    }
    for name, value in values.items():
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    raw_probability = fair_yes_probability if outcome == "YES" else 1.0 - fair_yes_probability
    conservative_probability = max(0.0, raw_probability - model_error_buffer)
    estimated_taker_fee = estimate_taker_fee_usdc(shares=1, price=executable_ask, fee_rate=fee_rate)
    return EdgeAssessment(
        outcome=outcome,
        conservative_fair_probability=conservative_probability,
        executable_ask=executable_ask,
        estimated_taker_fee=estimated_taker_fee,
        edge=conservative_probability - executable_ask - estimated_taker_fee,
        minimum_edge=minimum_edge,
    )
