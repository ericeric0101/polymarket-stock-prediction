from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.research import (
    OptionQuote,
    ScheduledRiskEvent,
    VolatilityRegime,
    black_scholes_price,
    evaluate_daily_direction,
    implied_volatility,
    risk_gate,
    select_near_atm_option,
)


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)
EXPIRY = NOW + timedelta(days=2)


class ResearchTests(unittest.TestCase):
    def test_implied_volatility_recovers_synthetic_price(self) -> None:
        price = black_scholes_price(100, 100, 0.40, (EXPIRY - NOW).total_seconds(), "call")
        quote = OptionQuote("TEST", "call", 100, price - 0.01, price + 0.01, NOW, EXPIRY)
        self.assertAlmostEqual(implied_volatility(100, quote), 0.40, places=2)

    def test_option_selection_rejects_stale_and_wide_quotes(self) -> None:
        stale = OptionQuote("STALE", "call", 100, 1, 1.1, NOW - timedelta(minutes=20), EXPIRY)
        wide = OptionQuote("WIDE", "call", 100, 1, 2, NOW, EXPIRY)
        liquid = OptionQuote("LIQUID", "put", 101, 1.0, 1.1, NOW, EXPIRY)
        self.assertEqual(select_near_atm_option(100, [stale, wide, liquid], NOW).symbol, "LIQUID")

    def test_event_gate_blocks_earnings_and_halt(self) -> None:
        event = ScheduledRiskEvent("earnings", NOW + timedelta(hours=1), blocking=True)
        passed, reasons = risk_gate(NOW, NOW + timedelta(hours=4), [event], halted=True)
        self.assertFalse(passed)
        self.assertIn("UNDERLYING_HALTED", reasons)
        self.assertIn("BLOCKING_EVENT:EARNINGS", reasons)

    def test_daily_evaluation_does_not_recommend_when_event_blocks(self) -> None:
        option_price = black_scholes_price(101, 100, 0.50, (EXPIRY - NOW).total_seconds(), "call")
        quote = OptionQuote("CALL", "call", 100, option_price - 0.01, option_price + 0.01, NOW, EXPIRY)
        evaluation = evaluate_daily_direction(
            market_id="test", spot=101, prior_close=100, now=NOW, resolves_at=NOW + timedelta(hours=4),
            volatility_regime=VolatilityRegime(overnight_annual=0.60, regular_annual=0.30),
            overnight_seconds=3600, regular_seconds=3 * 3600, option_quotes=[quote],
            up_ask=0.40, down_ask=0.60, fee_rate=0.01, slippage=0.001,
            model_error_buffer=0.02, minimum_edge=0.01,
            events=[ScheduledRiskEvent("CPI", NOW + timedelta(hours=2), blocking=True)], halted=False,
        )
        self.assertFalse(evaluation.risk_passed)
        self.assertIsNone(evaluation.paper_outcome)
