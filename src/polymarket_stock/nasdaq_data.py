"""Public Nasdaq data adapter for a non-settlement baseline only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Callable

from .baseline import DailyClose
from .http import get_json


NASDAQ_URL = "https://api.nasdaq.com/api/quote"
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}


class NasdaqPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class NasdaqQuote:
    symbol: str
    price: float
    last_trade_at: datetime
    is_real_time: bool


class NasdaqBaselineClient:
    def __init__(self, get_json_fn: Callable[..., object] = get_json) -> None:
        self._get_json = get_json_fn

    def latest_quote(self, symbol: str) -> NasdaqQuote:
        payload = self._get_json(f"{NASDAQ_URL}/{symbol}/info", {"assetclass": "stocks"}, headers=NASDAQ_HEADERS)
        try:
            primary = payload["data"]["primaryData"]
            price = float(str(primary["lastSalePrice"]).replace("$", "").replace(",", ""))
            last_trade_at = datetime.strptime(primary["lastTradeTimestamp"], "%b %d, %Y").replace(tzinfo=UTC)
            is_real_time = bool(primary["isRealTime"])
        except (KeyError, TypeError, ValueError) as error:
            raise NasdaqPayloadError("Nasdaq quote response is missing a usable last price") from error
        return NasdaqQuote(symbol=symbol.upper(), price=price, last_trade_at=last_trade_at, is_real_time=is_real_time)

    def daily_closes(self, symbol: str, now: datetime, lookback_calendar_days: int = 400) -> list[DailyClose]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        from_date = (now - timedelta(days=lookback_calendar_days)).date().isoformat()
        to_date = now.date().isoformat()
        payload = self._get_json(
            f"{NASDAQ_URL}/{symbol}/historical",
            {"assetclass": "stocks", "fromdate": from_date, "todate": to_date, "limit": 5000},
            headers=NASDAQ_HEADERS,
        )
        try:
            rows = payload["data"]["tradesTable"]["rows"]
            closes = [
                DailyClose(
                    date=datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat(),
                    close=float(str(row["close"]).replace("$", "").replace(",", "")),
                )
                for row in reversed(rows)
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise NasdaqPayloadError("Nasdaq historical response is missing usable daily closes") from error
        if len(closes) < 21:
            raise NasdaqPayloadError("Nasdaq returned fewer than 21 daily closes")
        return closes


def save_baseline_cache(path: Path, quote: NasdaqQuote, closes: list[DailyClose]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "quote": {"symbol": quote.symbol, "price": quote.price, "last_trade_at": quote.last_trade_at.isoformat(), "is_real_time": quote.is_real_time},
                "closes": [{"date": close.date, "close": close.close} for close in closes],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def load_baseline_cache(path: Path) -> tuple[NasdaqQuote, list[DailyClose]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        quote_data = payload["quote"]
        quote = NasdaqQuote(
            symbol=str(quote_data["symbol"]), price=float(quote_data["price"]),
            last_trade_at=datetime.fromisoformat(quote_data["last_trade_at"]),
            is_real_time=bool(quote_data["is_real_time"]),
        )
        closes = [DailyClose(str(row["date"]), float(row["close"])) for row in payload["closes"]]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise NasdaqPayloadError("no usable cached Nasdaq baseline data") from error
    if len(closes) < 21 or quote.last_trade_at.tzinfo is None:
        raise NasdaqPayloadError("cached Nasdaq baseline data is incomplete")
    return quote, closes
