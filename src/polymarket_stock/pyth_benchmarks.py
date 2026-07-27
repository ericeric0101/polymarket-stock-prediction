"""Read-only Pyth Benchmarks prices for historical settlement research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from .http import get_json

PYTH_FEEDS_URL = "https://hermes.pyth.network/v2/price_feeds"
PYTH_BENCHMARKS_URL = "https://benchmarks.pyth.network/v1/updates/price"


class PythPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class PythBenchmarkPrice:
    symbol: str
    feed_id: str
    requested_at: datetime
    published_at: datetime
    price: float
    confidence: float
    exponent: int
    provider: str = "PYTH_BENCHMARKS"

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["requested_at"] = self.requested_at.isoformat()
        payload["published_at"] = self.published_at.isoformat()
        return payload


class PythBenchmarksClient:
    """Resolve Pyth US-equity feeds and retrieve an official historical value."""

    def __init__(self, get_json_fn: Callable[..., object] = get_json, api_key: str = "") -> None:
        self._get_json = get_json_fn
        self._api_key = api_key.strip()

    def _request_json(self, url: str, params: Mapping[str, object]) -> object:
        if self._api_key:
            return self._get_json(url, params, headers={"Authorization": f"Bearer {self._api_key}"})
        return self._get_json(url, params)

    def equity_feed_id(self, symbol: str) -> str:
        ticker = symbol.upper().strip()
        if not ticker:
            raise ValueError("symbol is required")
        payload = self._request_json(PYTH_FEEDS_URL, {"query": ticker})
        if not isinstance(payload, list):
            raise PythPayloadError("Pyth feed lookup must return a list")
        expected_symbol = f"Equity.US.{ticker}/USD"
        for item in payload:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            attributes = item.get("attributes")
            if isinstance(attributes, Mapping) and attributes.get("symbol") == expected_symbol:
                return str(item["id"])
        raise PythPayloadError(f"Pyth does not expose expected feed {expected_symbol}")

    def price_at(self, *, symbol: str, feed_id: str, observed_at: datetime, maximum_delay_seconds: int = 60) -> PythBenchmarkPrice:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if maximum_delay_seconds < 0:
            raise ValueError("maximum_delay_seconds must be non-negative")
        requested_at = observed_at.astimezone(UTC)
        payload = self._request_json(f"{PYTH_BENCHMARKS_URL}/{int(requested_at.timestamp())}", {"ids": feed_id})
        try:
            parsed = payload["parsed"]
        except (KeyError, TypeError) as error:
            raise PythPayloadError("Pyth benchmarks response is missing parsed prices") from error
        if not isinstance(parsed, list):
            raise PythPayloadError("Pyth benchmarks parsed prices must be a list")
        for item in parsed:
            if not isinstance(item, Mapping) or str(item.get("id", "")).lower() != feed_id.lower():
                continue
            try:
                quote = item["price"]
                raw_price = int(quote["price"])
                raw_confidence = int(quote["conf"])
                exponent = int(quote["expo"])
                published_at = datetime.fromtimestamp(int(quote["publish_time"]), tz=UTC)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise PythPayloadError("Pyth benchmarks price is invalid") from error
            delay = (published_at - requested_at).total_seconds()
            if delay < 0 or delay > maximum_delay_seconds:
                raise PythPayloadError(f"Pyth benchmark timestamp is outside requested window: {delay:.3f}s")
            scale = 10 ** exponent
            price = raw_price * scale
            confidence = raw_confidence * scale
            if price <= 0 or confidence < 0:
                raise PythPayloadError("Pyth benchmarks returned an invalid price")
            return PythBenchmarkPrice(symbol.upper(), feed_id.lower(), requested_at, published_at, price, confidence, exponent)
        raise PythPayloadError("Pyth benchmarks response did not contain requested feed")
