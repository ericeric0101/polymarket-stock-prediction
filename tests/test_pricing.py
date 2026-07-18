from __future__ import annotations

import unittest

from polymarket_stock.pricing import digital_up_probability


class PricingTests(unittest.TestCase):
    def test_at_the_money_probability_is_below_half_for_positive_volatility(self) -> None:
        probability = digital_up_probability(100.0, 100.0, 0.20, 24 * 60 * 60)
        self.assertGreater(probability, 0.49)
        self.assertLess(probability, 0.50)

    def test_expired_contract_uses_observed_spot(self) -> None:
        self.assertEqual(digital_up_probability(101.0, 100.0, 0.20, 0), 1.0)
        self.assertEqual(digital_up_probability(100.0, 100.0, 0.20, 0), 0.0)
