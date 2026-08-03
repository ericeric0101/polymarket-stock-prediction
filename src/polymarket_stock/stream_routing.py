"""Shared stream routing for active shadow markets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class _Coordinator(Protocol):
    async def on_polymarket_message(self, payload: Mapping[str, object]) -> None: ...
    async def on_finnhub_message(self, payload: Mapping[str, object]) -> None: ...
    async def on_alpaca_message(self, payload: Mapping[str, object]) -> None: ...
    async def on_pyth_message(self, payload: Mapping[str, object], feed_ids: Mapping[str, str]) -> None: ...


class RoutableMarket(Protocol):
    symbol: str
    coordinator: _Coordinator

    @property
    def token_ids(self) -> tuple[str, str]: ...


class MultiMarketRouter:
    """Dispatch one shared provider stream to the relevant market evaluators."""

    def __init__(
        self, runtimes: Mapping[str, RoutableMarket], spot_provider: str, pyth_feed_ids: Mapping[str, str] | None = None
    ) -> None:
        self._runtimes = runtimes
        self._spot_provider = spot_provider
        self._pyth_feed_ids = {symbol.upper(): feed_id for symbol, feed_id in (pyth_feed_ids or {}).items()}
        self._token_market_ids: dict[str, list[str]] = {}
        self._symbol_market_ids: dict[str, list[str]] = {}
        for market_id, runtime in runtimes.items():
            for token_id in runtime.token_ids:
                self._token_market_ids.setdefault(token_id, []).append(market_id)
            self._symbol_market_ids.setdefault(runtime.symbol, []).append(market_id)

    async def on_polymarket_message(self, payload: Mapping[str, object]) -> None:
        event_type = str(payload.get("event_type", ""))
        if event_type == "price_change":
            changes = payload.get("price_changes")
            if not isinstance(changes, list):
                return
            for change in changes:
                if not isinstance(change, Mapping):
                    continue
                await self._dispatch_book(
                    {"event_type": event_type, "price_changes": [dict(change)]}, str(change.get("asset_id", ""))
                )
            return
        await self._dispatch_book(payload, str(payload.get("asset_id", "")))

    async def _dispatch_book(self, payload: Mapping[str, object], token_id: str) -> None:
        for market_id in self._token_market_ids.get(token_id, ()):
            await self._runtimes[market_id].coordinator.on_polymarket_message(payload)

    async def on_spot_message(self, payload: Mapping[str, object]) -> None:
        if self._spot_provider == "finnhub":
            trades = payload.get("data") if payload.get("type") == "trade" else None
            if not isinstance(trades, list):
                return
            by_symbol: dict[str, list[object]] = {}
            for trade in trades:
                if isinstance(trade, Mapping) and isinstance(trade.get("s"), str):
                    by_symbol.setdefault(str(trade["s"]).upper(), []).append(dict(trade))
            for symbol, symbol_trades in by_symbol.items():
                for market_id in self._symbol_market_ids.get(symbol, ()):
                    await self._runtimes[market_id].coordinator.on_finnhub_message(
                        {"type": "trade", "data": symbol_trades}
                    )
            return
        symbol = payload.get("S")
        if isinstance(symbol, str):
            for market_id in self._symbol_market_ids.get(symbol.upper(), ()):
                await self._runtimes[market_id].coordinator.on_alpaca_message(payload)

    async def on_pyth_message(self, payload: Mapping[str, object]) -> None:
        for market_id, runtime in self._runtimes.items():
            feed_id = self._pyth_feed_ids.get(runtime.symbol)
            if feed_id:
                await runtime.coordinator.on_pyth_message(payload, {feed_id: runtime.symbol})
