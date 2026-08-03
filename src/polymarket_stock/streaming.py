"""Read-only real-time streams and debounced shadow-revaluation triggers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import inspect
import json
import ssl
from typing import Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from .quality import us_equity_session

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ALPACA_IEX_WS = "wss://stream.data.alpaca.markets/v2/iex"
FINNHUB_WS = "wss://ws.finnhub.io"
FINNHUB_MAX_SILENCE_SECONDS = 60.0
PYTH_HERMES_HOST = "hermes.pyth.network"
PYTH_HERMES_STREAM_PATH = "/v2/updates/price/stream"
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
        except (ConnectionClosed, OSError, TimeoutError, asyncio.IncompleteReadError) as error:
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
    def __init__(
        self, delay_seconds: float, callback: EventCallback, *, error_callback: EventCallback | None = None
    ) -> None:
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        self._delay_seconds = delay_seconds
        self._callback = callback
        self._error_callback = error_callback
        self._reasons: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def notify(self, reason: str) -> None:
        if self._closed:
            return
        self._reasons.add(reason)
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            await asyncio.sleep(self._delay_seconds)
            if self._closed:
                return
            reasons = sorted(self._reasons)
            self._reasons.clear()
            await _emit(
                self._callback,
                {
                    "event_type": "SHADOW_REEVALUATION_REQUESTED",
                    "reasons": reasons,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
        except asyncio.CancelledError:
            return
        except KeyboardInterrupt:
            return
        except Exception as error:
            if self._error_callback is not None:
                await _emit(
                    self._error_callback,
                    {
                        "event_type": "SHADOW_REEVALUATION_FAILED",
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "recorded_at": datetime.now(UTC).isoformat(),
                    },
                )

    async def close(self) -> None:
        self._closed = True
        self._reasons.clear()
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass


@dataclass(frozen=True)
class SpotQuote:
    """A source-stamped underlying price retained for cross-source research."""

    source: str
    symbol: str
    price: float
    observed_at: datetime
    published_at: datetime | None = None
    confidence: float | None = None
    feed_id: str | None = None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "price": self.price,
            "observed_at": self.observed_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "confidence": self.confidence,
            "feed_id": self.feed_id,
        }


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
    primary_spot_source: str | None = None
    comparison_spot_source: str | None = None
    spot_observation_callback: EventCallback | None = None
    spot_comparison_callback: EventCallback | None = None
    source_gap_callback: EventCallback | None = None
    reevaluation_error_callback: EventCallback | None = None
    session_classifier: Callable[[datetime], str] = us_equity_session
    debounce_seconds: float = 0.5
    max_age_seconds: float = 15.0
    freshness: StreamFreshness = field(init=False)
    latest_spots: dict[str, float] = field(default_factory=dict)
    latest_source_quotes: dict[str, dict[str, SpotQuote]] = field(default_factory=dict)
    latest_books: dict[str, Mapping[str, object]] = field(default_factory=dict)
    latest_book_levels: dict[str, dict[str, dict[float, float]]] = field(default_factory=dict)
    latest_best_asks: dict[str, float] = field(default_factory=dict)
    latest_best_bids: dict[str, float] = field(default_factory=dict)
    _debouncer: DebouncedReevaluation = field(init=False)
    _persisted_spot_seconds: dict[tuple[str, str], str] = field(default_factory=dict)
    _persisted_comparison_seconds: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.primary_spot_source = self.primary_spot_source.upper() if self.primary_spot_source else None
        self.comparison_spot_source = self.comparison_spot_source.upper() if self.comparison_spot_source else None
        self.freshness = StreamFreshness(self.max_age_seconds)
        self._debouncer = DebouncedReevaluation(
            self.debounce_seconds,
            self.callback,
            error_callback=self.reevaluation_error_callback,
        )

    def latest_quote(self, source: str, symbol: str) -> SpotQuote | None:
        return self.latest_source_quotes.get(source.upper(), {}).get(symbol.upper())

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
        self._update_depth(asset_id, payload, event_type)
        if best_bid is not None:
            self.latest_best_bids[asset_id] = best_bid
        if best_ask is not None:
            self.latest_best_asks[asset_id] = best_ask
        self.freshness.last_book_at = datetime.now(UTC)
        return True

    def _update_depth(self, asset_id: str, payload: Mapping[str, object], event_type: str) -> None:
        levels = self.latest_book_levels.setdefault(asset_id, {"bids": {}, "asks": {}})
        if event_type == "book":
            levels["bids"] = _levels_from_payload(payload.get("bids"))
            levels["asks"] = _levels_from_payload(payload.get("asks"))
        elif event_type == "price_change":
            side = str(payload.get("side", "")).upper()
            price = _as_probability(payload.get("price"))
            size = _as_nonnegative(payload.get("size"))
            book_side = "bids" if side in {"BUY", "BID"} else "asks" if side in {"SELL", "ASK"} else None
            if book_side is not None and price is not None and size is not None:
                if size == 0:
                    levels[book_side].pop(price, None)
                else:
                    levels[book_side][price] = size
        self.latest_books[asset_id] = {
            "event_type": "RECONSTRUCTED_L2",
            "source_event": event_type,
            "bids": _top_levels(levels["bids"], maximum=True),
            "asks": _top_levels(levels["asks"], maximum=False),
            "last_event": dict(payload),
        }

    async def on_alpaca_message(self, payload: Mapping[str, object]) -> None:
        message_type = str(payload.get("T", ""))
        symbol = payload.get("S")
        price = payload.get("p") if message_type == "t" else payload.get("ap")
        if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
            await self._accept_spot(
                SpotQuote("ALPACA", symbol.upper(), float(price), datetime.now(UTC)), f"ALPACA_{message_type.upper()}"
            )

    async def on_finnhub_message(self, payload: Mapping[str, object]) -> None:
        """Accept Finnhub trade batches in the same spot-update pipeline."""

        if payload.get("type") != "trade":
            return
        trades = payload.get("data")
        if not isinstance(trades, list):
            return
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            symbol = trade.get("s")
            price = trade.get("p")
            if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
                observed_at = datetime.now(UTC)
                published_at = _unix_timestamp(trade.get("t"), milliseconds=True)
                await self._accept_spot(
                    SpotQuote("FINNHUB", symbol.upper(), float(price), observed_at, published_at), "FINNHUB_TRADE"
                )

    async def on_pyth_message(self, payload: Mapping[str, object], feed_symbols: Mapping[str, str]) -> None:
        parsed = payload.get("parsed")
        if not isinstance(parsed, list):
            return
        normalized_symbols = {_normalize_feed_id(feed_id): symbol.upper() for feed_id, symbol in feed_symbols.items()}
        for item in parsed:
            if not isinstance(item, Mapping):
                continue
            feed_id = _normalize_feed_id(str(item.get("id", "")))
            symbol = normalized_symbols.get(feed_id)
            quote = item.get("price")
            if symbol is None or not isinstance(quote, Mapping):
                continue
            try:
                exponent = int(quote["expo"])
                price = int(quote["price"]) * (10**exponent)
                confidence = int(quote["conf"]) * (10**exponent)
                published_at = datetime.fromtimestamp(int(quote["publish_time"]), tz=UTC)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if price > 0 and confidence >= 0:
                await self._accept_spot(
                    SpotQuote("PYTH_HERMES", symbol, price, datetime.now(UTC), published_at, confidence, feed_id),
                    "PYTH_HERMES_SPOT",
                )

    async def _accept_spot(self, quote: SpotQuote, reason: str) -> None:
        source_quotes = self.latest_source_quotes.setdefault(quote.source, {})
        previous = source_quotes.get(quote.symbol)
        source_quotes[quote.symbol] = quote
        if self.primary_spot_source is None or quote.source == self.primary_spot_source:
            self.latest_spots[quote.symbol] = quote.price
            self.freshness.last_spot_at = quote.observed_at
        self._debouncer.notify(reason)
        if self.session_classifier(quote.observed_at) != "REGULAR":
            return
        if previous is not None:
            gap_seconds = (quote.observed_at - previous.observed_at).total_seconds()
            if gap_seconds > self.max_age_seconds and self.source_gap_callback is not None:
                await _emit(
                    self.source_gap_callback,
                    {
                        "event_type": "SOURCE_SPOT_GAP_DETECTED",
                        "source": quote.source,
                        "symbol": quote.symbol,
                        "gap_seconds": gap_seconds,
                        "previous_observed_at": previous.observed_at.isoformat(),
                        "observed_at": quote.observed_at.isoformat(),
                    },
                )
        await self._record_spot_if_due(quote)
        await self._record_comparison_if_due(quote.symbol, quote.observed_at)

    async def _record_spot_if_due(self, quote: SpotQuote) -> None:
        if self.spot_observation_callback is None:
            return
        bucket = _persistence_bucket(quote.observed_at)
        key = (quote.source, quote.symbol)
        if self._persisted_spot_seconds.get(key) == bucket:
            return
        self._persisted_spot_seconds[key] = bucket
        await _emit(self.spot_observation_callback, quote.as_payload())

    async def _record_comparison_if_due(self, symbol: str, observed_at: datetime) -> None:
        if self.spot_comparison_callback is None:
            return
        comparison_source = self.comparison_spot_source or self.primary_spot_source
        if comparison_source is None:
            return
        primary = self.latest_quote(comparison_source, symbol)
        pyth = self.latest_quote("PYTH_HERMES", symbol)
        if primary is None or pyth is None or primary.source == pyth.source:
            return
        bucket = _persistence_bucket(observed_at)
        if self._persisted_comparison_seconds.get(symbol) == bucket:
            return
        self._persisted_comparison_seconds[symbol] = bucket
        difference_bps = (primary.price - pyth.price) / pyth.price * 10_000
        await _emit(
            self.spot_comparison_callback,
            {
                "observed_at": observed_at.isoformat(),
                "symbol": symbol,
                "primary_source": primary.source,
                "primary_price": primary.price,
                "primary_published_at": primary.published_at.isoformat() if primary.published_at else None,
                "pyth_price": pyth.price,
                "pyth_published_at": pyth.published_at.isoformat() if pyth.published_at else None,
                "pyth_confidence": pyth.confidence,
                "pyth_feed_id": pyth.feed_id,
                "difference_bps": difference_bps,
            },
        )

    async def close(self) -> None:
        await self._debouncer.close()


def _persistence_bucket(observed_at: datetime) -> str:
    """Keep normal-session diagnostics at one minute, but preserve the final five minutes per second."""

    local = observed_at.astimezone(ZoneInfo("America/New_York"))
    if (local.hour, local.minute) >= (15, 55):
        return observed_at.replace(microsecond=0).isoformat()
    return observed_at.replace(second=0, microsecond=0).isoformat()


def _as_probability(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 1 else None


def _as_nonnegative(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _levels_from_payload(levels: object) -> dict[float, float]:
    if not isinstance(levels, list):
        return {}
    parsed = {}
    for level in levels:
        if not isinstance(level, Mapping):
            continue
        price = _as_probability(level.get("price"))
        size = _as_nonnegative(level.get("size"))
        if price is not None and size is not None and size > 0:
            parsed[price] = size
    return parsed


def _top_levels(levels: Mapping[float, float], *, maximum: bool) -> list[Mapping[str, float]]:
    return [{"price": price, "size": levels[price]} for price in sorted(levels, reverse=maximum)[:5]]


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
            await websocket.send(
                json.dumps({"assets_ids": list(token_ids), "type": "market", "custom_feature_enabled": True})
            )
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


def _is_regular_equity_session() -> bool:
    return us_equity_session(datetime.now(UTC)) == "REGULAR"


def _has_finnhub_trade(payload: Mapping[str, object]) -> bool:
    trades = payload.get("data")
    return (
        payload.get("type") == "trade"
        and isinstance(trades, list)
        and any(
            isinstance(trade, Mapping)
            and isinstance(trade.get("s"), str)
            and isinstance(trade.get("p"), (int, float))
            and float(trade["p"]) > 0
            for trade in trades
        )
    )


def _has_pyth_price(payload: Mapping[str, object]) -> bool:
    parsed = payload.get("parsed")
    return isinstance(parsed, list) and any(
        isinstance(item, Mapping) and isinstance(item.get("price"), Mapping) and item["price"].get("price") is not None
        for item in parsed
    )


class FinnhubStockStream:
    """Read-only US equity trade stream; no brokerage account is involved."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Finnhub API key is required for stock streaming")
        self._api_key = api_key

    async def run(
        self,
        symbols: tuple[str, ...],
        callback: EventCallback,
        *,
        maximum_silence_seconds: float = FINNHUB_MAX_SILENCE_SECONDS,
    ) -> None:
        if not symbols:
            raise ValueError("at least one Finnhub symbol is required")
        if maximum_silence_seconds <= 0:
            raise ValueError("maximum_silence_seconds must be positive")
        websocket_url = f"{FINNHUB_WS}?{urlencode({'token': self._api_key})}"
        async with connect(websocket_url) as websocket:
            for symbol in symbols:
                await websocket.send(json.dumps({"type": "subscribe", "symbol": symbol}))
            last_trade_at = asyncio.get_running_loop().time()
            while True:
                # Finnhub sends non-trade traffic too. Only actual subscribed trades prove
                # that the primary spot feed remains live during the regular session.
                elapsed = asyncio.get_running_loop().time() - last_trade_at
                timeout = (
                    max(0.01, maximum_silence_seconds - elapsed)
                    if _is_regular_equity_session()
                    else maximum_silence_seconds
                )
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                except TimeoutError:
                    if _is_regular_equity_session():
                        raise TimeoutError("Finnhub produced no usable trades during the liveness window")
                    continue
                payload = json.loads(raw_message)
                if isinstance(payload, dict):
                    if _has_finnhub_trade(payload):
                        last_trade_at = asyncio.get_running_loop().time()
                    await _emit(callback, payload)
                if (
                    _is_regular_equity_session()
                    and asyncio.get_running_loop().time() - last_trade_at >= maximum_silence_seconds
                ):
                    raise TimeoutError("Finnhub produced no usable trades during the liveness window")


class PythHermesStockStream:
    """Read-only Hermes SSE client for the same Pyth equity feeds named in contracts."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key.strip()

    async def run(
        self,
        feed_ids: Mapping[str, str],
        callback: EventCallback,
        *,
        maximum_silence_seconds: float = FINNHUB_MAX_SILENCE_SECONDS,
    ) -> None:
        if not feed_ids:
            raise ValueError("at least one Pyth feed id is required")
        if maximum_silence_seconds <= 0:
            raise ValueError("maximum_silence_seconds must be positive")
        query = urlencode([("ids[]", f"0x{_normalize_feed_id(feed_id)}") for feed_id in feed_ids], doseq=True)
        reader, writer = await asyncio.open_connection(
            PYTH_HERMES_HOST,
            443,
            ssl=ssl.create_default_context(),
            server_hostname=PYTH_HERMES_HOST,
        )
        request_lines = [
            f"GET {PYTH_HERMES_STREAM_PATH}?parsed=true&{query} HTTP/1.1",
            f"Host: {PYTH_HERMES_HOST}",
            "Accept: text/event-stream",
            "Connection: close",
        ]
        if self._api_key:
            request_lines.append(f"Authorization: Bearer {self._api_key}")
        writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        try:
            status_line = (await reader.readline()).decode("iso-8859-1").strip()
            if " 200 " not in f" {status_line} ":
                raise OSError(f"Pyth Hermes stream rejected request: {status_line}")
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in {b"", b"\r\n", b"\n"}:
                    break
                name, _, value = line.decode("iso-8859-1").partition(":")
                headers[name.lower()] = value.strip()
            buffer = ""
            last_price_at = asyncio.get_running_loop().time()
            body = _http_response_body(reader, headers).__aiter__()
            while True:
                timeout = (
                    max(0.01, maximum_silence_seconds - (asyncio.get_running_loop().time() - last_price_at))
                    if _is_regular_equity_session()
                    else None
                )
                try:
                    chunk = (
                        await anext(body) if timeout is None else await asyncio.wait_for(anext(body), timeout=timeout)
                    )
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    raise TimeoutError("Pyth Hermes produced no parsed price updates during the liveness window")
                buffer += chunk.decode("utf-8")
                while "\n\n" in buffer:
                    event, buffer = buffer.split("\n\n", 1)
                    data = "\n".join(line[5:].strip() for line in event.splitlines() if line.startswith("data:"))
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, Mapping):
                        if _has_pyth_price(payload):
                            last_price_at = asyncio.get_running_loop().time()
                        await _emit(callback, payload)
                if (
                    _is_regular_equity_session()
                    and asyncio.get_running_loop().time() - last_price_at >= maximum_silence_seconds
                ):
                    raise TimeoutError("Pyth Hermes produced no parsed price updates during the liveness window")
        finally:
            writer.close()
            await writer.wait_closed()


async def _http_response_body(reader: asyncio.StreamReader, headers: Mapping[str, str]):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await reader.readline()
            if not size_line:
                return
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError as error:
                raise OSError("invalid Pyth Hermes chunk framing") from error
            if size == 0:
                await reader.readline()
                return
            yield await reader.readexactly(size)
            await reader.readexactly(2)
        return
    while chunk := await reader.read(4096):
        yield chunk


def _normalize_feed_id(feed_id: str) -> str:
    return feed_id.lower().removeprefix("0x")


def _unix_timestamp(value: object, *, milliseconds: bool) -> datetime | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
