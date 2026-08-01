from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.price_ladder import PriceLadderContract
from polymarket_stock.price_ladder_journal import PriceLadderJournal


def contract(market_id: str = "ladder-310", strike: float = 310) -> PriceLadderContract:
    return PriceLadderContract(
        market_id=market_id, event_id="event", event_slug="event-slug", symbol="TSLA", strike=strike,
        market_date="2026-08-03", resolves_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
        pyth_feed="Equity.US.TSLA/USD", yes_token_id=f"yes-{strike}", no_token_id=f"no-{strike}",
        question=f"TSLA close above ${strike}?", rules_hash="hash", raw_payload={"id": market_id},
    )


class PriceLadderJournalTests(unittest.TestCase):
    def test_isolated_contract_snapshot_and_settlement_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = PriceLadderJournal(Path(directory) / "journal.db")
            journal.initialize()
            item = contract()
            journal.upsert_contract(item)
            observed_at = datetime(2026, 8, 3, 16, tzinfo=UTC)
            arguments = dict(
                observed_at=observed_at, checkpoint_name="1200_EDT", yes_bid=0.48, yes_ask=0.52,
                no_bid=0.47, no_ask=0.53, yes_bid_depth=100, yes_ask_depth=80,
                no_bid_depth=90, no_ask_depth=70, yes_book={"bids": []}, no_book={"bids": []},
            )
            self.assertTrue(journal.record_snapshot(item, **arguments))
            self.assertFalse(journal.record_snapshot(item, **arguments))
            journal.record_settlement(item.market_id, "yes", {"closed": True}, settled_at=observed_at)
            stored = journal.list_contracts(symbols=("TSLA",), market_date="2026-08-03")
            snapshots = journal.list_snapshots(market_date="2026-08-03", checkpoint_only=True)
            latest = journal.latest_snapshot_rows("2026-08-03")
        self.assertEqual(stored, (item,))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(latest[0].yes_bid, 0.48)

    def test_snapshot_requires_timezone_aware_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = PriceLadderJournal(Path(directory) / "journal.db")
            journal.initialize()
            with self.assertRaises(ValueError):
                journal.record_snapshot(
                    contract(), observed_at=datetime(2026, 8, 3, 16), checkpoint_name=None,
                    yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
                    yes_bid_depth=0, yes_ask_depth=0, no_bid_depth=0, no_ask_depth=0,
                    yes_book={}, no_book={},
                )


if __name__ == "__main__":
    unittest.main()
