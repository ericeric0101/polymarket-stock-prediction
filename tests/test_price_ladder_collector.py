from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.polymarket_data import OrderBookSnapshot
from polymarket_stock.price_ladder_collector import PriceLadderCollector, PriceLadderGammaClient
from polymarket_stock.price_ladder_journal import PriceLadderJournal
from tests.test_price_ladder import candidate_payload


class FakeClob:
    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        if token_id == "broken":
            raise RuntimeError("unavailable")
        is_yes = token_id.startswith("yes")
        payload = {
            "market": "condition", "bids": [{"price": "0.48" if is_yes else "0.47", "size": "20"}],
            "asks": [{"price": "0.52" if is_yes else "0.53", "size": "30"}],
        }
        return OrderBookSnapshot.from_clob_payload(token_id, payload, datetime(2026, 8, 3, 16, tzinfo=UTC))


class PriceLadderCollectorTests(unittest.TestCase):
    def test_discovery_deduplicates_tags_and_rejects_non_matching_contracts(self) -> None:
        valid = candidate_payload()
        invalid = {**candidate_payload(320), "resolutionSource": "https://example.com"}
        event = {"id": "event", "slug": "event-slug", "title": "TSLA closes above", "active": True,
                 "closed": False, "markets": [valid, invalid]}
        client = PriceLadderGammaClient(get_json_fn=lambda _url, _params: {"events": [event], "next_cursor": ""})
        report = client.discover(symbols=("TSLA",), tag_slugs=("stocks", "equities"))
        self.assertEqual(len(report.contracts), 1)
        self.assertEqual(report.contracts[0].event_id, "event")
        self.assertEqual(report.rejected_markets, 2)

    def test_ctrl_c_during_book_request_stops_without_traceback(self) -> None:
        event = {"id": "event", "slug": "event-slug", "title": "TSLA closes above", "active": True,
                 "closed": False, "markets": [candidate_payload()]}
        gamma = PriceLadderGammaClient(get_json_fn=lambda _url, _params: {"events": [event], "next_cursor": ""})

        class InterruptingClob:
            def get_order_book(self, _token_id: str) -> OrderBookSnapshot:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            journal = PriceLadderJournal(Path(directory) / "journal.db")
            journal.initialize()
            collector = PriceLadderCollector(journal=journal, gamma=gamma, clob=InterruptingClob())
            output = io.StringIO()
            with redirect_stdout(output):
                collector.run(symbols=("TSLA",), interval_seconds=60)
        self.assertEqual(output.getvalue(), "\nStopped cleanly.\n")

    def test_collection_records_checkpoint_books_without_affecting_core_tables(self) -> None:
        event = {"id": "event", "slug": "event-slug", "title": "TSLA closes above", "active": True,
                 "closed": False, "markets": [candidate_payload()]}
        gamma = PriceLadderGammaClient(get_json_fn=lambda _url, _params: {"events": [event], "next_cursor": ""})
        with tempfile.TemporaryDirectory() as directory:
            journal = PriceLadderJournal(Path(directory) / "journal.db")
            journal.initialize()
            collector = PriceLadderCollector(journal=journal, gamma=gamma, clob=FakeClob())
            contract = collector.discover_and_store(symbols=("TSLA",)).contracts[0]
            report = collector.collect_once(contracts=(contract,), observed_at=datetime(2026, 8, 3, 16, tzinfo=UTC))
            snapshots = journal.list_snapshots(checkpoint_only=True)
        self.assertEqual((report.snapshots_written, report.failures), (1, ()))
        self.assertEqual(snapshots[0].checkpoint_name, "1200_EDT")
        self.assertEqual(snapshots[0].yes_bid_depth, 20)
        self.assertEqual(snapshots[0].yes_ask_depth, 30)


if __name__ == "__main__":
    unittest.main()
