"""Read-only real-time streams and debounced shadow-revaluation triggers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import inspect
import json
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ALPACA_IEX_WS = "wss://stream.data.alpaca.markets/v2/iex"
FINNHUB_WS = "wss://ws.finnhub.io"
EventCallback = Callable[[Mapping[str, object]], Awaitable[None] | None]
StreamRunner = Callable[[], Awaitable[None]]


async def _emit(callback: EventCallback, payload: Mapping[str, object]) -> None:
    result = callback(payload)
    if inspect.isawaitable(result):
        await result


async def run_with_reconnect(
    name: str,
    run_once: StreamRunner,
    status_callback: EventCallback,
    *,
    initial_delay_seconds: float = 1.0,
    maximum_delay_seconds: float = 30.0,
) -> None:
    """Keep a public read-only stream alive across transient network closures."""

    delay_seconds = initial_delay_seconds
    while True:
        try:
            await run_once()
            error_message = "stream ended without an explicit close reason"
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, TimeoutError) as error:
            error_message = str(error)
        await _emit(
            status_callback,
            {
                "event_type": "STREAM_RECONNECTING",
                "stream": name,
                "error": error_message,
                "retry_in_seconds": delay_seconds,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        await asyncio.sleep(delay_seconds)
        delay_seconds = min(maximum_delay_seconds, delay_seconds * 2)


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
    latest_best_asks: dict[str, float] = field(default_factory=dict)
    latest_best_bids: dict[str, float] = field(default_factory=dict)
    _debouncer: DebouncedReevaluation = field(init=False)

    def __post_init__(self) -> None:
        self.freshness = StreamFreshness(self.max_age_seconds)
        self._debouncer = DebouncedReevaluation(self.debounce_seconds, self.callback)

    async def on_polymarket_message(self, payload: Mapping[str, object]) -> None:
        event_type = str(payload.get("event_type", ""))
        if event_type == "price_change":
            changes = payload.get("price_changes")
            if isinstance(changes, list):
                changed = False
                for change in changes:
                    if isinstance(change, Mapping):
                        changed = self._update_book(str(change.get("asset_id", "")), change, event_type) or changed
                if changed:
                    self._debouncer.notify("POLYMARKET_PRICE_CHANGE")
            return
        asset_id = str(payload.get("asset_id", ""))
        if asset_id and event_type in {"book", "best_bid_ask", "last_trade_price"}:
            if self._update_book(asset_id, payload, event_type):
                self._debouncer.notify(f"POLYMARKET_{event_type.upper()}")

    def _update_book(self, asset_id: str, payload: Mapping[str, object], event_type: str) -> bool:
        if not asset_id:
            return False
        best_bid = _as_probability(payload.get("best_bid"))
        best_ask = _as_probability(payload.get("best_ask"))
        if event_type == "book":
            best_bid = _best_level_price(payload.get("bids"), maximum=True)
            best_ask = _best_level_price(payload.get("asks"), maximum=False)
        self.latest_books[asset_id] = dict(payload)
        if best_bid is not None:
            self.latest_best_bids[asset_id] = best_bid
        if best_ask is not None:
            self.latest_best_asks[asset_id] = best_ask
        self.freshness.last_book_at = datetime.now(UTC)
        return True

    async def on_alpaca_message(self, payload: Mapping[str, object]) -> None:
        message_type = str(payload.get("T", ""))
        symbol = payload.get("S")
        price = payload.get("p") if message_type == "t" else payload.get("ap")
        if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
            self.latest_spots[symbol] = float(price)
            self.freshness.last_spot_at = datetime.now(UTC)
            self._debouncer.notify(f"ALPACA_{message_type.upper()}")

    async def on_finnhub_message(self, payload: Mapping[str, object]) -> None:
        """Accept Finnhub's trade batches in the same spot-update pipeline."""

        if payload.get("type") != "trade":
            return
        trades = payload.get("data")
        if not isinstance(trades, list):
            return
        received_spot = False
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            symbol = trade.get("s")
            price = trade.get("p")
            if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
                self.latest_spots[symbol] = float(price)
                received_spot = True
        if received_spot:
            self.freshness.last_spot_at = datetime.now(UTC)
            self._debouncer.notify("FINNHUB_TRADE")

    async def close(self) -> None:
        await self._debouncer.close()


def _as_probability(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 1 else None


def _best_level_price(levels: object, *, maximum: bool) -> float | None:
    if not isinstance(levels, list):
        return None
    prices = [_as_probability(level.get("price")) for level in levels if isinstance(level, Mapping)]
    usable = [price for price in prices if price is not None]
    return (max(usable) if maximum else min(usable)) if usable else None


class PolymarketMarketStream:
    async def run(self, token_ids: tuple[str, ...], callback: EventCallback) -> None:
        if not token_ids:
            raise ValueError("at least one Polymarket token ID is required")
        async with connect(POLYMARKET_MARKET_WS, ping_interval=None) as websocket:
            await websocket.send(json.dumps({"assets_ids": list(token_ids), "type": "market", "custom_feature_enabled": True}))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw_message in websocket:
                    payload = self._decode_message(raw_message)
                    if isinstance(payload, dict):
                        await _emit(callback, payload)
            finally:
                heartbeat.cancel()

    @staticmethod
    def _decode_message(raw_message: str | bytes) -> dict[str, object] | None:
        """Ignore text heartbeat acknowledgements such as the server's PONG."""

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

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


class FinnhubStockStream:
    """Read-only US equity trade stream; no brokerage account is involved."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Finnhub API key is required for stock streaming")
        self._api_key = api_key

    async def run(self, symbols: tuple[str, ...], callback: EventCallback) -> None:
        if not symbols:
            raise ValueError("at least one Finnhub symbol is required")
        websocket_url = f"{FINNHUB_WS}?{urlencode({'token': self._api_key})}"
        async with connect(websocket_url) as websocket:
            for symbol in symbols:
                await websocket.send(json.dumps({"type": "subscribe", "symbol": symbol}))
            async for raw_message in websocket:
                payload = json.loads(raw_message)
                if isinstance(payload, dict):
                    await _emit(callback, payload)
