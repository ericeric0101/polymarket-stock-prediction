from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.checkpoints import latest_checkpoint
from polymarket_stock.journal import ShadowJournal


class CheckpointTests(unittest.TestCase):
    def test_latest_checkpoint_uses_new_york_time(self) -> None:
        self.assertIsNone(latest_checkpoint(datetime(2026, 7, 20, 13, 59, tzinfo=UTC)))
        self.assertEqual(latest_checkpoint(datetime(2026, 7, 20, 14, 0, tzinfo=UTC)), ("2026-07-20", "1000_EDT"))
        self.assertEqual(latest_checkpoint(datetime(2026, 7, 20, 19, 31, tzinfo=UTC)), ("2026-07-20", "1530_EDT"))

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
