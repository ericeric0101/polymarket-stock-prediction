from __future__ import annotations

import unittest

from polymarket_stock.alpaca_options import AlpacaCredentials, AlpacaIndicativeOptionsClient


class AlpacaOptionsTests(unittest.TestCase):
    def test_client_forces_indicative_feed_and_auth_headers(self) -> None:
        observed = {}

        def fake_get_json(url, params, timeout_seconds=15.0, headers=None):
            observed["url"] = url
            observed["params"] = params
            observed["headers"] = headers
            return {"quotes": {"SPY260718C00600000": {"bp": 1.0, "ap": 1.1}}}

        client = AlpacaIndicativeOptionsClient(
            AlpacaCredentials(api_key_id="test-key", api_secret_key="test-secret"),
            get_json_fn=fake_get_json,
        )
        quotes = client.latest_quotes(("SPY260718C00600000",))

        self.assertEqual(observed["params"]["feed"], "indicative")
        self.assertEqual(observed["headers"]["APCA-API-KEY-ID"], "test-key")
        self.assertEqual(quotes[0].quality_label, "INDICATIVE_DELAYED_OR_MODIFIED_NOT_LIVE_GRADE")
