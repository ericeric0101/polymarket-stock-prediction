from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.equity_contracts import EquityContractParseError, parse_daily_equity_close_contract
from polymarket_stock.market_discovery import MarketCandidate


def _candidate(*, description: str, source: str = "https://pyth.network/price-feeds/Equity.US.TSLA%2FUSD") -> MarketCandidate:
    return MarketCandidate.from_gamma_payload(
        {
            "id": "one", "question": "Tesla (TSLA) Up or Down on July 20?", "slug": "tsla-up-down",
            "description": description, "resolutionSource": source, "endDate": "2026-07-20T20:00:00Z",
            "outcomes": '["Up", "Down"]', "clobTokenIds": ["up", "down"],
        }
    )


class EquityContractParserTests(unittest.TestCase):
    def test_accepts_exact_pyth_daily_close_template(self) -> None:
        contract = parse_daily_equity_close_contract(_candidate(description=(
            "The Close price for Tesla (TSLA) is compared with the Close price on the most recent prior trading day. "
            "If the prices are equal, this market will resolve 50-50. Closing prices are published by Pyth without rounding."
        )))
        self.assertEqual(contract.symbol, "TSLA")
        self.assertEqual(contract.pyth_feed, "Equity.US.TSLA/USD")
        self.assertEqual(contract.resolves_at, datetime(2026, 7, 20, 20, tzinfo=UTC))

    def test_rejects_contract_without_tie_rule(self) -> None:
        with self.assertRaises(EquityContractParseError):
            parse_daily_equity_close_contract(_candidate(description=(
                "The Close price for Tesla (TSLA) is compared with the Close price on the most recent prior trading day. "
                "Closing prices are published by Pyth without rounding."
            )))
