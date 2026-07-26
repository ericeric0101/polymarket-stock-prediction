from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from polymarket_stock.yahoo_data import YAHOO_CHART_URL, YahooChartClient, YahooPayloadError


class YahooDataTests(unittest.TestCase):
    def test_daily_closes_parse_and_can_write_csv(self) -> None:
        observed = {}

        def fake_get_json(url, params):
            observed["url"] = url
            observed["params"] = params
            return {
                "chart": {
                    "result": [{
                        "timestamp": [1770000000, 1770086400],
                        "indicators": {"quote": [{"close": [101.25, 102.5]}]},
                    }]
                }
            }

        series = YahooChartClient(fake_get_json).daily_closes(
            "TSLA", start_date=date(2026, 2, 1), end_date=date(2026, 2, 2)
        )
        self.assertEqual(observed["url"], f"{YAHOO_CHART_URL}/TSLA")
        self.assertEqual(observed["params"]["interval"], "1d")
        self.assertEqual([item.close for item in series.closes], [101.25, 102.5])
        with TemporaryDirectory() as directory:
            output = Path(directory) / "TSLA.csv"
            series.write_csv(output)
            self.assertIn("Date,Close", output.read_text(encoding="utf-8"))

    def test_invalid_payload_is_rejected(self) -> None:
        client = YahooChartClient(lambda _url, _params: {"chart": {"result": []}})
        with self.assertRaises(YahooPayloadError):
            client.daily_closes("TSLA", start_date=date(2026, 2, 1), end_date=date(2026, 2, 2))
