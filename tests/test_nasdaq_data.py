from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.nasdaq_data import NasdaqBaselineClient, NasdaqQuote, load_baseline_cache, save_baseline_cache
from polymarket_stock.baseline import DailyClose


class NasdaqDataTests(unittest.TestCase):
    def test_parses_quote_and_daily_closes(self) -> None:
        rows = [{"date": f"01/{day:02d}/2026", "close": f"${100 + day}.00"} for day in range(1, 30)]

        def fake_get_json(url, _params, headers=None):
            self.assertIn("Mozilla", headers["User-Agent"])
            if url.endswith("/info"):
                return {"data": {"primaryData": {"lastSalePrice": "$130.00", "lastTradeTimestamp": "Jan 29, 2026", "isRealTime": False}}}
            return {"data": {"tradesTable": {"rows": list(reversed(rows))}}}

        client = NasdaqBaselineClient(fake_get_json)
        quote = client.latest_quote("TSLA")
        closes = client.daily_closes("TSLA", datetime(2026, 2, 1, tzinfo=UTC))
        self.assertEqual(quote.price, 130)
        self.assertEqual(closes[-1].close, 129)

    def test_cache_round_trip(self) -> None:
        quote = NasdaqQuote("TSLA", 100, datetime(2026, 1, 30, tzinfo=UTC), False)
        closes = [DailyClose(f"2026-01-{day:02d}", float(day)) for day in range(1, 30)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TSLA.json"
            save_baseline_cache(path, quote, closes)
            cached_quote, cached_closes = load_baseline_cache(path)
        self.assertEqual(cached_quote, quote)
        self.assertEqual(cached_closes, closes)
