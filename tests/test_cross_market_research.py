from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.cross_market import cross_market_diagnostics, research_dashboard_state
from polymarket_stock.journal import ShadowJournal
from polymarket_stock.price_ladder import PriceLadderContract
from polymarket_stock.price_ladder_journal import PriceLadderJournal
from polymarket_stock.research_web import HTML, ResearchDashboardServer


def contract(strike: float) -> PriceLadderContract:
    return PriceLadderContract(
        market_id=f"ladder-{strike}", event_id="event", event_slug="event", symbol="TSLA", strike=strike,
        market_date="2026-08-03", resolves_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
        pyth_feed="Equity.US.TSLA/USD", yes_token_id=f"yes-{strike}", no_token_id=f"no-{strike}",
        question="question", rules_hash="hash", raw_payload={},
    )


class CrossMarketResearchTests(unittest.TestCase):
    def test_checkpoint_join_produces_read_only_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            core = ShadowJournal(path)
            core.initialize()
            core.record_checkpoint_observation(
                checkpoint_date="2026-08-03", checkpoint_name="1200_EDT",
                payload={
                    "evaluated_at": "2026-08-03T16:00:00+00:00", "market_id": "updown",
                    "symbol": "TSLA", "price_to_beat": 310, "fair_up_probability": 0.54,
                    "up_bid": 0.50, "up_ask": 0.54, "down_bid": 0.46, "down_ask": 0.50,
                    "model_version": "test",
                },
            )
            ladder = PriceLadderJournal(path)
            ladder.initialize()
            for strike, probability in ((290, 0.80), (310, 0.50), (330, 0.20)):
                item = contract(strike)
                ladder.upsert_contract(item)
                ladder.record_snapshot(
                    item, observed_at=datetime(2026, 8, 3, 16, tzinfo=UTC), checkpoint_name="1200_EDT",
                    yes_bid=probability - 0.02, yes_ask=probability + 0.02,
                    no_bid=1 - probability - 0.02, no_ask=1 - probability + 0.02,
                    yes_bid_depth=100, yes_ask_depth=100, no_bid_depth=100, no_ask_depth=100,
                    yes_book={}, no_book={},
                )
            diagnostics = cross_market_diagnostics(path, market_date="2026-08-03")
            state = research_dashboard_state(path, now=datetime(2026, 8, 3, 17, tzinfo=UTC))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].status, "CONFIRM")
        self.assertEqual(state["isolation"]["affects_entries"], False)
        self.assertEqual(len(state["ladder_curves"]), 1)

    def test_web_dashboard_is_localhost_only_and_exposes_separate_views(self) -> None:
        with self.assertRaises(ValueError):
            ResearchDashboardServer(Path("journal.db"), host="0.0.0.0")
        self.assertIn("Core Up/Down", HTML)
        self.assertIn("Price Distribution", HTML)
        self.assertIn("Cross-Market", HTML)
        self.assertIn("Never changes entries or sizing", HTML)
        self.assertIn("Asia/Taipei", HTML)
        self.assertIn("America/New_York", HTML)
        self.assertIn("taipei-time", HTML)
        self.assertIn("new-york-time", HTML)


if __name__ == "__main__":
    unittest.main()
