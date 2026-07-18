from __future__ import annotations

import unittest

from polymarket_stock.baseline import DailyClose, annualized_realized_volatility, evaluate_realized_vol_baseline


def closes() -> list[DailyClose]:
    return [DailyClose(f"2026-06-{day:02d}", 100 + day * 0.5) for day in range(1, 30)]


class BaselineTests(unittest.TestCase):
    def test_realized_volatility_is_positive(self) -> None:
        self.assertGreater(annualized_realized_volatility(closes(), lookback_days=20), 0)

    def test_stale_fallback_never_recommends_paper_outcome(self) -> None:
        assessment = evaluate_realized_vol_baseline(
            spot=115, closes=closes(), seconds_to_resolution=4 * 3600,
            up_ask=0.2, down_ask=0.8, fee_rate=0.01, slippage=0.001,
            base_model_error_buffer=0.02, fallback_buffer=0.05, minimum_edge=0.01,
            data_is_fresh=False,
        )
        self.assertEqual(assessment.model_error_buffer, 0.07)
        self.assertIsNone(assessment.paper_outcome)
