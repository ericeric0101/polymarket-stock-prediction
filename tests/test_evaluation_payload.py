from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.evaluation_payload import (
    CURRENT_REQUIRED_KEYS,
    PAYLOAD_VERSION,
    read_entry_diagnostic_flags,
    read_entry_policy_category,
    read_model_outcome,
    read_threshold,
    validate_for_read,
    validate_for_write,
)
from polymarket_stock.realtime import RealtimeEvaluation


def make_evaluation() -> RealtimeEvaluation:
    return RealtimeEvaluation(
        evaluated_at=datetime(2026, 8, 3, 16, tzinfo=UTC),
        market_id="m",
        symbol="TSLA",
        spot_provider="FINNHUB",
        spot=100.0,
        reference_spot=None,
        reference_spot_age_seconds=None,
        cross_source_difference=None,
        option_iv=None,
        option_skew=None,
        option_iv_provider=None,
        option_iv_age_seconds=None,
        option_iv_status="IV_UNAVAILABLE",
        up_ask=0.5,
        down_ask=0.5,
        up_bid=0.49,
        down_bid=0.49,
        up_fee_rate=None,
        down_fee_rate=None,
        up_taker_fee=None,
        down_taker_fee=None,
        spot_age_seconds=1.0,
        book_age_seconds=1.0,
        stream_ready=True,
        market_session="REGULAR",
        daily_data_is_fresh=True,
        fair_up_probability=0.5,
        annualized_realized_volatility=0.2,
        volatility_estimator="EWMA",
        comparison_models=(),
        prior_close=99.0,
        model_error_buffer=0.02,
        minimum_edge=0.02,
        up_edge=0.0,
        down_edge=0.0,
        model_outcome="UP",
        paper_outcome="UP",
        paper_entry_eligible=True,
        paper_entry_block_reasons=(),
        price_to_beat_distance_bps=None,
        market_up_probability=0.5,
        market_model_divergence=0.0,
        model_majority_outcome="UP",
        entry_diagnostic_flags=(),
        entry_policy_category="MODEL_ALIGNED",
        quality_flags=(),
        trigger_reasons=(),
        skip_reasons=(),
    )


class EvaluationPayloadTests(unittest.TestCase):
    def test_payload_field_set_is_locked(self) -> None:
        payload = make_evaluation().as_payload()
        self.assertEqual(payload["payload_version"], PAYLOAD_VERSION)
        self.assertTrue(CURRENT_REQUIRED_KEYS.issubset(payload))
        validate_for_write(payload)

    def test_new_writes_cannot_use_legacy_escape_hatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "payload_version"):
            validate_for_write({"evaluated_at": "2026-08-03T16:00:00+00:00"})

    def test_legacy_payload_remains_readable(self) -> None:
        validate_for_read(
            {
                "evaluated_at": "2026-08-03T16:00:00+00:00",
                "market_id": "m",
                "symbol": "TSLA",
                "signal_status": "NO_PAPER_TRADE",
                "skip_reasons": [],
            }
        )

    def test_v1_payload_remains_readable_without_v2_diagnostics(self) -> None:
        validate_for_read(
            {
                "payload_version": 1,
                "evaluated_at": "2026-08-03T16:00:00+00:00",
                "market_id": "m",
                "symbol": "TSLA",
                "signal_status": "NO_PAPER_TRADE",
                "skip_reasons": [],
                "spot": 100.0,
                "fair_up_probability": 0.5,
                "up_ask": 0.5,
                "down_ask": 0.5,
            }
        )

    def test_v2_accessors_tolerate_historical_missing_diagnostic_fields(self) -> None:
        self.assertIsNone(read_model_outcome({"payload_version": 1}))
        self.assertEqual(read_entry_diagnostic_flags({"payload_version": 1}), ())

    def test_v2_payload_remains_readable_without_policy_category(self) -> None:
        payload = make_evaluation().as_payload()
        payload["payload_version"] = 2
        payload.pop("entry_policy_category")
        validate_for_read(payload)
        self.assertEqual(read_entry_policy_category(payload), "MODEL_ALIGNED")

    def test_threshold_preserves_zero_instead_of_falling_back(self) -> None:
        self.assertEqual(read_threshold({"price_to_beat": 0.0, "prior_close": 99.0}), 0.0)


if __name__ == "__main__":
    unittest.main()
