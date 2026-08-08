from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.evaluation_payload import PAYLOAD_VERSION
from polymarket_stock.cross_market import cross_market_diagnostics, research_dashboard_state
from polymarket_stock.journal import ShadowJournal
from polymarket_stock.market_discovery import MarketCandidate
from polymarket_stock.price_ladder import PriceLadderContract
from polymarket_stock.price_ladder_journal import PriceLadderJournal
from polymarket_stock.research_web import HTML, ResearchDashboardServer


def contract(strike: float) -> PriceLadderContract:
    return PriceLadderContract(
        market_id=f"ladder-{strike}",
        event_id="event",
        event_slug="event",
        symbol="TSLA",
        strike=strike,
        market_date="2026-08-03",
        resolves_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
        pyth_feed="Equity.US.TSLA/USD",
        yes_token_id=f"yes-{strike}",
        no_token_id=f"no-{strike}",
        question="question",
        rules_hash="hash",
        raw_payload={},
    )


class CrossMarketResearchTests(unittest.TestCase):
    def test_weekend_state_exposes_next_contract_live_book_without_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            core = ShadowJournal(path)
            core.initialize()
            candidate = MarketCandidate.from_gamma_payload(
                {
                    "id": "monday",
                    "question": "Tesla (TSLA) Up or Down on August 3?",
                    "slug": "tsla-up-down",
                    "description": "Pyth daily close",
                    "resolutionSource": "https://pyth.network/price-feeds/Equity.US.TSLA%2FUSD",
                    "endDate": "2026-08-03T20:00:00+00:00",
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": ["up-token", "down-token"],
                }
            )
            core.upsert_market_candidate(candidate)
            core.record_realtime_evaluation(
                {
                    "payload_version": PAYLOAD_VERSION,
                    "price_to_beat_distance_bps": None,
                    "market_up_probability": None,
                    "market_model_divergence": None,
                    "model_majority_outcome": None,
                    "entry_diagnostic_flags": [],
                    "entry_policy_category": "NO_EDGE",
                    "evaluated_at": "2026-08-01T16:00:00+00:00",
                    "market_id": "monday",
                    "symbol": "TSLA",
                    "spot": None,
                    "price_to_beat": 310,
                    "up_bid": 0.54,
                    "up_ask": 0.56,
                    "down_bid": 0.44,
                    "down_ask": 0.46,
                    "fair_up_probability": None,
                    "up_book": {"best_bid_size": 20, "best_ask_size": 15},
                    "down_book": {"best_bid_size": 30, "best_ask_size": 25},
                    "book_age_seconds": 1,
                    "market_session": "WEEKEND",
                    "signal_status": "NO_PAPER_TRADE",
                    "skip_reasons": ["NON_REGULAR_SESSION:WEEKEND"],
                }
            )
            state = research_dashboard_state(
                path,
                now=datetime(2026, 8, 1, 16, 0, 5, tzinfo=UTC),
            )
        self.assertEqual(state["market_date"], "2026-08-03")
        self.assertFalse(state["market_status"]["decision_enabled"])
        self.assertEqual(len(state["live_markets"]), 1)
        self.assertAlmostEqual(state["live_markets"][0]["market_up_probability"], 0.55)
        self.assertEqual(state["live_markets"][0]["state"], "LIVE")
        self.assertIsNone(state["live_markets"][0]["model_up_probability"])
        self.assertEqual(state["live_markets"][0]["entry_policy_category"], "NO_EDGE")

    def test_checkpoint_join_produces_read_only_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            core = ShadowJournal(path)
            core.initialize()
            core.record_checkpoint_observation(
                checkpoint_date="2026-08-03",
                checkpoint_name="1200_EDT",
                payload={
                    "payload_version": PAYLOAD_VERSION,
                    "price_to_beat_distance_bps": None,
                    "market_up_probability": None,
                    "market_model_divergence": None,
                    "model_majority_outcome": None,
                    "entry_diagnostic_flags": [],
                    "entry_policy_category": "NO_EDGE",
                    "evaluated_at": "2026-08-03T16:00:00+00:00",
                    "market_id": "updown",
                    "symbol": "TSLA",
                    "price_to_beat": 310,
                    "fair_up_probability": 0.54,
                    "up_bid": 0.50,
                    "up_ask": 0.54,
                    "down_bid": 0.46,
                    "down_ask": 0.50,
                    "model_version": "test",
                },
            )
            core.record_realtime_evaluation(
                {
                    "payload_version": PAYLOAD_VERSION,
                    "price_to_beat_distance_bps": None,
                    "market_up_probability": None,
                    "market_model_divergence": None,
                    "model_majority_outcome": None,
                    "entry_diagnostic_flags": [],
                    "entry_policy_category": "NO_EDGE",
                    "evaluated_at": "2026-08-03T15:45:00+00:00",
                    "market_id": "signal-market",
                    "symbol": "TSLA",
                    "spot": 310,
                    "up_ask": 0.45,
                    "down_ask": 0.57,
                    "fair_up_probability": 0.62,
                    "model_outcome": "UP",
                    "signal_status": "PAPER_UP",
                    "skip_reasons": [],
                    "model_version": "test",
                }
            )
            core.record_market_settlement("signal-market", "UP", {"closed": True})
            settled_position, _ = core.open_paper_position(
                market_id="paper-1",
                symbol="TSLA",
                outcome="UP",
                entry_ask=0.45,
                fair_probability=0.62,
                model_version="test",
                payload={},
                contracts=10,
                fee_rate=0,
                opened_at=datetime(2026, 8, 3, 16, 10, tzinfo=UTC),
            )
            core.settle_paper_position(
                settled_position.position_id,
                settlement_outcome="UP",
                settlement_payload={"closed": True},
                settled_at=datetime(2026, 8, 3, 20, 5, tzinfo=UTC),
            )
            core.open_paper_position(
                market_id="paper-2",
                symbol="NVDA",
                outcome="DOWN",
                entry_ask=0.55,
                fair_probability=0.64,
                model_version="test",
                payload={},
                contracts=5,
                fee_rate=0,
                opened_at=datetime(2026, 8, 3, 16, 20, tzinfo=UTC),
            )
            ladder = PriceLadderJournal(path)
            ladder.initialize()
            for strike, probability in ((290, 0.80), (310, 0.50), (330, 0.20)):
                item = contract(strike)
                ladder.upsert_contract(item)
                ladder.record_snapshot(
                    item,
                    observed_at=datetime(2026, 8, 3, 16, tzinfo=UTC),
                    checkpoint_name="1200_EDT",
                    yes_bid=probability - 0.02,
                    yes_ask=probability + 0.02,
                    no_bid=1 - probability - 0.02,
                    no_ask=1 - probability + 0.02,
                    yes_bid_depth=100,
                    yes_ask_depth=100,
                    no_bid_depth=100,
                    no_ask_depth=100,
                    yes_book={},
                    no_book={},
                )
            diagnostics = cross_market_diagnostics(path, market_date="2026-08-03")
            state = research_dashboard_state(path, now=datetime(2026, 8, 3, 17, tzinfo=UTC))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].status, "CONFIRM")
        self.assertEqual(state["isolation"]["affects_entries"], False)
        self.assertEqual(len(state["ladder_curves"]), 1)
        self.assertEqual(state["cross_market_readiness"]["status"], "READY")
        self.assertEqual(len(state["ladder_candidates"]), 3)
        self.assertTrue(
            all(item["fee_status"] == "NOT_CAPTURED_SUBTRACT_CURRENT_TAKER_FEE" for item in state["ladder_candidates"])
        )
        portfolio = state["paper_portfolio"]
        self.assertEqual(portfolio["selected_count"], 2)
        self.assertEqual(portfolio["settled_count"], 1)
        self.assertEqual((portfolio["wins"], portfolio["losses"]), (1, 0))
        self.assertEqual(portfolio["daily_entry_limit"], 5)
        self.assertEqual(portfolio["first_signal_performance"]["wins"], 1)
        self.assertEqual([entry["status"] for entry in portfolio["entries"]], ["UP WIN", "OPEN"])
        self.assertAlmostEqual(portfolio["entries"][0]["realized_pnl"], 5.5)
        self.assertFalse(portfolio["sizing"]["kelly_enabled"])

    def test_web_dashboard_is_localhost_only_and_exposes_separate_views(self) -> None:
        with self.assertRaises(ValueError):
            ResearchDashboardServer(Path("journal.db"), host="0.0.0.0")
        with self.assertRaises(ValueError):
            ResearchDashboardServer(Path("journal.db"), daily_entry_limit=0)
        self.assertIn("Trade Today", HTML)
        self.assertIn("Core Up/Down", HTML)
        self.assertIn("Price Distribution", HTML)
        self.assertIn("Cross-Market", HTML)
        self.assertIn("Never changes entries or sizing", HTML)
        self.assertIn("Live Above-X ladder research candidates", HTML)
        self.assertIn("cross-readiness", HTML)
        self.assertIn("gross edge excludes the current taker fee", HTML)
        self.assertIn("Top Recommendations", HTML)
        self.assertIn("Daily Paper Portfolio", HTML)
        self.assertIn("portfolio-summary", HTML)
        self.assertIn("Trade Today · Core Up/Down", HTML)
        self.assertIn("OBSERVATION ONLY", HTML)
        self.assertIn("research flags do not block entries", HTML)
        self.assertIn("entry_diagnostic_flags", HTML)
        self.assertIn("CONTRARIAN_VALUE", HTML)
        self.assertIn("Asia/Taipei", HTML)
        self.assertIn("America/New_York", HTML)
        self.assertIn("taipei-time", HTML)
        self.assertIn("new-york-time", HTML)


if __name__ == "__main__":
    unittest.main()
