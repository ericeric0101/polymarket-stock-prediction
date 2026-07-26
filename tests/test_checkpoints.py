from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.checkpoints import checkpoint_window, latest_checkpoint
from polymarket_stock.journal import ShadowJournal
from polymarket_stock.trading_calendar import previous_nyse_trading_day


class CheckpointTests(unittest.TestCase):
    def test_latest_checkpoint_uses_new_york_time(self) -> None:
        self.assertIsNone(latest_checkpoint(datetime(2026, 7, 20, 13, 59, tzinfo=UTC)))
        self.assertEqual(latest_checkpoint(datetime(2026, 7, 20, 14, 0, tzinfo=UTC)), ("2026-07-20", "1000_EDT"))
        self.assertEqual(latest_checkpoint(datetime(2026, 7, 20, 19, 31, tzinfo=UTC)), ("2026-07-20", "1530_EDT"))

    def test_checkpoint_window_rejects_late_capture(self) -> None:
        self.assertIsNone(checkpoint_window(datetime(2026, 7, 20, 15, 6, tzinfo=UTC)))
        window = checkpoint_window(datetime(2026, 7, 20, 14, 4, 59, tzinfo=UTC))
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.checkpoint_name, "1000_EDT")
        self.assertEqual(window.delay_seconds, 299.0)

    def test_previous_nyse_trading_day_skips_weekend_and_holiday(self) -> None:
        self.assertEqual(previous_nyse_trading_day(datetime(2026, 7, 20).date()).isoformat(), "2026-07-17")
        self.assertEqual(previous_nyse_trading_day(datetime(2026, 7, 6).date()).isoformat(), "2026-07-02")

    def test_checkpoint_is_immutable_per_market_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            payload = {
                "evaluated_at": "2026-07-20T14:00:01+00:00", "market_id": "market-1", "symbol": "TSLA",
                "fair_up_probability": 0.61, "up_ask": 0.55, "down_ask": 0.46,
                "option_iv": None, "model_version": "realized-vol-baseline-v1",
            }
            self.assertTrue(journal.record_checkpoint_observation(
                checkpoint_date="2026-07-20", checkpoint_name="1000_EDT", payload=payload
            ))
            self.assertFalse(journal.record_checkpoint_observation(
                checkpoint_date="2026-07-20", checkpoint_name="1000_EDT", payload=payload
            ))
            journal.record_market_settlement("market-1", "UP", {"id": "market-1"})
            observations = journal.list_checkpoint_observations(eligible_only=False)
            self.assertTrue(observations[0].eligible_for_calibration)

    def test_late_checkpoint_is_preserved_but_excluded_from_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            payload = {
                "evaluated_at": "2026-07-20T15:00:00+00:00", "market_id": "late-market", "symbol": "TSLA",
                "fair_up_probability": 0.61, "up_ask": 0.55, "down_ask": 0.46,
                "option_iv": None, "model_version": "realized-vol-baseline-v1",
            }
            self.assertTrue(journal.record_checkpoint_observation(
                checkpoint_date="2026-07-20", checkpoint_name="1000_EDT", payload=payload
            ))
            journal.record_market_settlement("late-market", "UP", {"id": "late-market"})
            self.assertEqual(journal.list_checkpoint_observations(), ())
            observation = journal.list_checkpoint_observations(eligible_only=False)[0]
            self.assertFalse(observation.eligible_for_calibration)
            self.assertEqual(observation.checkpoint_delay_seconds, 3600.0)
