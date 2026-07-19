from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import unittest

from polymarket_stock.streaming import PolymarketMarketStream, ShadowStreamCoordinator, run_with_reconnect


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_debounces_spot_and_book_updates(self) -> None:
        events = []

        async def callback(payload):
            events.append(payload)

        coordinator = ShadowStreamCoordinator(callback=callback, debounce_seconds=0.01)
        await coordinator.on_alpaca_message({"T": "t", "S": "TSLA", "p": 100.0})
        await coordinator.on_polymarket_message(
            {"event_type": "best_bid_ask", "asset_id": "up-token", "best_bid": "0.50", "best_ask": "0.52"}
        )
        await asyncio.sleep(0.03)
        await coordinator.close()
        self.assertEqual(len(events), 1)
        self.assertIn("ALPACA_T", events[0]["reasons"])
        self.assertIn("POLYMARKET_BEST_BID_ASK", events[0]["reasons"])
        self.assertTrue(coordinator.freshness.ready(datetime.now(UTC)))
        self.assertEqual(coordinator.latest_best_asks["up-token"], 0.52)

    async def test_coordinator_extracts_book_and_price_change_asks(self) -> None:
        coordinator = ShadowStreamCoordinator(callback=lambda _payload: None, debounce_seconds=0.01)
        await coordinator.on_polymarket_message(
            {"event_type": "book", "asset_id": "up-token", "bids": [{"price": "0.49"}], "asks": [{"price": "0.52"}, {"price": "0.53"}]}
        )
        await coordinator.on_polymarket_message(
            {"event_type": "price_change", "price_changes": [{"asset_id": "down-token", "best_bid": "0.48", "best_ask": "0.49"}]}
        )
        await coordinator.close()
        self.assertEqual(coordinator.latest_best_asks, {"up-token": 0.52, "down-token": 0.49})

    async def test_coordinator_accepts_finnhub_trade_batch(self) -> None:
        events = []

        async def callback(payload):
            events.append(payload)

        coordinator = ShadowStreamCoordinator(callback=callback, debounce_seconds=0.01)
        await coordinator.on_finnhub_message({"type": "trade", "data": [{"s": "TSLA", "p": 101.25}]})
        await coordinator.on_polymarket_message({"event_type": "best_bid_ask", "asset_id": "up-token"})
        await asyncio.sleep(0.03)
        await coordinator.close()
        self.assertEqual(coordinator.latest_spots["TSLA"], 101.25)
        self.assertEqual(len(events), 1)
        self.assertIn("FINNHUB_TRADE", events[0]["reasons"])

    def test_polymarket_text_heartbeat_is_ignored(self) -> None:
        self.assertIsNone(PolymarketMarketStream._decode_message("PONG"))
        self.assertEqual(PolymarketMarketStream._decode_message('{"event_type":"book"}'), {"event_type": "book"})

    async def test_reconnect_runner_reports_a_transient_failure(self) -> None:
        statuses = []
        attempts = 0

        async def run_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("network interrupted")
            raise asyncio.CancelledError

        async def status_callback(payload):
            statuses.append(payload)

        task = asyncio.create_task(
            run_with_reconnect(
                "TEST", run_once, status_callback, initial_delay_seconds=0.001, maximum_delay_seconds=0.001
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(attempts, 2)
        self.assertEqual(statuses[0]["event_type"], "STREAM_RECONNECTING")
