from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.calibration import (
    calibrate_checkpoint_observations,
    calibrate_settled_positions,
    load_calibration_recommendation,
    write_calibration_recommendation,
)
from polymarket_stock.journal import CheckpointObservation, PaperPosition
from polymarket_stock.replay import replay_settled_positions


def _position(index: int, settled: bool = True) -> PaperPosition:
    return PaperPosition(
        position_id=str(index),
        opened_at=datetime(2026, 7, 20, tzinfo=UTC),
        market_id=str(index),
        symbol="TSLA",
        outcome="UP",
        status="SETTLED" if settled else "OPEN",
        contracts=1,
        entry_ask=0.50,
        entry_fee=0.005,
        entry_slippage=0.001,
        fair_probability=0.60,
        model_version="test",
        settled_at=datetime(2026, 7, 20, 20, tzinfo=UTC) if settled else None,
        settlement_outcome="UP" if settled else None,
        payout=1 if settled else None,
        realized_pnl=0.494 if settled else None,
    )


class CalibrationReplayTests(unittest.TestCase):
    def test_replay_uses_only_settled_immutable_entries(self) -> None:
        report = replay_settled_positions([_position(1), _position(2, False)])
        self.assertEqual(report.settled_positions, 1)
        self.assertEqual(report.skipped_open_positions, 1)
        self.assertAlmostEqual(report.mean_entry_edge_before_costs or 0, 0.10)

    def test_calibration_requires_conservative_minimum_sample(self) -> None:
        insufficient = calibrate_settled_positions([_position(1)])
        ready = calibrate_settled_positions([_position(index) for index in range(30)])
        self.assertEqual(insufficient.status, "INSUFFICIENT_SETTLED_SAMPLE")
        self.assertEqual(ready.status, "READY_FOR_OPERATOR_REVIEW")
        self.assertGreaterEqual(ready.recommended_model_error_buffer or 0, 0.02)

    def test_ready_calibration_round_trips_as_supervisor_input(self) -> None:
        import tempfile
        from pathlib import Path

        ready = calibrate_settled_positions([_position(index) for index in range(30)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model_calibration.json"
            write_calibration_recommendation(path, ready)
            loaded = load_calibration_recommendation(path)
        self.assertEqual(loaded, ready)

    def test_checkpoint_calibration_reports_probability_bands(self) -> None:
        observations = [
            CheckpointObservation(
                "one",
                "TSLA",
                "2026-07-20",
                "1000_EDT",
                datetime(2026, 7, 20, 14, tzinfo=UTC),
                0.72,
                0.60,
                0.30,
                "iv-blend-v1",
                0.3,
                "UP",
                datetime(2026, 7, 20, 14, tzinfo=UTC),
                0.0,
                True,
            ),
            CheckpointObservation(
                "two",
                "AAPL",
                "2026-07-20",
                "1000_EDT",
                datetime(2026, 7, 20, 14, tzinfo=UTC),
                0.78,
                0.70,
                0.20,
                "iv-blend-v1",
                0.3,
                "DOWN",
                datetime(2026, 7, 20, 14, tzinfo=UTC),
                0.0,
                True,
            ),
        ]
        report = calibrate_checkpoint_observations(observations)
        self.assertEqual(report.sample_size, 2)
        self.assertEqual(report.buckets[0].probability_band, "70-80%")
        self.assertAlmostEqual(report.buckets[0].realized_up_frequency, 0.5)
