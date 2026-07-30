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
            closes=self.closes, spot_provider="FINNHUB", up_fee_rate=0.04, down_fee_rate=0.04,
        )

    def test_fresh_state_produces_shadow_evaluation(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=0.50, down_ask=0.50,
            up_bid=0.49, down_bid=0.49,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNotNone(result.fair_up_probability)
        self.assertEqual(result.skip_reasons, ())
        self.assertIn(result.as_payload()["signal_status"], {"NO_PAPER_TRADE", "PAPER_UP", "PAPER_DOWN"})
        self.assertEqual(result.paper_outcome, result.model_outcome)
        self.assertEqual(result.paper_entry_eligible, result.model_outcome is not None)
        self.assertAlmostEqual(result.model_error_buffer, 0.02)

    def test_dual_mode_records_comparison_without_second_paper_outcome(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=0.50, down_ask=0.50,
            up_bid=0.49, down_bid=0.49, spot_age_seconds=0.2, book_age_seconds=0.1,
            stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
        )
        payload = result.as_payload()
        self.assertEqual(result.volatility_estimator, "CLOSE_TO_CLOSE")
        self.assertEqual(len(payload["comparison_models"]), 1)
        comparison = payload["comparison_models"][0]
        self.assertEqual(comparison["volatility_estimator"], "EWMA")
        self.assertIn("fair_up_probability", comparison)
        self.assertNotIn("paper_outcome", comparison)

    def test_stale_or_incomplete_state_is_recorded_without_signal(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=None, down_ask=None,
            spot_age_seconds=20.0, book_age_seconds=None, stream_ready=False, trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNone(result.fair_up_probability)
        self.assertIn("STALE_OR_INCOMPLETE_STREAM", result.skip_reasons)
        self.assertIn("MISSING_EXECUTABLE_ASK", result.skip_reasons)

    def test_non_regular_session_and_crossed_book_are_blocked(self) -> None:
        result = self.evaluator.evaluate(
            now=datetime(2026, 7, 19, 15, tzinfo=UTC), spot=101.0, up_ask=0.50, down_ask=0.50,
            up_bid=0.51, down_bid=0.49,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("BOOK",),
        )
        self.assertIn("NON_REGULAR_SESSION:WEEKEND", result.skip_reasons)
        self.assertIn("CROSSED_UP_BOOK", result.skip_reasons)

    def test_fresh_cross_source_divergence_is_blocked(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, reference_spot=100.0, reference_spot_age_seconds=1.0,
            up_ask=0.50, down_ask=0.50, up_bid=0.49, down_bid=0.49,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIn("CROSS_SOURCE_SPOT_DIVERGENCE", result.skip_reasons)
        self.assertAlmostEqual(result.cross_source_difference or 0, 1 / 101)

    def test_unavailable_option_iv_uses_labeled_realized_volatility_fallback(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=0.01, down_ask=0.99, up_bid=0.005, down_bid=0.98,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
            option_quality_flags=("OPTION_IV_UNAVAILABLE",),
        )
        self.assertEqual(result.option_iv_status, "IV_UNAVAILABLE")
        self.assertEqual(result.model_outcome, "UP")
        self.assertEqual(result.paper_outcome, "UP")
        self.assertTrue(result.paper_entry_eligible)
        self.assertIn("PAPER_ENTRY_REALIZED_VOL_FALLBACK", result.quality_flags)

    def test_valid_option_iv_permits_paper_entry_eligibility(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now, spot=101.0, up_ask=0.01, down_ask=0.99, up_bid=0.005, down_bid=0.98,
            spot_age_seconds=0.2, book_age_seconds=0.1, stream_ready=True, trigger_reasons=("FINNHUB_TRADE",),
            option_iv=0.35, option_iv_provider="TEST", option_iv_age_seconds=1.0,
        )
        self.assertEqual(result.option_iv_status, "IV_VALID")
        self.assertTrue(result.paper_entry_eligible)
        self.assertAlmostEqual(result.model_error_buffer, 0.02)
