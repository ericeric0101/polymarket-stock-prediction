from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from polymarket_stock.journal import ShadowJournal
from polymarket_stock.market_discovery import MarketCandidate


class JournalTests(unittest.TestCase):
    def test_stored_outcome_tokens_are_retrieved_by_market_id(self) -> None:
        candidate = MarketCandidate.from_gamma_payload(
            {
                "id": "market-1",
                "question": "Tesla (TSLA) Up or Down on July 20?",
                "slug": "tsla-updown",
                "description": "Pyth close terms",
                "resolutionSource": "https://pyth.example",
                "endDate": "2026-07-20T20:00:00Z",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            journal.upsert_market_candidate(candidate)
            outcomes = journal.get_market_outcome_tokens("market-1")
        self.assertEqual([(item.label, item.token_id) for item in outcomes], [("Up", "up-token"), ("Down", "down-token")])
