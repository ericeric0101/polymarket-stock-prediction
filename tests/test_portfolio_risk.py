from __future__ import annotations

import unittest

from polymarket_stock.portfolio_risk import PaperEntryCandidate, select_diversified_entries


class PortfolioRiskTests(unittest.TestCase):
    def test_batch_keeps_only_one_mega_cap_and_respects_direction_limit(self) -> None:
        candidates = (
            PaperEntryCandidate("aapl", "AAPL", "UP", 0.40, 0.70, 0.20),
            PaperEntryCandidate("msft", "MSFT", "UP", 0.42, 0.68, 0.18),
            PaperEntryCandidate("nvda", "NVDA", "UP", 0.43, 0.65, 0.15),
            PaperEntryCandidate("abnb", "ABNB", "DOWN", 0.44, 0.65, 0.14),
        )
        decisions = select_diversified_entries(candidates, existing_symbols=(), max_daily_entries=3, max_per_risk_group=1, max_same_direction=2)
        selected = [item.candidate.market_id for item in decisions if item.accepted]
        rejected = {item.candidate.market_id: item.reason for item in decisions if not item.accepted}
        self.assertEqual(selected, ["aapl", "nvda", "abnb"])
        self.assertEqual(rejected["msft"], "CORRELATION_LIMIT")

    def test_existing_entry_consumes_group_budget(self) -> None:
        candidate = PaperEntryCandidate("meta", "META", "DOWN", 0.40, 0.70, 0.20)
        decision = select_diversified_entries((candidate,), existing_symbols=(("AAPL", "UP"),))[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "CORRELATION_LIMIT")
