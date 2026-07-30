from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import unittest

from polymarket_stock.streaming import (
    DebouncedReevaluation, FinnhubStockStream, PolymarketMarketStream, PythHermesStockStream,
    ShadowStreamCoordinator, SpotQuote,
    _has_finnhub_trade, _has_pyth_price, run_with_reconnect,
)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_debouncer_close_cancels_a_pending_callback(self) -> None:
        events = []
        debouncer = DebouncedReevaluation(1.0, events.append)
        debouncer.notify("TEST")
        await debouncer.close()
        await asyncio.sleep(0)
        self.assertEqual(events, [])

    async def test_debouncer_treats_keyboard_interrupt_during_flush_as_shutdown(self) -> None:
        def interrupted_callback(_payload):
            raise KeyboardInterrupt

        debouncer = DebouncedReevaluation(0.001, interrupted_callback)
        debouncer.notify("TEST")
        await asyncio.sleep(0.01)
        await debouncer.close()

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
            callback=lambda _payload: None, primary_spot_source="PYTH_HERMES", comparison_spot_source="FINNHUB",
            spot_observation_callback=record_spot, spot_comparison_callback=record_comparison,
            session_classifier=lambda _now: "REGULAR",
        )
        await coordinator.on_finnhub_message({"type": "trade", "data": [{"s": "TSLA", "p": 100.0, "t": 1_784_000_000_000}]})
        await coordinator.on_pyth_message(
            {"parsed": [{"id": "0xfeed", "price": {"price": "10025", "conf": "15", "expo": -2, "publish_time": 1_784_000_000}}]},
            {"feed": "TSLA"},
        )
        await coordinator.close()
        self.assertEqual(coordinator.latest_quote("PYTH_HERMES", "TSLA").price, 100.25)
        self.assertEqual(coordinator.latest_spots["TSLA"], 100.25)
        self.assertEqual([item["source"] for item in spots], ["FINNHUB", "PYTH_HERMES"])
        self.assertEqual(len(comparisons), 1)
        self.assertAlmostEqual(comparisons[0]["difference_bps"], -24.9376558603)


    async def test_coordinator_persists_only_regular_session_and_reports_source_gap(self) -> None:
        spots = []
        gaps = []

        async def record_spot(payload):
            spots.append(payload)

        async def record_gap(payload):
            gaps.append(payload)

        regular = ShadowStreamCoordinator(
            callback=lambda _payload: None, spot_observation_callback=record_spot,
            source_gap_callback=record_gap, session_classifier=lambda _now: "REGULAR", max_age_seconds=15,
        )
        start = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
        await regular._accept_spot(SpotQuote("PYTH_HERMES", "TSLA", 100.0, start), "TEST")
        await regular._accept_spot(SpotQuote("PYTH_HERMES", "TSLA", 100.1, start.replace(second=20)), "TEST")
        await regular.close()
        self.assertEqual(len(spots), 2)
        self.assertEqual(gaps[0]["event_type"], "SOURCE_SPOT_GAP_DETECTED")
        self.assertEqual(gaps[0]["gap_seconds"], 20)

        after_hours_spots = []
        after_hours = ShadowStreamCoordinator(
            callback=lambda _payload: None, spot_observation_callback=after_hours_spots.append,
            session_classifier=lambda _now: "AFTER_HOURS",
        )
        await after_hours._accept_spot(SpotQuote("PYTH_HERMES", "TSLA", 100.0, start), "TEST")
        await after_hours.close()
        self.assertEqual(after_hours_spots, [])

    def test_liveness_helpers_require_actual_price_messages(self) -> None:
        self.assertTrue(_has_finnhub_trade({"type": "trade", "data": [{"s": "TSLA", "p": 100.0}]}))
        self.assertFalse(_has_finnhub_trade({"type": "ping", "data": []}))
        self.assertTrue(_has_pyth_price({"parsed": [{"price": {"price": "100"}}]}))
        self.assertFalse(_has_pyth_price({"parsed": [{"price": {}}]}))

    async def test_pyth_stream_rejects_non_positive_silence_timeout(self) -> None:
        stream = PythHermesStockStream()
        with self.assertRaises(ValueError):
            await stream.run({"feed": "TSLA"}, lambda _payload: None, maximum_silence_seconds=0)

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
