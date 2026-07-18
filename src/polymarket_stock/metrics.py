"""Calibration and conservative paper-settlement metrics."""

from __future__ import annotations

from dataclasses import dataclass
from math import log


@dataclass(frozen=True)
class CalibrationMetrics:
    sample_size: int
    brier_score: float
    log_loss: float


def calibration_metrics(predictions: list[tuple[float, bool]]) -> CalibrationMetrics:
    if not predictions:
        raise ValueError("at least one settled prediction is required")
    for probability, _ in predictions:
        if not 0 <= probability <= 1:
            raise ValueError("probabilities must be between 0 and 1")
    epsilon = 1e-12
    brier = sum((probability - float(outcome)) ** 2 for probability, outcome in predictions) / len(predictions)
    log_loss = -sum(
        float(outcome) * log(max(epsilon, probability)) + (1.0 - float(outcome)) * log(max(epsilon, 1.0 - probability))
        for probability, outcome in predictions
    ) / len(predictions)
    return CalibrationMetrics(sample_size=len(predictions), brier_score=brier, log_loss=log_loss)


def paper_pnl(entry_price: float, won: bool, fee_rate: float, slippage: float) -> float:
    if not 0 < entry_price < 1 or not 0 <= fee_rate <= 1 or not 0 <= slippage <= 1:
        raise ValueError("invalid paper PnL inputs")
    cost = entry_price + entry_price * fee_rate + slippage
    return (1.0 if won else 0.0) - cost
