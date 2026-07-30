from __future__ import annotations

from datetime import UTC, date, datetime
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

    def test_intraday_spots_write_required_csv_shape(self) -> None:
        client = YahooChartClient(lambda _url, _params: {
            "chart": {"result": [{
                "timestamp": [1784554200, 1784554260],
                "indicators": {"quote": [{"close": [370.0, 370.25]}]},
            }]}
        })
        start_at = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
        series = client.intraday_spots("TSLA", start_at=start_at, end_at=datetime(2026, 7, 20, 20, tzinfo=UTC))
        self.assertEqual(len(series.points), 2)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "TSLA_intraday.csv"
            series.write_csv(output)
            self.assertIn("DateTime,Spot", output.read_text(encoding="utf-8"))

    def test_daily_bars_parse_and_write_ohlc_csv(self) -> None:
        client = YahooChartClient(lambda _url, _params: {
            "chart": {"result": [{
                "timestamp": [1770000000, 1770086400, 1770172800],
                "indicators": {"quote": [{
                    "open": [100.0, 101.0, 102.0],
                    "high": [102.0, 103.0, 104.0],
                    "low": [99.0, 100.0, 101.0],
                    "close": [101.0, 102.0, 103.0],
                }]},
            }]}
        })
        series = client.daily_bars("TSLA", start_date=date(2026, 2, 1), end_date=date(2026, 2, 3))
        self.assertEqual(series.bars[0].high, 102.0)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "TSLA_ohlc.csv"
            series.write_csv(output)
            self.assertIn("Date,Open,High,Low,Close", output.read_text(encoding="utf-8"))

    def test_invalid_payload_is_rejected(self) -> None:
        client = YahooChartClient(lambda _url, _params: {"chart": {"result": []}})
        with self.assertRaises(YahooPayloadError):
            client.daily_closes("TSLA", start_date=date(2026, 2, 1), end_date=date(2026, 2, 2))
