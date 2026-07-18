from __future__ import annotations

import unittest

from polymarket_stock.contracts import ContractValidationError, DailyDirectionContract


class DailyDirectionContractTests(unittest.TestCase):
    def test_mapping_requires_exact_contract_fields(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "reference_source"):
            DailyDirectionContract.from_mapping({"market_id": "abc"})

    def test_mapping_builds_valid_contract(self) -> None:
        contract = DailyDirectionContract.from_mapping(
            {
                "market_id": "market-123",
                "title": "Will SPY close higher today?",
                "underlying": "SPY",
                "reference_price": 600.0,
                "reference_source": "Official close",
                "resolves_at": "2026-07-18T16:00:00-04:00",
                "timezone": "America/New_York",
            }
        )
        self.assertEqual(contract.underlying, "SPY")
        self.assertEqual(contract.reference_price, 600.0)
