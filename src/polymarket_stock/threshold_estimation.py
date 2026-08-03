"""Calibrated non-Pyth estimates for a Pyth-resolved prior close."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ThresholdSource:
    source: str
    price: float
    calibration_source: str | None = None


@dataclass(frozen=True)
class ThresholdEstimate:
    price: float
    quality: str
    source_count: int
    calibration_samples: int
    estimated_error_bps: float
    source_dispersion_bps: float
    sources: tuple[Mapping[str, object], ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "price": self.price,
            "threshold_quality": self.quality,
            "estimated": True,
            "source_count": self.source_count,
            "calibration_samples": self.calibration_samples,
            "estimated_error_bps": self.estimated_error_bps,
            "source_dispersion_bps": self.source_dispersion_bps,
            "sources": list(self.sources),
        }


def calibrated_threshold_estimate(
    sources: Iterable[ThresholdSource],
    calibrations: Iterable[Mapping[str, object]],
) -> ThresholdEstimate:
    """Return a robust Pyth-scale estimate without treating it as settlement truth.

    Each source is debiased using prior Pyth-final comparisons. The median avoids
    one delayed/free data source moving the threshold by itself. With no historic
    sample, the source is retained but carries the conservative default error.
    """
    usable = tuple(item for item in sources if item.price > 0 and item.source)
    if not usable:
        raise ValueError("THRESHOLD_ESTIMATE_UNAVAILABLE")
    rows = tuple(item for item in calibrations if str(item.get("status")) == "COMPLETE")
    details = []
    adjusted_prices = []
    errors = []
    for item in usable:
        key = item.calibration_source or item.source
        sample_errors = _source_errors(rows, key)
        bias = median(sample_errors) if sample_errors else 0.0
        p90 = _percentile_abs(sample_errors, 0.90) if sample_errors else 35.0
        adjusted = item.price / (1.0 + bias / 10_000.0)
        adjusted_prices.append(adjusted)
        errors.append(p90)
        details.append(
            {
                "source": item.source,
                "raw_price": item.price,
                "calibration_source": key,
                "bias_bps": bias,
                "calibration_samples": len(sample_errors),
                "p90_absolute_error_bps": p90,
                "adjusted_price": adjusted,
            }
        )
    estimate = median(adjusted_prices)
    dispersion = (
        max(abs(value - estimate) / estimate * 10_000 for value in adjusted_prices) if len(adjusted_prices) > 1 else 0.0
    )
    samples = sum(int(item["calibration_samples"]) for item in details)
    error = max(median(errors), dispersion)
    if len(usable) >= 3 and samples >= 5:
        quality = "CALIBRATED_MULTI_SOURCE_HIGH"
    elif len(usable) >= 2:
        quality = "CALIBRATED_MULTI_SOURCE_MEDIUM"
    else:
        quality = "SINGLE_SOURCE_ESTIMATE"
    return ThresholdEstimate(
        price=estimate,
        quality=quality,
        source_count=len(usable),
        calibration_samples=samples,
        estimated_error_bps=error,
        source_dispersion_bps=dispersion,
        sources=tuple(details),
    )


def _source_errors(rows: Iterable[Mapping[str, object]], source: str) -> list[float]:
    values = []
    for row in rows:
        errors = row.get("source_errors_bps")
        if isinstance(errors, Mapping) and isinstance(errors.get(source), (int, float)):
            values.append(float(errors[source]))
        elif source == "FINNHUB_CLOSE_WINDOW" and isinstance(row.get("difference_bps"), (int, float)):
            values.append(float(row["difference_bps"]))
    return values


def _percentile_abs(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(abs(value) for value in values)
    if not ordered:
        return 35.0
    index = min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999))
    return ordered[index]
