from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch

from polymarket_stock.baseline import DailyClose
from polymarket_stock.realtime import (
    RealtimeBaselineEvaluator,
    classify_entry_policy,
    entry_research_diagnostics,
    executable_market_up_probability,
    volatility_models_disagree,
)


class RealtimeBaselineEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 20, 15, tzinfo=UTC)
        self.closes = [
            DailyClose((self.now.date() - timedelta(days=day)).isoformat(), 100 + day) for day in range(30, -1, -1)
        ]
        self.evaluator = RealtimeBaselineEvaluator(
            market_id="market-1",
            symbol="TSLA",
            resolves_at=self.now + timedelta(hours=5),
            closes=self.closes,
            spot_provider="FINNHUB",
            up_fee_rate=0.04,
            down_fee_rate=0.04,
        )

    def test_fresh_state_produces_shadow_evaluation(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            up_ask=0.50,
            down_ask=0.50,
            up_bid=0.49,
            down_bid=0.49,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNotNone(result.fair_up_probability)
        self.assertEqual(result.skip_reasons, ())
        self.assertIn(result.as_payload()["signal_status"], {"NO_PAPER_TRADE", "PAPER_UP", "PAPER_DOWN"})
        self.assertEqual(result.paper_outcome, result.model_outcome)
        self.assertEqual(result.paper_entry_eligible, result.model_outcome is not None)
        self.assertAlmostEqual(result.model_error_buffer, 0.02)
        self.assertAlmostEqual(result.minimum_edge, 0.02)
        self.assertAlmostEqual(result.as_payload()["minimum_edge"], 0.02)
        self.assertEqual(result.entry_policy_category, "MODEL_ALIGNED")

    def test_entry_risk_diagnostics_flag_abnb_and_msft_patterns_without_gating(self) -> None:
        market_probability = executable_market_up_probability(
            up_bid=0.88,
            up_ask=0.91,
            down_bid=0.07,
            down_ask=0.09,
        )
        distance, divergence, majority, flags = entry_research_diagnostics(
            spot=150.95,
            price_to_beat=150.96982,
            fair_up_probability=0.4912,
            market_up_probability=market_probability,
            selected_outcome="DOWN",
        )
        self.assertLess(distance or 0.0, 100.0)
        self.assertLess(divergence or 0.0, -0.25)
        self.assertEqual(majority, "DOWN")
        self.assertEqual(flags, ("NEAR_THRESHOLD_HIGH_RISK", "UNCERTAIN_CONTRARIAN_ENTRY"))

        _, _, majority, flags = entry_research_diagnostics(
            spot=100.698,
            price_to_beat=100.0,
            fair_up_probability=0.2834,
            market_up_probability=0.14,
            selected_outcome="UP",
        )
        self.assertEqual(majority, "DOWN")
        self.assertIn("CONTRADICTORY_SIDE_ENTRY", flags)
        self.assertNotIn("UNCERTAIN_CONTRARIAN_ENTRY", flags)

    def test_entry_risk_diagnostics_do_not_change_paper_eligibility(self) -> None:
        evaluator = RealtimeBaselineEvaluator(
            market_id="market-1",
            symbol="TSLA",
            resolves_at=self.now + timedelta(hours=5),
            closes=self.closes,
            spot_provider="FINNHUB",
            up_fee_rate=0.04,
            down_fee_rate=0.04,
            price_to_beat=100.0,
        )
        result = evaluator.evaluate(
            now=self.now,
            spot=100.01,
            up_ask=0.91,
            down_ask=0.09,
            up_bid=0.88,
            down_bid=0.07,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
            option_iv=0.25,
            option_iv_provider="TEST",
            option_iv_age_seconds=1.0,
        )
        self.assertEqual(result.model_outcome, "DOWN")
        self.assertTrue(result.paper_entry_eligible)
        self.assertEqual(result.paper_entry_block_reasons, ())
        self.assertIn("NEAR_THRESHOLD_HIGH_RISK", result.entry_diagnostic_flags)
        self.assertIn("UNCERTAIN_CONTRARIAN_ENTRY", result.entry_diagnostic_flags)

    def test_entry_policy_category_is_diagnostic_only(self) -> None:
        self.assertEqual(classify_entry_policy("UP", "UP"), "MODEL_ALIGNED")
        self.assertEqual(classify_entry_policy("UP", "DOWN"), "CONTRARIAN_VALUE")
        self.assertEqual(classify_entry_policy(None, "UP"), "NO_EDGE")
        self.assertEqual(classify_entry_policy("UP", None), "UNKNOWN")

    def test_volatility_disagreement_detects_direction_and_large_probability_gap(self) -> None:
        self.assertTrue(volatility_models_disagree(0.70, ({"fair_up_probability": 0.49},)))
        self.assertTrue(volatility_models_disagree(0.70, ({"fair_up_probability": 0.59},)))
        self.assertFalse(volatility_models_disagree(0.70, ({"fair_up_probability": 0.65},)))

    def test_dual_mode_records_comparison_without_second_paper_outcome(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            up_ask=0.50,
            down_ask=0.50,
            up_bid=0.49,
            down_bid=0.49,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
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
            now=self.now,
            spot=101.0,
            up_ask=None,
            down_ask=None,
            spot_age_seconds=20.0,
            book_age_seconds=None,
            stream_ready=False,
            trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIsNone(result.fair_up_probability)
        self.assertIn("STALE_OR_INCOMPLETE_STREAM", result.skip_reasons)
        self.assertIn("MISSING_EXECUTABLE_ASK", result.skip_reasons)

    def test_non_regular_session_and_crossed_book_are_blocked(self) -> None:
        result = self.evaluator.evaluate(
            now=datetime(2026, 7, 19, 15, tzinfo=UTC),
            spot=101.0,
            up_ask=0.50,
            down_ask=0.50,
            up_bid=0.51,
            down_bid=0.49,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("BOOK",),
        )
        self.assertIn("NON_REGULAR_SESSION:WEEKEND", result.skip_reasons)
        self.assertIn("CROSSED_UP_BOOK", result.skip_reasons)

    def test_fresh_cross_source_divergence_is_blocked(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            reference_spot=100.0,
            reference_spot_age_seconds=1.0,
            up_ask=0.50,
            down_ask=0.50,
            up_bid=0.49,
            down_bid=0.49,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
        )
        self.assertIn("CROSS_SOURCE_SPOT_DIVERGENCE", result.skip_reasons)
        self.assertAlmostEqual(result.cross_source_difference or 0, 1 / 101)

    def test_entry_risk_gate_preserves_model_signal(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            up_ask=0.01,
            down_ask=0.99,
            up_bid=0.005,
            down_bid=0.98,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
            risk_reasons=("EVENT_CALENDAR_UNAVAILABLE",),
        )
        self.assertIsNotNone(result.fair_up_probability)
        self.assertEqual(result.model_outcome, "UP")
        self.assertIsNone(result.paper_outcome)
        self.assertFalse(result.paper_entry_eligible)
        self.assertIn("RISK_GATE:EVENT_CALENDAR_UNAVAILABLE", result.skip_reasons)
        self.assertIn("RISK_GATE:EVENT_CALENDAR_UNAVAILABLE", result.paper_entry_block_reasons)

    def test_unavailable_option_iv_uses_labeled_realized_volatility_fallback(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            up_ask=0.01,
            down_ask=0.99,
            up_bid=0.005,
            down_bid=0.98,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
            option_quality_flags=("OPTION_IV_UNAVAILABLE",),
        )
        self.assertEqual(result.option_iv_status, "IV_UNAVAILABLE")
        self.assertEqual(result.model_outcome, "UP")
        self.assertEqual(result.paper_outcome, "UP")
        self.assertTrue(result.paper_entry_eligible)
        self.assertIn("PAPER_ENTRY_REALIZED_VOL_FALLBACK", result.quality_flags)

    def test_fallback_volatility_disagreement_blocks_entry_but_preserves_model_signal(self) -> None:
        with patch("polymarket_stock.realtime.volatility_models_disagree", return_value=True):
            result = self.evaluator.evaluate(
                now=self.now,
                spot=101.0,
                up_ask=0.01,
                down_ask=0.99,
                up_bid=0.005,
                down_bid=0.98,
                spot_age_seconds=0.2,
                book_age_seconds=0.1,
                stream_ready=True,
                trigger_reasons=("FINNHUB_TRADE",),
                option_quality_flags=("OPTION_IV_UNAVAILABLE",),
            )
        self.assertEqual(result.model_outcome, "UP")
        self.assertIsNone(result.paper_outcome)
        self.assertFalse(result.paper_entry_eligible)
        self.assertIn("VOLATILITY_MODEL_DISAGREEMENT", result.paper_entry_block_reasons)

    def test_valid_option_iv_permits_paper_entry_eligibility(self) -> None:
        result = self.evaluator.evaluate(
            now=self.now,
            spot=101.0,
            up_ask=0.01,
            down_ask=0.99,
            up_bid=0.005,
            down_bid=0.98,
            spot_age_seconds=0.2,
            book_age_seconds=0.1,
            stream_ready=True,
            trigger_reasons=("FINNHUB_TRADE",),
            option_iv=0.35,
            option_iv_provider="TEST",
            option_iv_age_seconds=1.0,
        )
        self.assertEqual(result.option_iv_status, "IV_VALID")
        self.assertTrue(result.paper_entry_eligible)
        self.assertAlmostEqual(result.model_error_buffer, 0.02)
