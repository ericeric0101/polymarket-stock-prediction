"""Read-only CLOB order-book snapshots for shadow research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping

from .http import get_json


CLOB_BOOK_URL = "https://clob.polymarket.com/book"


class OrderBookPayloadError(ValueError):
    """Raised when a CLOB order-book response cannot be safely interpreted."""


@dataclass(frozen=True, order=True)
class PriceLevel:
    price: float
    size: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "PriceLevel":
        try:
            price = float(payload["price"])
            size = float(payload["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise OrderBookPayloadError("price level requires numeric price and size") from error
        if not 0 <= price <= 1 or size <= 0:
            raise OrderBookPayloadError("price level is outside valid bounds")
        return cls(price=price, size=size)


@dataclass(frozen=True)
class OrderBookSnapshot:
    token_id: str
    market: str
    observed_at: datetime
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    min_order_size: float | None
    tick_size: float | None
    raw_payload: Mapping[str, object]

    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @classmethod
    def from_clob_payload(cls, token_id: str, payload: Mapping[str, object], observed_at: datetime | None = None) -> "OrderBookSnapshot":
        bids_raw = payload.get("bids")
        asks_raw = payload.get("asks")
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            raise OrderBookPayloadError("order book requires bids and asks arrays")
        if not all(isinstance(level, dict) for level in bids_raw + asks_raw):
            raise OrderBookPayloadError("order book levels must be objects")
        timestamp = observed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise OrderBookPayloadError("observed_at must be timezone-aware")
        return cls(
            token_id=token_id,
            market=str(payload.get("market", "")),
            observed_at=timestamp,
            bids=tuple(PriceLevel.from_mapping(level) for level in bids_raw),
            asks=tuple(PriceLevel.from_mapping(level) for level in asks_raw),
            min_order_size=_optional_float(payload.get("min_order_size")),
            tick_size=_optional_float(payload.get("tick_size")),
            raw_payload=dict(payload),
        )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise OrderBookPayloadError("expected optional numeric field") from error
    if parsed <= 0:
        raise OrderBookPayloadError("optional numeric field must be positive")
    return parsed


class ClobMarketDataClient:
    """Public CLOB client with no trading or authentication capabilities."""

    def __init__(self, get_json_fn=get_json) -> None:
        self._get_json = get_json_fn

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        if not token_id.strip():
            raise ValueError("token_id is required")
        payload = self._get_json(CLOB_BOOK_URL, {"token_id": token_id})
        if not isinstance(payload, dict):
            raise OrderBookPayloadError("CLOB book response must be an object")
        return OrderBookSnapshot.from_clob_payload(token_id, payload)
