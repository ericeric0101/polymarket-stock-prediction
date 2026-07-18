from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import unittest

from polymarket_stock.streaming import ShadowStreamCoordinator


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_debounces_spot_and_book_updates(self) -> None:
        events = []

        async def callback(payload):
            events.append(payload)

        coordinator = ShadowStreamCoordinator(callback=callback, debounce_seconds=0.01)
        await coordinator.on_alpaca_message({"T": "t", "S": "TSLA", "p": 100.0})
        await coordinator.on_polymarket_message({"event_type": "best_bid_ask", "asset_id": "up-token"})
        await asyncio.sleep(0.03)
        await coordinator.close()
        self.assertEqual(len(events), 1)
        self.assertIn("ALPACA_T", events[0]["reasons"])
        self.assertIn("POLYMARKET_BEST_BID_ASK", events[0]["reasons"])
        self.assertTrue(coordinator.freshness.ready(datetime.now(UTC)))
