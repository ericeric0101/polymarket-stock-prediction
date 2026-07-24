from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.journal import ShadowJournal
from polymarket_stock.maker_shadow import propose_maker_buy_quote


class MakerShadowTests(unittest.TestCase):
    def test_down_fair_value_528_proposes_passive_52_cent_quote(self) -> None:
        proposal = propose_maker_buy_quote(
            outcome="DOWN", fair_probability=0.528, best_bid=0.52, best_ask=0.54, minimum_edge=0.005
        )
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.limit_price, 0.52)
        self.assertAlmostEqual(proposal.theoretical_edge, 0.008)

    def test_quote_is_never_marketable_or_above_fair_edge_target(self) -> None:
        proposal = propose_maker_buy_quote(
            outcome="UP", fair_probability=0.56, best_bid=0.50, best_ask=0.52, minimum_edge=0.005
        )
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.limit_price, 0.51)
        self.assertLess(proposal.limit_price, proposal.best_ask)

    def test_journal_reprice_hysteresis_and_touch_without_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            now = datetime(2026, 7, 20, 14, tzinfo=UTC)
            quote, action = journal.sync_maker_shadow_quote(
                market_id="market-1", symbol="TSLA", outcome="DOWN", limit_price=0.52,
                fair_probability=0.528, theoretical_edge=0.008, best_bid=0.52, best_ask=0.54,
                payload={"source": "test"}, observed_at=now,
            )
            self.assertEqual(action, "OPENED")
            self.assertEqual(quote.limit_price if quote else None, 0.52)
            _, same_action = journal.sync_maker_shadow_quote(
                market_id="market-1", symbol="TSLA", outcome="DOWN", limit_price=0.52,
                fair_probability=0.527, theoretical_edge=0.007, best_bid=0.52, best_ask=0.54,
                payload={"source": "test"}, observed_at=now,
            )
            self.assertIsNone(same_action)
            touched = journal.record_maker_shadow_touch(
                market_id="market-1", outcome="DOWN", current_ask=0.52, observed_at=now
            )
            self.assertEqual(touched.touch_count if touched else None, 1)
            held, held_action = journal.sync_maker_shadow_quote(
                market_id="market-1", symbol="TSLA", outcome="DOWN", limit_price=0.51,
                fair_probability=0.518, theoretical_edge=0.008, best_bid=0.50, best_ask=0.53,
                payload={"source": "test"}, minimum_reprice_price_change=0.02,
                minimum_quote_lifetime_seconds=30, observed_at=now + timedelta(seconds=31),
            )
            self.assertIsNone(held_action)
            self.assertEqual(held.limit_price if held else None, 0.52)
            repriced, reprice_action = journal.sync_maker_shadow_quote(
                market_id="market-1", symbol="TSLA", outcome="DOWN", limit_price=0.50,
                fair_probability=0.508, theoretical_edge=0.008, best_bid=0.49, best_ask=0.52,
                payload={"source": "test"}, minimum_reprice_price_change=0.02,
                minimum_quote_lifetime_seconds=30, observed_at=now + timedelta(seconds=32),
            )
            self.assertEqual(reprice_action, "REPRICED")
            self.assertEqual(repriced.limit_price if repriced else None, 0.50)
            cancelled = journal.list_maker_shadow_quotes("CANCELLED")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].cancel_reason, "REPRICE_LIMIT_CHANGED")

    def test_quote_lifetime_delays_a_large_reprice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            now = datetime(2026, 7, 20, 14, tzinfo=UTC)
            journal.sync_maker_shadow_quote(
                market_id="market-2", symbol="TSLA", outcome="UP", limit_price=0.52,
                fair_probability=0.528, theoretical_edge=0.008, best_bid=0.52, best_ask=0.54,
                payload={"source": "test"}, observed_at=now,
            )
            held, action = journal.sync_maker_shadow_quote(
                market_id="market-2", symbol="TSLA", outcome="UP", limit_price=0.50,
                fair_probability=0.508, theoretical_edge=0.008, best_bid=0.49, best_ask=0.52,
                payload={"source": "test"}, minimum_reprice_price_change=0.02,
                minimum_quote_lifetime_seconds=30, observed_at=now + timedelta(seconds=1),
            )
        self.assertIsNone(action)
        self.assertEqual(held.limit_price if held else None, 0.52)
