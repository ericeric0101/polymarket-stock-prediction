from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.journal import FirstSignalCalibrationObservation
from polymarket_stock.probability_calibration import (
    sizing_readiness,
    stratified_first_signal_calibration,
    walk_forward_probability_calibration,
)


def _observation(index: int, *, days: int = 0, probability: float = 0.8, won: bool = True,
                 direction: str = "UP", regime: str = "REALIZED_VOL_FALLBACK") -> FirstSignalCalibrationObservation:
    evaluated_at = datetime(2026, 7, 20, 14, tzinfo=UTC) + timedelta(days=days, minutes=index)
    return FirstSignalCalibrationObservation(
        market_id=f"market-{days}-{index}", symbol="TSLA", evaluated_at=evaluated_at,
        model_outcome=direction, selected_fair_probability=probability, entry_ask=0.5, entry_fee=0.01,
        winning_outcome=direction if won else ("DOWN" if direction == "UP" else "UP"),
        model_version="test-v1", option_iv_status="IV_VALID" if regime == "IV_VALID" else "IV_UNAVAILABLE",
        iv_regime=regime, spot_provider="PYTH_HERMES", threshold_distance_bps=42.0,
    )


class ProbabilityCalibrationTests(unittest.TestCase):
    def test_stratified_report_keeps_direction_iv_and_threshold_cohorts_separate(self) -> None:
        report = stratified_first_signal_calibration((
            _observation(1, probability=0.8, won=True, direction="UP"),
            _observation(2, probability=0.8, won=False, direction="DOWN", regime="IV_VALID"),
        ))
        buckets = {(bucket.dimension, bucket.segment): bucket for bucket in report.buckets}
        self.assertEqual(report.sample_size, 2)
        self.assertEqual(buckets[("direction", "UP")].sample_size, 1)
        self.assertEqual(buckets[("iv_regime", "IV_VALID")].realized_win_rate, 0.0)
        self.assertEqual(buckets[("threshold_distance", "ABS_26_50_BPS")].sample_size, 2)
        self.assertLess(buckets[("overall", "ALL")].win_rate_ci_low, 0.5)

    def test_sizing_readiness_keeps_kelly_disabled_without_per_regime_samples(self) -> None:
        readiness = sizing_readiness([_observation(index) for index in range(10)])
        self.assertEqual(readiness.position_sizing, "FIXED_SMALL_POSITION_ONLY")
        self.assertFalse(readiness.kelly_enabled)
        self.assertEqual(readiness.cohorts[1].status, "KELLY_DISABLED_INSUFFICIENT_SAMPLES")

    def test_walk_forward_never_fits_validation_days(self) -> None:
        observations = []
        for day in range(3):
            observations.extend(_observation(index, days=day, probability=0.8, won=False) for index in range(3))
        report = walk_forward_probability_calibration(
            observations, training_days=2, validation_days=1, minimum_training_samples=5,
        )
        self.assertEqual(report.status, "READY_FOR_OPERATOR_REVIEW")
        self.assertEqual(report.folds[0].training_dates, ("2026-07-20", "2026-07-21"))
        self.assertEqual(report.folds[0].validation_dates, ("2026-07-22",))
        self.assertLess(report.calibrated_brier_score or 1, report.raw_brier_score or 0)

    def test_walk_forward_requires_distinct_days(self) -> None:
        report = walk_forward_probability_calibration([_observation(1)], training_days=2, validation_days=1)
        self.assertEqual(report.status, "INSUFFICIENT_DISTINCT_DAYS")
