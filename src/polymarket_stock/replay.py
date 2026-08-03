"""Replay immutable paper-entry inputs against official settled outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .journal import PaperPosition, ReplayObservation
from .metrics import calibration_metrics


@dataclass(frozen=True)
class ReplayReport:
    settled_positions: int
    skipped_open_positions: int
    total_realized_pnl: float
    brier_score: float | None
    log_loss: float | None
    mean_entry_edge_before_costs: float | None
    skipped_excluded_positions: int = 0

    def as_payload(self) -> Mapping[str, object]:
        return asdict(self)


def replay_settled_positions(positions: Iterable[PaperPosition]) -> ReplayReport:
    all_positions = tuple(positions)
    skipped_excluded_positions = sum(not position.included_in_calibration for position in all_positions)
    all_positions = tuple(position for position in all_positions if position.included_in_calibration)
    settled = tuple(
        position for position in all_positions if position.status == "SETTLED" and position.settlement_outcome
    )
    if not settled:
        return ReplayReport(
            0,
            sum(position.status == "OPEN" for position in all_positions),
            0.0,
            None,
            None,
            None,
            skipped_excluded_positions,
        )
    predictions = [(position.fair_probability, position.outcome == position.settlement_outcome) for position in settled]
    metrics = calibration_metrics(predictions)
    edges = [position.fair_probability - position.entry_ask for position in settled]
    return ReplayReport(
        len(settled),
        sum(position.status == "OPEN" for position in all_positions),
        sum(position.realized_pnl or 0 for position in settled),
        metrics.brier_score,
        metrics.log_loss,
        sum(edges) / len(edges),
        skipped_excluded_positions,
    )


def replay_market_observations(observations: Iterable[ReplayObservation]) -> ReplayReport:
    """Evaluate the latest valid observation per market against its official result."""

    items = tuple(observations)
    if not items:
        return ReplayReport(0, 0, 0.0, None, None, None)
    predictions = [(item.fair_up_probability, item.winning_outcome == "UP") for item in items]
    metrics = calibration_metrics(predictions)
    edges = [
        (item.fair_up_probability - item.up_ask)
        if item.up_ask is not None
        else ((1 - item.fair_up_probability) - item.down_ask)
        if item.down_ask is not None
        else 0.0
        for item in items
    ]
    return ReplayReport(len(items), 0, 0.0, metrics.brier_score, metrics.log_loss, sum(edges) / len(edges))
