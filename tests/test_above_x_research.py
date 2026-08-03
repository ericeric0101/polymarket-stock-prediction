import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from polymarket_stock.above_x_research import AboveXHistoricalDiscovery, write_above_x_discovery


def _market(market_id: str, symbol: str = "TSLA") -> dict[str, object]:
    return {
        "id": market_id,
        "question": "Tesla (TSLA) closes above $300 on July 31?",
        "slug": "tsla-above-300",
        "description": "Resolves according to the Pyth close price.",
        "resolutionSource": "https://pyth.network/Equity.US.TSLA/USD",
        "endDate": "2026-07-31T20:00:00Z",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes-" + market_id, "no-" + market_id]),
    }


class AboveXDiscoveryTests(TestCase):
    def test_discovers_closed_market_and_writes_contracts(self) -> None:
        responses = [
            {"events": [{"id": "event-1", "slug": "tsla-above", "markets": [_market("1")]}], "next_cursor": "x"},
            {"events": [], "next_cursor": ""},
        ]
        report = AboveXHistoricalDiscovery(lambda *_args, **_kwargs: responses.pop(0)).discover(
            symbols=("TSLA",), page_size=10
        )
        self.assertEqual(len(report.contracts), 1)
        self.assertEqual(report.contracts[0].strike, 300.0)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            write_above_x_discovery(path, report)
            self.assertEqual(json.loads(path.read_text())[0]["market_id"], "1")

    def test_date_and_symbol_filters_are_applied(self) -> None:
        payloads = [
            {"events": [{"id": "event-1", "slug": "tsla-above", "markets": [_market("1")]}], "next_cursor": "x"},
            {"events": [], "next_cursor": ""},
        ]
        report = AboveXHistoricalDiscovery(lambda *_args, **_kwargs: payloads.pop(0)).discover(
            symbols=("TSLA",), date_start="2026-08-01", page_size=10
        )
        self.assertEqual(report.contracts, ())
