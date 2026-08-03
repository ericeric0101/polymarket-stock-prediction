from __future__ import annotations

from datetime import datetime
import unittest

from polymarket_stock.buffer_sweep import buffer_values, run_buffer_sweep, walk_forward_buffer_sweep
from polymarket_stock.journal import BufferSweepObservation


def _observation(
    market_id: str,
    date: str,
    checkpoint: str,
    fair_up: float,
    outcome: str,
    *,
    hour: int = 14,
) -> BufferSweepObservation:
    return BufferSweepObservation(
        market_id=market_id,
        symbol=market_id,
        checkpoint_date=date,
        checkpoint_name=checkpoint,
        evaluated_at=datetime.fromisoformat(f"{date}T{hour:02d}:00:00+00:00"),
        fair_up_probability=fair_up,
        up_ask=0.60,
        down_ask=0.40,
        up_taker_fee=0.01,
        down_taker_fee=0.01,
        winning_outcome=outcome,
    )


class BufferSweepTests(unittest.TestCase):
    def test_sweep_uses_first_eligible_checkpoint_once_per_market_day(self) -> None:
        observations = [
            _observation("one", "2026-07-20", "1000_EDT", 0.75, "UP", hour=14),
            _observation("one", "2026-07-20", "1200_EDT", 0.95, "UP", hour=16),
        ]
        report = run_buffer_sweep(observations, buffers=(0.05, 0.10), minimum_edge=0.02)
        at_five = report.results[0]
        at_ten = report.results[1]
        self.assertEqual(at_five.selected_trades, 1)
        self.assertAlmostEqual(at_five.total_realized_pnl, 0.39)
        self.assertEqual(at_ten.selected_trades, 1)
        self.assertAlmostEqual(at_ten.total_realized_pnl, 0.39)

    def test_larger_buffer_can_remove_a_trade(self) -> None:
        observations = [_observation("one", "2026-07-20", "1000_EDT", 0.70, "UP")]
        report = run_buffer_sweep(observations, buffers=(0.05, 0.08), minimum_edge=0.02)
        self.assertEqual(report.results[0].selected_trades, 1)
        self.assertEqual(report.results[1].selected_trades, 0)

    def test_walk_forward_never_selects_from_validation_dates(self) -> None:
        observations = [
            _observation("one", "2026-07-20", "1200_EDT", 0.70, "UP"),
            _observation("two", "2026-07-21", "1200_EDT", 0.70, "UP"),
            _observation("three", "2026-07-22", "1200_EDT", 0.70, "UP"),
        ]
        report = walk_forward_buffer_sweep(
            observations,
            buffers=(0.05, 0.08),
            minimum_edge=0.02,
            training_days=2,
            validation_days=1,
            minimum_training_trades=1,
        )
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.windows[0].training_dates, ("2026-07-20", "2026-07-21"))
        self.assertEqual(report.windows[0].validation_dates, ("2026-07-22",))
        self.assertEqual(report.windows[0].selected_buffer, 0.05)

    def test_buffer_values_has_a_stable_inclusive_range(self) -> None:
        self.assertEqual(buffer_values(0.0, 0.02, 0.01), (0.0, 0.01, 0.02))
