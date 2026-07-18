"""Read-only real-time streams and debounced shadow-revaluation triggers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import inspect
import json
from typing import Awaitable, Callable, Mapping

from websockets.asyncio.client import connect


POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ALPACA_IEX_WS = "wss://stream.data.alpaca.markets/v2/iex"
EventCallback = Callable[[Mapping[str, object]], Awaitable[None] | None]


async def _emit(callback: EventCallback, payload: Mapping[str, object]) -> None:
    result = callback(payload)
    if inspect.isawaitable(result):
        await result


class DebouncedReevaluation:
    def __init__(self, delay_seconds: float, callback: EventCallback) -> None:
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        self._delay_seconds = delay_seconds
        self._callback = callback
        self._reasons: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def notify(self, reason: str) -> None:
        self._reasons.add(reason)
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            await asyncio.sleep(self._delay_seconds)
        except asyncio.CancelledError:
            return
        reasons = sorted(self._reasons)
        self._reasons.clear()
        await _emit(self._callback, {"event_type": "SHADOW_REEVALUATION_REQUESTED", "reasons": reasons, "recorded_at": datetime.now(UTC).isoformat()})

    async def close(self) -> None:
        if self._task:
            await self._task


@dataclass
class StreamFreshness:
    max_age_seconds: float
    last_spot_at: datetime | None = None
    last_book_at: datetime | None = None

    def ready(self, now: datetime) -> bool:
        if now.tzinfo is None or self.last_spot_at is None or self.last_book_at is None:
            return False
        maximum_age = timedelta(seconds=self.max_age_seconds)
        return now - self.last_spot_at <= maximum_age and now - self.last_book_at <= maximum_age


@dataclass
class ShadowStreamCoordinator:
    callback: EventCallback
    debounce_seconds: float = 0.5
    max_age_seconds: float = 15.0
    freshness: StreamFreshness = field(init=False)
    latest_spots: dict[str, float] = field(default_factory=dict)
    latest_books: dict[str, Mapping[str, object]] = field(default_factory=dict)
    _debouncer: DebouncedReevaluation = field(init=False)

    def __post_init__(self) -> None:
        self.freshness = StreamFreshness(self.max_age_seconds)
        self._debouncer = DebouncedReevaluation(self.debounce_seconds, self.callback)

    async def on_polymarket_message(self, payload: Mapping[str, object]) -> None:
        event_type = str(payload.get("event_type", ""))
        asset_id = str(payload.get("asset_id", ""))
        if asset_id and event_type in {"book", "price_change", "best_bid_ask", "last_trade_price"}:
            self.latest_books[asset_id] = dict(payload)
            self.freshness.last_book_at = datetime.now(UTC)
            self._debouncer.notify(f"POLYMARKET_{event_type.upper()}")

    async def on_alpaca_message(self, payload: Mapping[str, object]) -> None:
        message_type = str(payload.get("T", ""))
        symbol = payload.get("S")
        price = payload.get("p") if message_type == "t" else payload.get("ap")
        if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
            self.latest_spots[symbol] = float(price)
            self.freshness.last_spot_at = datetime.now(UTC)
            self._debouncer.notify(f"ALPACA_{message_type.upper()}")

    async def close(self) -> None:
        await self._debouncer.close()


class PolymarketMarketStream:
    async def run(self, token_ids: tuple[str, ...], callback: EventCallback) -> None:
        if not token_ids:
            raise ValueError("at least one Polymarket token ID is required")
        async with connect(POLYMARKET_MARKET_WS, ping_interval=None) as websocket:
            await websocket.send(json.dumps({"assets_ids": list(token_ids), "type": "market", "custom_feature_enabled": True}))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw_message in websocket:
                    payload = json.loads(raw_message)
                    if isinstance(payload, dict):
                        await _emit(callback, payload)
            finally:
                heartbeat.cancel()

    @staticmethod
    async def _heartbeat(websocket: object) -> None:
        while True:
            await asyncio.sleep(10)
            await websocket.send("PING")


class AlpacaIexStockStream:
    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("Alpaca API key and secret are required for IEX streaming")
        self._api_key = api_key
        self._api_secret = api_secret

    async def run(self, symbols: tuple[str, ...], callback: EventCallback) -> None:
        if not symbols or len(symbols) > 30:
            raise ValueError("Alpaca Basic IEX stream supports 1 to 30 symbols")
        async with connect(ALPACA_IEX_WS) as websocket:
            await websocket.send(json.dumps({"action": "auth", "key": self._api_key, "secret": self._api_secret}))
            await websocket.send(json.dumps({"action": "subscribe", "trades": list(symbols), "quotes": list(symbols)}))
            async for raw_message in websocket:
                messages = json.loads(raw_message)
                for payload in messages if isinstance(messages, list) else [messages]:
                    if isinstance(payload, dict):
                        await _emit(callback, payload)
