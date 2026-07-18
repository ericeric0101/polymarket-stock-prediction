from __future__ import annotations

import unittest

from polymarket_stock.polymarket_data import ClobMarketDataClient, OrderBookSnapshot


BOOK = {
    "market": "condition-id",
    "bids": [{"price": "0.49", "size": "100"}, {"price": "0.48", "size": "10"}],
    "asks": [{"price": "0.51", "size": "20"}, {"price": "0.52", "size": "30"}],
    "min_order_size": "5",
    "tick_size": "0.01",
}


class PolymarketDataTests(unittest.TestCase):
    def test_order_book_extracts_executable_top_of_book(self) -> None:
        snapshot = OrderBookSnapshot.from_clob_payload("yes-token", BOOK)
        self.assertEqual(snapshot.best_bid, 0.49)
        self.assertEqual(snapshot.best_ask, 0.51)
        self.assertEqual(snapshot.midpoint, 0.5)

    def test_client_uses_public_token_query(self) -> None:
        observed_params = {}

        def fake_get_json(_url, params):
            observed_params.update(params)
            return BOOK

        snapshot = ClobMarketDataClient(get_json_fn=fake_get_json).get_order_book("yes-token")
        self.assertEqual(snapshot.token_id, "yes-token")
        self.assertEqual(observed_params, {"token_id": "yes-token"})
