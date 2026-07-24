from __future__ import annotations

import unittest

from polymarket_stock.option_pricing_validation import (
    OptionPricingInputs,
    black_scholes_merton_price,
    crr_binomial_price,
    implied_volatility_bsm,
    validate_option_quote,
)
from polymarket_stock.pricing import SECONDS_PER_YEAR


class OptionPricingValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.call = OptionPricingInputs(100, 100, 0.20, SECONDS_PER_YEAR, "call", risk_free_rate=0.05)
        self.put = OptionPricingInputs(100, 100, 0.20, SECONDS_PER_YEAR, "put", risk_free_rate=0.05)

    def test_black_scholes_merton_matches_known_atm_prices(self) -> None:
        self.assertAlmostEqual(black_scholes_merton_price(self.call), 10.4506, places=3)
        self.assertAlmostEqual(black_scholes_merton_price(self.put), 5.5735, places=3)

    def test_european_binomial_converges_on_black_scholes_merton(self) -> None:
        bsm = black_scholes_merton_price(self.call)
        binomial = crr_binomial_price(self.call, style="european", steps=800)
        self.assertAlmostEqual(binomial, bsm, places=2)

    def test_implied_volatility_recovers_market_midpoint(self) -> None:
        midpoint = black_scholes_merton_price(self.call)
        self.assertAlmostEqual(implied_volatility_bsm(self.call, midpoint), 0.20, places=4)

    def test_quote_validation_is_explicitly_research_only(self) -> None:
        result = validate_option_quote(self.call, bid=10.40, ask=10.50, style="european")
        self.assertEqual(result.status, "RESEARCH_ONLY_VALIDATED")
        self.assertFalse(result.as_payload()["entry_eligible"])
