from __future__ import annotations

from datetime import UTC, datetime
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

    def test_bounded_spot_observations_and_comparisons_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = ShadowJournal(path)
            journal.initialize()
            observed_at = "2026-07-27T14:00:00.500000+00:00"
            journal.record_spot_observation({
                "observed_at": observed_at, "source": "PYTH_HERMES", "symbol": "TSLA", "price": 100.25,
                "published_at": "2026-07-27T14:00:00+00:00", "confidence": 0.02, "feed_id": "feed",
            })
            journal.record_spot_observation({
                "observed_at": "2026-07-27T14:00:00.900000+00:00", "source": "PYTH_HERMES", "symbol": "TSLA", "price": 100.30,
            })
            journal.record_spot_source_comparison({
                "observed_at": observed_at, "symbol": "TSLA", "primary_source": "FINNHUB", "primary_price": 100.0,
                "pyth_price": 100.25, "difference_bps": -24.9376558603, "pyth_feed_id": "feed",
            })
            with sqlite3.connect(path) as connection:
                spot_count = connection.execute("SELECT COUNT(*) FROM spot_observations").fetchone()[0]
                comparison = connection.execute("SELECT primary_source, difference_bps FROM spot_source_comparisons").fetchone()
        self.assertEqual(spot_count, 1)
        self.assertEqual(comparison[0], "FINNHUB")
        self.assertAlmostEqual(comparison[1], -24.9376558603)

    def test_contract_review_is_upserted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            journal = ShadowJournal(path)
            journal.initialize()
            journal.record_contract_review(
                "market-1", accepted=True, reason="PYTH_DAILY_CLOSE_TEMPLATE", contract={"symbol": "TSLA"}
            )
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT status, reason, contract_json FROM market_contract_reviews WHERE market_id = 'market-1'"
                ).fetchone()
        self.assertEqual(row, ("ACCEPTED", "PYTH_DAILY_CLOSE_TEMPLATE", '{"symbol":"TSLA"}'))

    def test_settled_market_replay_observation_uses_latest_valid_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            for minute, probability in ((5, 0.51), (6, 0.62)):
                journal.record_realtime_evaluation({
                    "evaluated_at": f"2026-07-20T15:0{minute}:00+00:00", "market_id": "market-1", "symbol": "TSLA",
                    "spot": 100, "up_ask": 0.50, "down_ask": 0.50, "fair_up_probability": probability,
                    "signal_status": "NO_PAPER_TRADE", "skip_reasons": [],
                })
            journal.record_market_settlement("market-1", "UP", {"closed": True})
            observations = journal.list_replay_observations()
        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].fair_up_probability, 0.62)

    def test_first_signal_performance_uses_only_first_settled_signal_per_market(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            for market_id, prediction, outcome in (("market-1", "UP", "UP"), ("market-2", "DOWN", "UP")):
                journal.record_realtime_evaluation({
                    "evaluated_at": "2026-07-20T15:00:00+00:00", "market_id": market_id, "symbol": "TSLA",
                    "spot": 100.0, "up_ask": 0.5, "down_ask": 0.5, "fair_up_probability": 0.6,
                    "model_outcome": prediction, "signal_status": f"PAPER_{prediction}", "skip_reasons": [],
                })
                journal.record_market_settlement(market_id, outcome, {"closed": True})
            performance = journal.first_signal_performance()
        self.assertEqual(performance["settled_markets"], 2)
        self.assertEqual(performance["wins"], 1)
        self.assertEqual(performance["losses"], 1)
        self.assertAlmostEqual(performance["win_rate"] or 0, 0.5)

    def test_first_signal_calibration_observations_use_selected_side_probability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            journal.record_realtime_evaluation({
                "evaluated_at": "2026-07-20T15:00:00+00:00", "market_id": "market-1", "symbol": "TSLA",
                "spot": 99.0, "prior_close": 100.0, "up_ask": 0.2, "down_ask": 0.8,
                "fair_up_probability": 0.25, "model_outcome": "DOWN", "signal_status": "PAPER_DOWN",
                "skip_reasons": [], "option_iv_status": "IV_UNAVAILABLE", "spot_provider": "PYTH_HERMES",
                "model_version": "test-v1", "down_taker_fee": 0.01,
            })
            journal.record_realtime_evaluation({
                "evaluated_at": "2026-07-20T15:01:00+00:00", "market_id": "market-1", "symbol": "TSLA",
                "spot": 99.0, "prior_close": 100.0, "up_ask": 0.2, "down_ask": 0.8,
                "fair_up_probability": 0.10, "model_outcome": "DOWN", "signal_status": "PAPER_DOWN",
                "skip_reasons": [],
            })
            journal.record_market_settlement("market-1", "DOWN", {"closed": True})
            observation = journal.list_first_signal_calibration_observations()[0]
        self.assertEqual(observation.model_outcome, "DOWN")
        self.assertAlmostEqual(observation.selected_fair_probability, 0.75)
        self.assertAlmostEqual(observation.entry_ask, 0.8)
        self.assertAlmostEqual(observation.entry_fee or 0, 0.01)
        self.assertEqual(observation.iv_regime, "REALIZED_VOL_FALLBACK")
        self.assertAlmostEqual(observation.threshold_distance_bps or 0, -100.0)

    def test_paper_position_is_idempotent_and_settles_at_official_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            position, created = journal.open_paper_position(
                market_id="market-1", symbol="TSLA", outcome="DOWN", entry_ask=0.49,
                fair_probability=0.55, model_version="test-v1", payload={"source": "test"}, fee_rate=0.04,
            )
            duplicate, duplicate_created = journal.open_paper_position(
                market_id="market-1", symbol="TSLA", outcome="DOWN", entry_ask=0.48,
                fair_probability=0.56, model_version="test-v1", payload={"source": "duplicate"}, fee_rate=0.04,
            )
            opposite, opposite_created = journal.open_paper_position(
                market_id="market-1", symbol="TSLA", outcome="UP", entry_ask=0.48,
                fair_probability=0.56, model_version="test-v1", payload={"source": "opposite"}, fee_rate=0.04,
            )
            settled = journal.settle_paper_position(
                position.position_id, settlement_outcome="DOWN", settlement_payload={"closed": True},
            )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.position_id, position.position_id)
        self.assertFalse(opposite_created)
        self.assertEqual(opposite.position_id, position.position_id)
        self.assertEqual(settled.status, "SETTLED")
        self.assertAlmostEqual(settled.payout or 0, 1.0)
        self.assertAlmostEqual(position.entry_fee, 0.01)
        self.assertAlmostEqual(position.entry_slippage, 0)
        self.assertAlmostEqual(settled.realized_pnl or 0, 1 - (0.49 + 0.01))

    def test_precontract_day_paper_position_is_preserved_but_excluded(self) -> None:
        candidate = MarketCandidate.from_gamma_payload(
            {
                "id": "next-day", "question": "Tesla (TSLA) Up or Down on July 21?", "slug": "tsla-next-day",
                "description": "Pyth close terms", "resolutionSource": "https://pyth.example",
                "endDate": "2026-07-21T20:00:00Z", "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            journal.upsert_market_candidate(candidate)
            journal.open_paper_position(
                market_id="next-day", symbol="TSLA", outcome="UP", entry_ask=0.50,
                fair_probability=0.60, model_version="test", payload={"source": "test"}, fee_rate=0.04,
                opened_at=datetime(2026, 7, 20, 19, 55, tzinfo=UTC),
            )
            journal.initialize()
            position = journal.list_paper_positions("OPEN")[0]
        self.assertFalse(position.included_in_calibration)
        self.assertEqual(position.exclusion_reason, "PRECONTRACT_TRADE_DATE")

    def test_portfolio_decision_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            journal.record_portfolio_decision(
                batch_id="batch-1", market_id="market-1", symbol="TSLA", outcome="UP", risk_group="EV_AUTO",
                edge=0.05, selected=False, reason="CORRELATION_LIMIT", payload={"source": "test"},
            )
            decision = journal.list_portfolio_decisions()[0]
        self.assertEqual(decision["reason"], "CORRELATION_LIMIT")
        self.assertEqual(decision["status"], "REJECTED")
