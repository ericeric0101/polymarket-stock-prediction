from __future__ import annotations

import unittest

from polymarket_stock.edge import assess_buy_edge


class EdgeTests(unittest.TestCase):
    def test_yes_edge_uses_conservative_probability_and_costs(self) -> None:
        assessment = assess_buy_edge(
            fair_yes_probability=0.70,
            outcome="YES",
            executable_ask=0.60,
            fee_rate=0.02,
            slippage=0.01,
            model_error_buffer=0.03,
            minimum_edge=0.01,
        )
        self.assertAlmostEqual(assessment.conservative_fair_probability, 0.67)
        self.assertAlmostEqual(assessment.estimated_cost, 0.022)
        self.assertAlmostEqual(assessment.edge, 0.048)
        self.assertTrue(assessment.should_record_paper_trade)

    def test_no_edge_inverts_yes_probability(self) -> None:
        assessment = assess_buy_edge(
            fair_yes_probability=0.30,
            outcome="NO",
            executable_ask=0.60,
            fee_rate=0.00,
            slippage=0.00,
            model_error_buffer=0.05,
            minimum_edge=0.05,
        )
        self.assertAlmostEqual(assessment.conservative_fair_probability, 0.65)
        self.assertAlmostEqual(assessment.edge, 0.05)
        self.assertTrue(assessment.should_record_paper_trade)
