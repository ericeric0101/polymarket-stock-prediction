"""Out-of-sample calibration research for selected-side shadow signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import sqrt
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .journal import FirstSignalCalibrationObservation
from .metrics import calibration_metrics


NEW_YORK = ZoneInfo("America/New_York")
KELLY_MINIMUM_COHORT_SAMPLES = 100
_EMPIRICAL_PRIOR_STRENGTH = 20
_MINIMUM_BIN_TRAINING_SAMPLES = 5


@dataclass(frozen=True)
class CalibrationBucket:
    dimension: str
    segment: str
    sample_size: int
    mean_predicted_probability: float
    realized_win_rate: float
    calibration_bias: float
    brier_score: float
    log_loss: float
    win_rate_ci_low: float
    win_rate_ci_high: float


@dataclass(frozen=True)
class StratifiedCalibrationReport:
    sample_size: int
    buckets: tuple[CalibrationBucket, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {"sample_size": self.sample_size, "buckets": [asdict(bucket) for bucket in self.buckets]}


@dataclass(frozen=True)
class SizingCohortReadiness:
    iv_regime: str
    sample_size: int
    brier_score: float | None
    status: str


@dataclass(frozen=True)
class SizingReadiness:
    sample_size: int
    position_sizing: str
    kelly_enabled: bool
    kelly_minimum_cohort_samples: int
    cohorts: tuple[SizingCohortReadiness, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "sample_size": self.sample_size,
            "position_sizing": self.position_sizing,
            "kelly_enabled": self.kelly_enabled,
            "kelly_minimum_cohort_samples": self.kelly_minimum_cohort_samples,
            "cohorts": [asdict(cohort) for cohort in self.cohorts],
        }


@dataclass(frozen=True)
class ProbabilityCalibrationFold:
    training_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    training_sample_size: int
    validation_sample_size: int
    raw_brier_score: float
    calibrated_brier_score: float
    raw_log_loss: float
    calibrated_log_loss: float


@dataclass(frozen=True)
class WalkForwardProbabilityCalibrationReport:
    status: str
    distinct_dates: int
    required_distinct_dates: int
    folds: tuple[ProbabilityCalibrationFold, ...]
    validation_sample_size: int
    raw_brier_score: float | None
    calibrated_brier_score: float | None
    raw_log_loss: float | None
    calibrated_log_loss: float | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "distinct_dates": self.distinct_dates,
            "required_distinct_dates": self.required_distinct_dates,
            "folds": [asdict(fold) for fold in self.folds],
            "validation_sample_size": self.validation_sample_size,
            "raw_brier_score": self.raw_brier_score,
            "calibrated_brier_score": self.calibrated_brier_score,
            "raw_log_loss": self.raw_log_loss,
            "calibrated_log_loss": self.calibrated_log_loss,
        }


def stratified_first_signal_calibration(
    observations: Iterable[FirstSignalCalibrationObservation],
) -> StratifiedCalibrationReport:
    """Report reliability by independent operator-relevant cohort dimensions."""

    items = tuple(observations)
    dimensions: dict[str, dict[str, list[FirstSignalCalibrationObservation]]] = {"overall": {"ALL": list(items)}}
    for name, selector in (
        ("probability_band", lambda item: _probability_band(item.selected_fair_probability)),
        ("direction", lambda item: item.model_outcome),
        ("iv_regime", lambda item: item.iv_regime),
        ("option_iv_status", lambda item: item.option_iv_status),
        ("volatility_estimator", lambda item: item.volatility_estimator),
        ("entry_time_bucket", lambda item: _entry_time_bucket(item.evaluated_at)),
        ("spot_provider", lambda item: item.spot_provider),
        ("model_version", lambda item: item.model_version),
        ("threshold_distance", lambda item: _threshold_distance_band(item.threshold_distance_bps)),
    ):
        groups: dict[str, list[FirstSignalCalibrationObservation]] = {}
        for item in items:
            groups.setdefault(selector(item), []).append(item)
        dimensions[name] = groups
    buckets = [
        _bucket(dimension, segment, values)
        for dimension, groups in dimensions.items()
        for segment, values in sorted(groups.items())
        if values
    ]
    return StratifiedCalibrationReport(len(items), tuple(buckets))


def sizing_readiness(observations: Iterable[FirstSignalCalibrationObservation]) -> SizingReadiness:
    """Explicitly keep Kelly disabled until each data-quality cohort is large enough."""

    items = tuple(observations)
    cohorts = []
    for regime in ("IV_VALID", "REALIZED_VOL_FALLBACK"):
        values = [item for item in items if item.iv_regime == regime]
        metrics = _metrics(values) if values else None
        cohorts.append(
            SizingCohortReadiness(
                iv_regime=regime,
                sample_size=len(values),
                brier_score=metrics.brier_score if metrics else None,
                status=(
                    "KELLY_DISABLED_INSUFFICIENT_SAMPLES"
                    if len(values) < KELLY_MINIMUM_COHORT_SAMPLES
                    else "OPERATOR_REVIEW_REQUIRED"
                ),
            )
        )
    return SizingReadiness(
        sample_size=len(items),
        position_sizing="FIXED_SMALL_POSITION_ONLY",
        kelly_enabled=False,
        kelly_minimum_cohort_samples=KELLY_MINIMUM_COHORT_SAMPLES,
        cohorts=tuple(cohorts),
    )


def walk_forward_probability_calibration(
    observations: Iterable[FirstSignalCalibrationObservation],
    *,
    training_days: int = 20,
    validation_days: int = 5,
    minimum_training_samples: int = 50,
) -> WalkForwardProbabilityCalibrationReport:
    """Fit bin-level probability shrinkage on earlier dates and score later dates only."""

    if training_days < 1 or validation_days < 1 or minimum_training_samples < 1:
        raise ValueError("training_days, validation_days, and minimum_training_samples must be positive")
    items = tuple(observations)
    by_date: dict[str, list[FirstSignalCalibrationObservation]] = {}
    for item in items:
        by_date.setdefault(_new_york_date(item.evaluated_at), []).append(item)
    dates = tuple(sorted(by_date))
    required_dates = training_days + validation_days
    if len(dates) < required_dates:
        return WalkForwardProbabilityCalibrationReport(
            "INSUFFICIENT_DISTINCT_DAYS",
            len(dates),
            required_dates,
            (),
            0,
            None,
            None,
            None,
            None,
        )

    folds = []
    raw_pairs: list[tuple[float, bool]] = []
    calibrated_pairs: list[tuple[float, bool]] = []
    for validation_start in range(training_days, len(dates) - validation_days + 1, validation_days):
        train_dates = dates[validation_start - training_days : validation_start]
        validation_dates = dates[validation_start : validation_start + validation_days]
        training = [item for day in train_dates for item in by_date[day]]
        validation = [item for day in validation_dates for item in by_date[day]]
        if len(training) < minimum_training_samples:
            continue
        calibrator = _BinnedShrinkageCalibrator.fit(training)
        raw = [(item.selected_fair_probability, _won(item)) for item in validation]
        calibrated = [(calibrator.transform(item.selected_fair_probability), _won(item)) for item in validation]
        raw_metrics = calibration_metrics(raw)
        calibrated_metrics = calibration_metrics(calibrated)
        folds.append(
            ProbabilityCalibrationFold(
                train_dates,
                validation_dates,
                len(training),
                len(validation),
                raw_metrics.brier_score,
                calibrated_metrics.brier_score,
                raw_metrics.log_loss,
                calibrated_metrics.log_loss,
            )
        )
        raw_pairs.extend(raw)
        calibrated_pairs.extend(calibrated)
    if not folds:
        return WalkForwardProbabilityCalibrationReport(
            "INSUFFICIENT_TRAINING_SAMPLES",
            len(dates),
            required_dates,
            (),
            0,
            None,
            None,
            None,
            None,
        )
    raw_metrics = calibration_metrics(raw_pairs)
    calibrated_metrics = calibration_metrics(calibrated_pairs)
    return WalkForwardProbabilityCalibrationReport(
        "READY_FOR_OPERATOR_REVIEW",
        len(dates),
        required_dates,
        tuple(folds),
        len(raw_pairs),
        raw_metrics.brier_score,
        calibrated_metrics.brier_score,
        raw_metrics.log_loss,
        calibrated_metrics.log_loss,
    )


@dataclass(frozen=True)
class _BinnedShrinkageCalibrator:
    wins_by_band: Mapping[str, int]
    samples_by_band: Mapping[str, int]

    @classmethod
    def fit(cls, observations: Iterable[FirstSignalCalibrationObservation]) -> "_BinnedShrinkageCalibrator":
        wins: dict[str, int] = {}
        samples: dict[str, int] = {}
        for item in observations:
            band = _probability_band(item.selected_fair_probability)
            samples[band] = samples.get(band, 0) + 1
            wins[band] = wins.get(band, 0) + int(_won(item))
        return cls(wins, samples)

    def transform(self, raw_probability: float) -> float:
        band = _probability_band(raw_probability)
        sample_size = self.samples_by_band.get(band, 0)
        if sample_size < _MINIMUM_BIN_TRAINING_SAMPLES:
            return raw_probability
        return (self.wins_by_band.get(band, 0) + _EMPIRICAL_PRIOR_STRENGTH * raw_probability) / (
            sample_size + _EMPIRICAL_PRIOR_STRENGTH
        )


def _bucket(dimension: str, segment: str, observations: list[FirstSignalCalibrationObservation]) -> CalibrationBucket:
    metrics = _metrics(observations)
    wins = sum(_won(item) for item in observations)
    sample_size = len(observations)
    realized = wins / sample_size
    predicted = sum(item.selected_fair_probability for item in observations) / sample_size
    lower, upper = _wilson_interval(wins, sample_size)
    return CalibrationBucket(
        dimension,
        segment,
        sample_size,
        predicted,
        realized,
        realized - predicted,
        metrics.brier_score,
        metrics.log_loss,
        lower,
        upper,
    )


def _metrics(observations: Iterable[FirstSignalCalibrationObservation]):
    return calibration_metrics([(item.selected_fair_probability, _won(item)) for item in observations])


def _won(item: FirstSignalCalibrationObservation) -> bool:
    return item.model_outcome == item.winning_outcome


def _new_york_date(value: datetime) -> str:
    return value.astimezone(NEW_YORK).date().isoformat()


def _entry_time_bucket(value: datetime) -> str:
    local = value.astimezone(NEW_YORK)
    minutes = local.hour * 60 + local.minute
    if minutes < 10 * 60:
        return "OPEN_0930_0959"
    if minutes < 12 * 60:
        return "1000_1159_EDT"
    if minutes < 14 * 60:
        return "1200_1359_EDT"
    if minutes < 15 * 60 + 30:
        return "1400_1529_EDT"
    return "1530_CLOSE_EDT"


def _probability_band(probability: float) -> str:
    lower = min(0.9, int(probability * 10) / 10)
    return f"{int(lower * 100):02d}-{int((lower + 0.1) * 100):02d}%"


def _threshold_distance_band(distance_bps: float | None) -> str:
    if distance_bps is None:
        return "UNKNOWN"
    distance = abs(distance_bps)
    if distance <= 25:
        return "ABS_LE_25_BPS"
    if distance <= 50:
        return "ABS_26_50_BPS"
    if distance <= 100:
        return "ABS_51_100_BPS"
    return "ABS_GT_100_BPS"


def _wilson_interval(wins: int, sample_size: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if sample_size == 0:
        return 0.0, 1.0
    proportion = wins / sample_size
    denominator = 1.0 + z * z / sample_size
    center = (proportion + z * z / (2 * sample_size)) / denominator
    radius = z * sqrt((proportion * (1 - proportion) + z * z / (4 * sample_size)) / sample_size) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)
