from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.clob_history import CLOB_PRICES_HISTORY_URL, ClobPriceHistoryClient, PriceHistoryPayloadError


class ClobPriceHistoryTests(unittest.TestCase):
    def test_client_parses_sorted_history_points(self) -> None:
        observed = {}

        def fake_get_json(url, params):
            observed["url"] = url
            observed["params"] = params
            return {"history": [{"t": 1770000060, "p": 0.51}, {"t": 1770000000, "p": 0.49}]}

        points = ClobPriceHistoryClient(fake_get_json).prices_history(
            "token-1",
            start_at=datetime(2026, 2, 1, tzinfo=UTC),
            end_at=datetime(2026, 2, 2, tzinfo=UTC),
        )
        self.assertEqual(observed["url"], CLOB_PRICES_HISTORY_URL)
        self.assertEqual(observed["params"]["market"], "token-1")
        self.assertEqual([point.price for point in points], [0.49, 0.51])

    def test_invalid_probability_is_rejected(self) -> None:
        client = ClobPriceHistoryClient(lambda _url, _params: {"history": [{"t": 1, "p": 1.2}]})
        with self.assertRaises(PriceHistoryPayloadError):
            client.prices_history("token-1")
