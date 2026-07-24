"""Conservative calibration recommendations derived from immutable paper entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping

from .journal import CheckpointObservation, PaperPosition, ReplayObservation
from .metrics import calibration_metrics


MINIMUM_CALIBRATION_SAMPLE = 30


@dataclass(frozen=True)
class CalibrationRecommendation:
    sample_size: int
    mean_absolute_error: float | None
    p90_absolute_error: float | None
    recommended_model_error_buffer: float | None
    recommended_minimum_edge: float | None
    status: str

    def as_payload(self) -> Mapping[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CheckpointCalibrationBucket:
    checkpoint_name: str
    probability_band: str
    sample_size: int
    predicted_up_probability: float
    realized_up_frequency: float
    brier_score: float


@dataclass(frozen=True)
class CheckpointCalibrationReport:
    sample_size: int
    brier_score: float | None
    log_loss: float | None
    buckets: tuple[CheckpointCalibrationBucket, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "sample_size": self.sample_size, "brier_score": self.brier_score, "log_loss": self.log_loss,
            "buckets": [asdict(bucket) for bucket in self.buckets],
        }


def calibrate_settled_positions(positions: Iterable[PaperPosition]) -> CalibrationRecommendation:
    settled = [
        position for position in positions
        if position.included_in_calibration and position.status == "SETTLED" and position.settlement_outcome
    ]
    return _calibrate_predictions((position.fair_probability, position.outcome == position.settlement_outcome) for position in settled)


def calibrate_market_observations(observations: Iterable[ReplayObservation]) -> CalibrationRecommendation:
    return _calibrate_predictions((item.fair_up_probability, item.winning_outcome == "UP") for item in observations)


def calibrate_checkpoint_observations(observations: Iterable[CheckpointObservation]) -> CheckpointCalibrationReport:
    """Report immutable checkpoint accuracy by probability band, without fitting on the same data."""

    items = tuple(observations)
    if not items:
        return CheckpointCalibrationReport(0, None, None, ())
    predictions = [(item.fair_up_probability, item.winning_outcome == "UP") for item in items]
    metrics = calibration_metrics(predictions)
    grouped: dict[tuple[str, str], list[CheckpointObservation]] = {}
    for item in items:
        grouped.setdefault((item.checkpoint_name, _probability_band(item.fair_up_probability)), []).append(item)
    buckets = []
    for (checkpoint_name, band), values in sorted(grouped.items()):
        bucket_predictions = [(value.fair_up_probability, value.winning_outcome == "UP") for value in values]
        bucket_metrics = calibration_metrics(bucket_predictions)
        buckets.append(CheckpointCalibrationBucket(
            checkpoint_name=checkpoint_name, probability_band=band, sample_size=len(values),
            predicted_up_probability=sum(value.fair_up_probability for value in values) / len(values),
            realized_up_frequency=sum(value.winning_outcome == "UP" for value in values) / len(values),
            brier_score=bucket_metrics.brier_score,
        ))
    return CheckpointCalibrationReport(metrics.sample_size, metrics.brier_score, metrics.log_loss, tuple(buckets))


def _probability_band(probability: float) -> str:
    lower = min(0.9, int(probability * 10) / 10)
    return f"{int(lower * 100):02d}-{int((lower + 0.1) * 100):02d}%"


def _calibrate_predictions(predictions: Iterable[tuple[float, bool]]) -> CalibrationRecommendation:
    items = tuple(predictions)
    if len(items) < MINIMUM_CALIBRATION_SAMPLE:
        return CalibrationRecommendation(len(items), None, None, None, None, "INSUFFICIENT_SETTLED_SAMPLE")
    errors = sorted(abs(probability - float(outcome)) for probability, outcome in items)
    mean_absolute_error = sum(errors) / len(errors)
    p90 = errors[min(len(errors) - 1, int(0.9 * len(errors)))]
    # Never weaken the pre-calibration floor. The edge floor remains independent of model error.
    return CalibrationRecommendation(
        len(items), mean_absolute_error, p90,
        max(0.02, p90), max(0.02, min(0.10, p90 / 2)), "READY_FOR_OPERATOR_REVIEW",
    )


def write_calibration_recommendation(path: Path, recommendation: CalibrationRecommendation) -> None:
    if recommendation.status != "READY_FOR_OPERATOR_REVIEW":
        raise ValueError("cannot write calibration before the minimum settled sample is available")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recommendation.as_payload(), sort_keys=True, indent=2) + "\n", encoding="utf-8")


def load_calibration_recommendation(path: Path) -> CalibrationRecommendation | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        recommendation = CalibrationRecommendation(
            sample_size=int(payload["sample_size"]), mean_absolute_error=float(payload["mean_absolute_error"]),
            p90_absolute_error=float(payload["p90_absolute_error"]),
            recommended_model_error_buffer=float(payload["recommended_model_error_buffer"]),
            recommended_minimum_edge=float(payload["recommended_minimum_edge"]), status=str(payload["status"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("model calibration file is invalid") from error
    if recommendation.status != "READY_FOR_OPERATOR_REVIEW" or recommendation.sample_size < MINIMUM_CALIBRATION_SAMPLE:
        raise ValueError("model calibration file has not passed the minimum-sample review gate")
    return recommendation
