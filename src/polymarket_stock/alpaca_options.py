"""Alpaca free-tier option quotes, deliberately fixed to the Indicative feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from typing import Callable, Mapping

from .http import get_json


ALPACA_OPTIONS_LATEST_QUOTES_URL = "https://data.alpaca.markets/v1beta1/options/quotes/latest"
ALPACA_INDICATIVE_FEED = "indicative"


class AlpacaConfigurationError(ValueError):
    """Raised when read-only Alpaca market-data credentials are unavailable."""


class AlpacaPayloadError(ValueError):
    """Raised when Alpaca does not return a valid option quote payload."""


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key_id: str
    api_secret_key: str

    @classmethod
    def from_environment(cls) -> "AlpacaCredentials":
        api_key_id = os.getenv("ALPACA_API_KEY_ID", "").strip()
        api_secret_key = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
        if not api_key_id or not api_secret_key:
            raise AlpacaConfigurationError(
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are required for Alpaca data"
            )
        return cls(api_key_id=api_key_id, api_secret_key=api_secret_key)


@dataclass(frozen=True)
class IndicativeOptionQuote:
    symbol: str
    bid_price: float
    ask_price: float
    observed_at: datetime
    feed: str = ALPACA_INDICATIVE_FEED
    quality_label: str = "INDICATIVE_DELAYED_OR_MODIFIED_NOT_LIVE_GRADE"

    @classmethod
    def from_mapping(cls, symbol: str, payload: Mapping[str, object], observed_at: datetime | None = None) -> "IndicativeOptionQuote":
        bid_value = payload.get("bp", payload.get("b"))
        ask_value = payload.get("ap", payload.get("a"))
        try:
            bid_price = float(bid_value)
            ask_price = float(ask_value)
        except (TypeError, ValueError) as error:
            raise AlpacaPayloadError(f"{symbol} quote requires bid and ask prices") from error
        if bid_price < 0 or ask_price <= 0 or bid_price > ask_price:
            raise AlpacaPayloadError(f"{symbol} quote is crossed or outside valid bounds")
        timestamp = observed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise AlpacaPayloadError("observed_at must be timezone-aware")
        return cls(symbol=symbol, bid_price=bid_price, ask_price=ask_price, observed_at=timestamp)


class AlpacaIndicativeOptionsClient:
    """Read-only API client that cannot request OPRA or submit trading requests."""

    def __init__(self, credentials: AlpacaCredentials, get_json_fn: Callable[..., object] = get_json) -> None:
        self._credentials = credentials
        self._get_json = get_json_fn

    def latest_quotes(self, symbols: tuple[str, ...]) -> list[IndicativeOptionQuote]:
        if not symbols or len(symbols) > 100:
            raise ValueError("provide between 1 and 100 option symbols")
        if any(not symbol.strip() for symbol in symbols):
            raise ValueError("option symbols cannot be empty")
        response = self._get_json(
            ALPACA_OPTIONS_LATEST_QUOTES_URL,
            {"symbols": ",".join(symbols), "feed": ALPACA_INDICATIVE_FEED},
            headers={
                "APCA-API-KEY-ID": self._credentials.api_key_id,
                "APCA-API-SECRET-KEY": self._credentials.api_secret_key,
            },
        )
        if not isinstance(response, dict) or not isinstance(response.get("quotes"), dict):
            raise AlpacaPayloadError("Alpaca latest-quotes response requires a quotes object")
        quotes: list[IndicativeOptionQuote] = []
        for symbol in symbols:
            payload = response["quotes"].get(symbol)
            if not isinstance(payload, dict):
                continue
            quotes.append(IndicativeOptionQuote.from_mapping(symbol, payload))
        return quotes
