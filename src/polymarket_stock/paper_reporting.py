"""Aggregate hold-to-settlement paper-position results without execution access."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .journal import PaperPosition
from .metrics import calibration_metrics


@dataclass(frozen=True)
class PaperPerformance:
    open_positions: int
    settled_positions: int
    wins: int
    total_realized_pnl: float
    win_rate: float | None
    brier_score: float | None
    log_loss: float | None
    excluded_positions: int = 0

    def as_payload(self) -> Mapping[str, object]:
        return asdict(self)


def paper_performance(positions: Iterable[PaperPosition]) -> PaperPerformance:
    all_positions = tuple(positions)
    excluded_positions = sum(not position.included_in_calibration for position in all_positions)
    all_positions = tuple(position for position in all_positions if position.included_in_calibration)
    settled = tuple(position for position in all_positions if position.status == "SETTLED")
    if not settled:
        return PaperPerformance(
            open_positions=sum(position.status == "OPEN" for position in all_positions), settled_positions=0,
            wins=0, total_realized_pnl=0.0, win_rate=None, brier_score=None, log_loss=None,
            excluded_positions=excluded_positions,
        )
    predictions = [(position.fair_probability, position.outcome == position.settlement_outcome) for position in settled]
    calibration = calibration_metrics(predictions)
    wins = sum(outcome for _, outcome in predictions)
    return PaperPerformance(
        open_positions=sum(position.status == "OPEN" for position in all_positions),
        settled_positions=len(settled), wins=wins,
        total_realized_pnl=sum(position.realized_pnl or 0.0 for position in settled),
        win_rate=wins / len(settled), brier_score=calibration.brier_score, log_loss=calibration.log_loss,
        excluded_positions=excluded_positions,
    )
