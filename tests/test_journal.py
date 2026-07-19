from __future__ import annotations

from pathlib import Path
import sqlite3
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
            listed = journal.list_market_candidates("TSLA")
        self.assertEqual([(item.label, item.token_id) for item in outcomes], [("Up", "up-token"), ("Down", "down-token")])
        self.assertEqual([(item.market_id, item.outcome_a_label, item.outcome_b_label) for item in listed], [("market-1", "Up", "Down")])

    def test_realtime_evaluation_is_persisted_for_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = ShadowJournal(path)
            journal.initialize()
            journal.record_realtime_evaluation(
                {
                    "evaluated_at": "2026-07-20T15:00:00+00:00", "market_id": "market-1", "symbol": "TSLA",
                    "spot": 100.0, "up_ask": 0.50, "down_ask": 0.50, "fair_up_probability": 0.51,
                    "signal_status": "NO_PAPER_TRADE", "skip_reasons": [],
                }
            )
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT market_id, symbol, fair_up_probability, signal_status FROM realtime_evaluations"
                ).fetchone()
        self.assertEqual(row, ("market-1", "TSLA", 0.51, "NO_PAPER_TRADE"))
