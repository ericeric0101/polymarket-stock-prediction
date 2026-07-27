from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.baseline import DailyClose
from polymarket_stock.equity_contracts import parse_daily_equity_close_contract
from polymarket_stock.market_discovery import MarketCandidate
from polymarket_stock.realtime import RealtimeBaselineEvaluator
from polymarket_stock.streaming import ShadowStreamCoordinator, SpotQuote
from polymarket_stock.supervisor import ActiveMarket, MultiMarketRouter, _dual_source_risk_reasons, select_active_candidates, symbol_from_candidate


def _candidate(market_id: str, symbol: str, end_date: str) -> MarketCandidate:
    return MarketCandidate.from_gamma_payload(
        {
            "id": market_id, "question": f"Tesla ({symbol}) Up or Down on July 20?", "slug": f"{symbol.lower()}-updown",
            "description": (
                f"The Close price for Tesla ({symbol}) is compared with the Close price on the most recent prior "
                "trading day. If equal, this market will resolve 50-50. Closing prices are published by Pyth "
                "without rounding."
            ),
            "resolutionSource": f"https://pyth.network/price-feeds/Equity.US.{symbol}%2FUSD", "endDate": end_date,
            "outcomes": '["Up", "Down"]', "clobTokenIds": [f"{market_id}-up", f"{market_id}-down"],
        }
    )


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 20, 12, tzinfo=UTC)
        self.future = (self.now + timedelta(hours=8)).isoformat()

    def test_universe_selection_uses_ticker_and_resolution_window(self) -> None:
        candidates = (_candidate("one", "TSLA", self.future), _candidate("two", "AAPL", (self.now + timedelta(seconds=10)).isoformat()))
        selected = select_active_candidates(candidates, now=self.now, max_markets=10, minimum_seconds_to_resolution=900)
        self.assertEqual([candidate.market_id for candidate in selected], ["one"])

    def test_universe_selection_excludes_next_new_york_trading_day(self) -> None:
        current_day = _candidate("current", "TSLA", self.future)
        next_day = _candidate("next", "AAPL", (self.now + timedelta(days=1, hours=8)).isoformat())
        selected = select_active_candidates(
            (current_day, next_day), now=self.now, max_markets=10, minimum_seconds_to_resolution=900
        )
        self.assertEqual([candidate.market_id for candidate in selected], ["current"])
        self.assertEqual(symbol_from_candidate(selected[0]), "TSLA")

    def test_dual_source_gate_requires_fresh_pyth_and_primary_prices(self) -> None:
        fresh_primary = SpotQuote("FINNHUB", "TSLA", 100.0, self.now, self.now)
        stale_pyth = SpotQuote("PYTH_HERMES", "TSLA", 100.0, self.now, self.now - timedelta(seconds=16))
        self.assertEqual(_dual_source_risk_reasons(self.now, fresh_primary, None, 15), ("PYTH_SPOT_UNAVAILABLE",))
        self.assertEqual(_dual_source_risk_reasons(self.now, fresh_primary, stale_pyth, 15), ("PYTH_SPOT_STALE",))
        self.assertEqual(_dual_source_risk_reasons(self.now, fresh_primary, SpotQuote("PYTH_HERMES", "TSLA", 100.0, self.now, self.now), 15), ())

    async def test_shared_router_dispatches_spot_and_books_to_owning_market(self) -> None:
        candidate = _candidate("one", "TSLA", self.future)
        closes = [DailyClose((self.now.date() - timedelta(days=day)).isoformat(), 100.0) for day in range(30, -1, -1)]
        evaluator = RealtimeBaselineEvaluator(
            market_id="one", symbol="TSLA", resolves_at=self.now + timedelta(hours=8), closes=closes,
            spot_provider="FINNHUB", up_fee_rate=0.04, down_fee_rate=0.04,
        )
        coordinator = ShadowStreamCoordinator(callback=lambda _payload: None, debounce_seconds=0.01)
        runtime = ActiveMarket(
            candidate, parse_daily_equity_close_contract(candidate), "TSLA", evaluator, "TEST", 0.04, 0.04, 100.0,
            self.now, 100.0, {"provider": "TEST_PYTH"}, None, ("OPTION_IV_PROVIDER_UNCONFIGURED",), (), 0.0, coordinator,
        )
        router = MultiMarketRouter({"one": runtime}, "finnhub")
        await router.on_spot_message({"type": "trade", "data": [{"s": "TSLA", "p": 101.0}]})
        await router.on_polymarket_message(
            {"event_type": "price_change", "price_changes": [{"asset_id": "one-up", "best_bid": "0.49", "best_ask": "0.50"}]}
        )
        await asyncio.sleep(0.02)
        await coordinator.close()
        self.assertEqual(coordinator.latest_spots["TSLA"], 101.0)
        self.assertEqual(coordinator.latest_best_asks["one-up"], 0.50)
