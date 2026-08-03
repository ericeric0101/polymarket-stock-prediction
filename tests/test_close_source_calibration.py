from datetime import UTC, date, datetime
import unittest

from polymarket_stock.close_source_calibration import calibrate_close_sources
from polymarket_stock.pyth_history import PythIntradaySpotSeries
from polymarket_stock.streaming import SpotQuote


class FakePythHistory:
    def intraday_spots(self, symbol, *, start_at, end_at):
        value = 100.0 if start_at.date() == date(2026, 7, 30) else 101.0
        return PythIntradaySpotSeries(symbol.upper(), ((start_at, value),))


class CloseSourceCalibrationTests(unittest.TestCase):
    def test_reports_direction_flip_against_prior_pyth_close(self):
        report = calibrate_close_sources(
            client=FakePythHistory(),
            market_date=date(2026, 7, 31),
            symbols=("TSLA",),
            finnhub_spots=(SpotQuote("FINNHUB", "TSLA", 99.99, datetime(2026, 7, 31, 19, 59, 30, tzinfo=UTC)),),
        )
        item = report.observations[0]
        self.assertEqual(item.status, "COMPLETE")
        self.assertEqual(item.pyth_direction, "UP")
        self.assertEqual(item.finnhub_direction, "DOWN")
        self.assertTrue(item.direction_flipped)
        self.assertAlmostEqual(item.difference_bps or 0, (99.99 - 101.0) / 101.0 * 10_000)

    def test_reports_missing_finnhub_close_window_without_guessing(self):
        report = calibrate_close_sources(
            client=FakePythHistory(),
            market_date=date(2026, 7, 31),
            symbols=("NVDA",),
            finnhub_spots=(SpotQuote("FINNHUB", "NVDA", 99.0, datetime(2026, 7, 31, 19, 58, 59, tzinfo=UTC)),),
        )
        item = report.observations[0]
        self.assertEqual(item.status, "FINNHUB_CLOSE_UNAVAILABLE")
        self.assertIsNone(item.direction_flipped)


if __name__ == "__main__":
    unittest.main()
