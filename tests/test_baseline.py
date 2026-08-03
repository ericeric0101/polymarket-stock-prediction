from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket_stock.baseline import (
    DailyBar,
    DailyClose,
    annualized_realized_volatility,
    annualized_volatility,
    evaluate_realized_vol_baseline,
    load_daily_bars_csv,
)


def closes() -> list[DailyClose]:
    return [DailyClose(f"2026-06-{day:02d}", 100 + day * 0.5) for day in range(1, 30)]


class BaselineTests(unittest.TestCase):
    def test_realized_volatility_is_positive(self) -> None:
        self.assertGreater(annualized_realized_volatility(closes(), lookback_days=20), 0)

    def test_ewma_volatility_is_available_as_an_explicit_estimator(self) -> None:
        value = annualized_volatility(closes(), lookback_days=20, estimator="EWMA", decay=0.94)
        self.assertGreater(value, 0)
        self.assertNotEqual(value, annualized_realized_volatility(closes(), lookback_days=20))

    def test_ohlc_estimators_use_daily_bars(self) -> None:
        bars = [
            DailyBar(f"2026-06-{day:02d}", 99 + day * 0.5, 101 + day * 0.5, 98 + day * 0.5, 100 + day * 0.5)
            for day in range(1, 30)
        ]
        for estimator in ("GARMAN_KLASS", "YANG_ZHANG"):
            self.assertGreater(annualized_volatility(bars, lookback_days=20, estimator=estimator), 0)

    def test_ohlc_estimators_require_daily_bars(self) -> None:
        with self.assertRaises(ValueError):
            annualized_volatility(closes(), lookback_days=20, estimator="YANG_ZHANG")

    def test_ohlc_csv_loader_requires_and_preserves_bar_fields(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "Date,Open,High,Low,Close\n"
                "2026-06-01,99,101,98,100\n"
                "2026-06-02,100,102,99,101\n"
                "2026-06-03,101,103,100,102\n",
                encoding="utf-8",
            )
            bars = load_daily_bars_csv(path)
        self.assertEqual(bars[0], DailyBar("2026-06-01", 99.0, 101.0, 98.0, 100.0))

    def test_pyth_price_to_beat_override_replaces_non_settlement_close(self) -> None:
        assessment = evaluate_realized_vol_baseline(
            spot=115,
            closes=closes(),
            seconds_to_resolution=4 * 3600,
            up_ask=0.2,
            down_ask=0.8,
            up_fee_rate=0.04,
            down_fee_rate=0.04,
            base_model_error_buffer=0.02,
            fallback_buffer=0.0,
            minimum_edge=0.01,
            data_is_fresh=True,
            price_to_beat_override=114.25,
        )
        self.assertEqual(assessment.prior_close, 114.25)

    def test_stale_fallback_never_recommends_paper_outcome(self) -> None:
        assessment = evaluate_realized_vol_baseline(
            spot=115,
            closes=closes(),
            seconds_to_resolution=4 * 3600,
            up_ask=0.2,
            down_ask=0.8,
            up_fee_rate=0.04,
            down_fee_rate=0.04,
            base_model_error_buffer=0.02,
            fallback_buffer=0.05,
            minimum_edge=0.01,
            data_is_fresh=False,
        )
        self.assertEqual(assessment.model_error_buffer, 0.07)
        self.assertIsNone(assessment.paper_outcome)
