"""Event-conditioned volatility research helpers.

This module is report-only infrastructure. It does not turn an event into a
trade, bypass an event gate, or change the realtime default model.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from .baseline import TRADING_DAYS_PER_YEAR


@dataclass(frozen=True)
class EventReturn:
    date: str
    event_type: str
    close_to_close_return: float


@dataclass(frozen=True)
class EventVolatilityResult:
    event_type: str
    sample_size: int
    minimum_samples: int
    annualized_volatility: float | None
    status: str


def event_conditioned_volatility(
    events: list[EventReturn], *, event_type: str, minimum_samples: int = 8
) -> EventVolatilityResult:
    if minimum_samples < 2:
        raise ValueError("minimum_samples must be at least 2")
    cohort = [event.close_to_close_return for event in events if event.event_type == event_type]
    if len(cohort) < minimum_samples:
        return EventVolatilityResult(event_type, len(cohort), minimum_samples, None, "INSUFFICIENT_EVENT_SAMPLES")
    mean = sum(cohort) / len(cohort)
    variance = sum((value - mean) ** 2 for value in cohort) / (len(cohort) - 1)
    return EventVolatilityResult(
        event_type, len(cohort), minimum_samples, sqrt(variance * TRADING_DAYS_PER_YEAR), "READY_FOR_RESEARCH"
    )
