from __future__ import annotations

import unittest

from polymarket_stock.fees import PolymarketFeeRateClient, estimate_taker_fee_usdc


class PolymarketFeeTests(unittest.TestCase):
    def test_official_taker_fee_uses_price_symmetric_curve_and_precision(self) -> None:
        self.assertAlmostEqual(estimate_taker_fee_usdc(shares=100, price=0.50, fee_rate=0.04), 1.0)
        self.assertAlmostEqual(estimate_taker_fee_usdc(shares=1, price=0.51, fee_rate=0.04), 0.01)
        self.assertAlmostEqual(estimate_taker_fee_usdc(shares=100, price=0.30, fee_rate=0.04), 0.84)

    def test_client_parses_base_fee_bps_and_uses_cache(self) -> None:
        calls: list[str] = []

        def get_json(url: str) -> object:
            calls.append(url)
            return {"base_fee": 400}

        client = PolymarketFeeRateClient(get_json_fn=get_json)
        first = client.get_fee_rate("token-1")
        second = client.get_fee_rate("token-1")
        self.assertEqual(first.fee_rate, 0.04)
        self.assertEqual(second.fee_rate, 0.04)
        self.assertEqual(len(calls), 1)
