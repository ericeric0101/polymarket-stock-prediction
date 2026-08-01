from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.baseline import DailyClose
from polymarket_stock.equity_contracts import parse_daily_equity_close_contract
from polymarket_stock.journal import ShadowJournal
from polymarket_stock.market_discovery import MarketCandidate, MarketSettlement
from polymarket_stock.realtime import RealtimeBaselineEvaluator
from polymarket_stock.streaming import ShadowStreamCoordinator, SpotQuote
from polymarket_stock.supervisor import ActiveMarket, MultiMarketRouter, MultiMarketShadowSupervisor, _cross_source_uncertainty_buffer, _pyth_primary_risk_reasons, select_active_candidates, symbol_from_candidate


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

    def test_pyth_primary_gate_requires_only_a_fresh_pyth_price(self) -> None:
        stale_pyth = SpotQuote("PYTH_HERMES", "TSLA", 100.0, self.now, self.now - timedelta(seconds=16))
        self.assertEqual(_pyth_primary_risk_reasons(self.now, None, 15), ("PYTH_SPOT_UNAVAILABLE",))
        self.assertEqual(_pyth_primary_risk_reasons(self.now, stale_pyth, 15), ("PYTH_SPOT_STALE",))
        self.assertEqual(_pyth_primary_risk_reasons(self.now, SpotQuote("PYTH_HERMES", "TSLA", 100.0, self.now, self.now), 15), ())

    def test_fresh_cross_source_difference_adds_bounded_model_buffer(self) -> None:
        pyth = SpotQuote("PYTH_HERMES", "TSLA", 100.0, self.now, self.now, 0.01)
        finnhub = SpotQuote("FINNHUB", "TSLA", 100.25, self.now, self.now)
        buffer = _cross_source_uncertainty_buffer(self.now, pyth, finnhub, 15)
        self.assertGreater(buffer, 0.0025)
        self.assertLess(buffer, 0.02)

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

    async def test_full_settlement_reconciliation_records_paper_and_model_outcomes(self) -> None:
        class SettledGamma:
            def get_market_settlement(self, market_id: str) -> MarketSettlement:
                return MarketSettlement(market_id, True, "UP", {"closed": True, "outcomePrices": "[\"1\", \"0\"]"})

        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "journal.db")
            journal.initialize()
            journal.record_realtime_evaluation({
                "evaluated_at": self.now.isoformat(), "market_id": "market-1", "symbol": "TSLA",
                "spot": 100.0, "up_ask": 0.50, "down_ask": 0.50, "fair_up_probability": 0.60,
                "model_outcome": "UP", "signal_status": "PAPER_UP", "skip_reasons": [],
            })
            position, _ = journal.open_paper_position(
                market_id="market-1", symbol="TSLA", outcome="UP", entry_ask=0.50,
                fair_probability=0.60, model_version="test", payload={}, fee_rate=0.04,
            )
            supervisor = MultiMarketShadowSupervisor(
                journal=journal, log_path=Path(directory) / "events.jsonl", spot_provider="finnhub",
                gamma_client=SettledGamma(),
            )
            await supervisor.reconcile_settlements()
            settled = next(item for item in journal.list_paper_positions() if item.position_id == position.position_id)
            outcome = journal.get_market_settlement_outcome("market-1")
        self.assertEqual(settled.status, "SETTLED")
        self.assertEqual(settled.settlement_outcome, "UP")
        self.assertEqual(outcome, "UP")
