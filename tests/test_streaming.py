from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import unittest

from polymarket_stock.streaming import FinnhubStockStream, PolymarketMarketStream, ShadowStreamCoordinator, run_with_reconnect


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

    async def test_coordinator_reconstructs_top_five_depth_after_price_changes(self) -> None:
        coordinator = ShadowStreamCoordinator(callback=lambda _payload: None, debounce_seconds=0.01)
        await coordinator.on_polymarket_message({
            "event_type": "book", "asset_id": "up-token",
            "bids": [{"price": "0.49", "size": "10"}, {"price": "0.48", "size": "8"}],
            "asks": [{"price": "0.52", "size": "5"}],
        })
        await coordinator.on_polymarket_message({
            "event_type": "price_change", "price_changes": [
                {"asset_id": "up-token", "side": "BUY", "price": "0.50", "size": "7", "best_bid": "0.50", "best_ask": "0.52"}
            ],
        })
        await coordinator.close()
        snapshot = coordinator.latest_books["up-token"]
        self.assertEqual(snapshot["bids"][0], {"price": 0.5, "size": 7.0})
        self.assertEqual(snapshot["asks"], [{"price": 0.52, "size": 5.0}])

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

    async def test_coordinator_records_bounded_pyth_finnhub_comparison(self) -> None:
        spots = []
        comparisons = []

        async def record_spot(payload):
            spots.append(payload)

        async def record_comparison(payload):
            comparisons.append(payload)

        coordinator = ShadowStreamCoordinator(
            callback=lambda _payload: None, primary_spot_source="FINNHUB",
            spot_observation_callback=record_spot, spot_comparison_callback=record_comparison,
        )
        await coordinator.on_finnhub_message({"type": "trade", "data": [{"s": "TSLA", "p": 100.0, "t": 1_784_000_000_000}]})
        await coordinator.on_pyth_message(
            {"parsed": [{"id": "0xfeed", "price": {"price": "10025", "conf": "15", "expo": -2, "publish_time": 1_784_000_000}}]},
            {"feed": "TSLA"},
        )
        await coordinator.close()
        self.assertEqual(coordinator.latest_quote("PYTH_HERMES", "TSLA").price, 100.25)
        self.assertEqual([item["source"] for item in spots], ["FINNHUB", "PYTH_HERMES"])
        self.assertEqual(len(comparisons), 1)
        self.assertAlmostEqual(comparisons[0]["difference_bps"], -24.9376558603)

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

    async def test_finnhub_stream_rejects_non_positive_silence_timeout(self) -> None:
        stream = FinnhubStockStream("test-key")
        with self.assertRaises(ValueError):
            await stream.run(("TSLA",), lambda _payload: None, maximum_silence_seconds=0)
