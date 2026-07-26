from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.pyth_benchmarks import PythBenchmarksClient, PythPayloadError


class PythBenchmarksTests(unittest.TestCase):
    def test_resolves_equity_feed_and_scales_price(self) -> None:
        feed_id = "feed-id"
        def fake_get_json(url, params):
            if "price_feeds" in url:
                return [{"id": feed_id, "attributes": {"symbol": "Equity.US.TSLA/USD"}}]
            self.assertEqual(params, {"ids": feed_id})
            return {"parsed": [{"id": feed_id, "price": {"price": "36958009", "conf": "44951", "expo": -5, "publish_time": 1784577600}}]}
        client = PythBenchmarksClient(fake_get_json)
        self.assertEqual(client.equity_feed_id("tsla"), feed_id)
        quote = client.price_at(symbol="TSLA", feed_id=feed_id, observed_at=datetime(2026, 7, 20, 20, tzinfo=UTC))
        self.assertAlmostEqual(quote.price, 369.58009)
        self.assertAlmostEqual(quote.confidence, 0.44951)

    def test_rejects_price_before_requested_timestamp(self) -> None:
        client = PythBenchmarksClient(lambda _url, _params: {"parsed": [{"id": "feed", "price": {"price": "1", "conf": "1", "expo": 0, "publish_time": 1}}]})
        with self.assertRaises(PythPayloadError):
            client.price_at(symbol="TSLA", feed_id="feed", observed_at=datetime(2026, 7, 20, 20, tzinfo=UTC))
