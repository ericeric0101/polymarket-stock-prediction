"""Baseline probability math used for research, not a claim of live fair value."""

from __future__ import annotations

from math import erf, log, sqrt


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def digital_up_probability(
    spot: float,
    threshold: float,
    annual_volatility: float,
    time_to_resolution_seconds: float,
) -> float:
    """Return P(S_T > threshold) under a zero-drift lognormal baseline.

    The function deliberately has no event adjustment. Phase 2 will add calibrated
    volatility regimes and data quality checks around this transparent baseline.
    """

    if spot <= 0 or threshold <= 0:
        raise ValueError("spot and threshold must be positive")
    if annual_volatility <= 0:
        raise ValueError("annual_volatility must be positive")
    if time_to_resolution_seconds < 0:
        raise ValueError("time_to_resolution_seconds cannot be negative")
    if time_to_resolution_seconds == 0:
        return 1.0 if spot > threshold else 0.0

    time_in_years = time_to_resolution_seconds / SECONDS_PER_YEAR
    volatility_term = annual_volatility * sqrt(time_in_years)
    d2 = (log(spot / threshold) - 0.5 * annual_volatility**2 * time_in_years) / volatility_term
    return normal_cdf(d2)
