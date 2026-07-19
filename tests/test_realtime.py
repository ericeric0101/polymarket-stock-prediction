from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.baseline import DailyClose
from polymarket_stock.realtime import RealtimeBaselineEvaluator


class RealtimeBaselineEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 20, 15, tzinfo=UTC)
        self.closes = [DailyClose((self.now.date() - timedelta(days=day)).isoformat(), 100 + day) for day in range(30, -1, -1)]
        self.evaluator = RealtimeBaselineEvaluator(
            market_id="market-1", symbol="TSLA", resolves_at=self.now + timedelta(hours=5),
            closes=self.closes, spot_provider="FINNHUB",
        )

    def test_fresh_state_produces_shadow_evaluation(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=0.50, down_ask=0.50,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNotNone(result.fair_up_probability)
        self.assertEqual(result.skip_reasons, ())
        self.assertIn(result.as_payload()["signal_status"], {"NO_PAPER_TRADE", "PAPER_UP", "PAPER_DOWN"})

    def test_stale_or_incomplete_state_is_recorded_without_signal(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=None, down_ask=None,
            spot_age_seconds=20.0, book_age_seconds=None, stream_ready=False, trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNone(result.fair_up_probability)
        self.assertIn("STALE_OR_INCOMPLETE_STREAM", result.skip_reasons)
        self.assertIn("MISSING_EXECUTABLE_ASK", result.skip_reasons)
