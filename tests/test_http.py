from __future__ import annotations

from unittest.mock import patch
import unittest

from polymarket_stock.http import PublicApiError, get_json


class HttpTests(unittest.TestCase):
    def test_socket_timeout_becomes_public_api_error(self) -> None:
        with patch("polymarket_stock.http.urlopen", side_effect=TimeoutError("read timed out")):
            with self.assertRaisesRegex(PublicApiError, "read timed out"):
                get_json("https://example.test/data")
