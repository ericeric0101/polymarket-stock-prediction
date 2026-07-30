"""Yahoo chart daily closes for offline research backfills.

This is a non-settlement source. Polymarket daily equity markets resolve from
Pyth or the fallback described in each market's rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
import csv
from typing import Callable, Mapping

from .baseline import DailyBar, DailyClose
from .http import get_json


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


class YahooPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class YahooDailyCloseSeries:
    symbol: str
    closes: tuple[DailyClose, ...]
    provider: str = "YAHOO_CHART_NON_SETTLEMENT"

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("Date", "Close"))
            writer.writeheader()
            for close in self.closes:
                writer.writerow({"Date": close.date, "Close": close.close})


@dataclass(frozen=True)
class YahooDailyBarSeries:
    symbol: str
    bars: tuple[DailyBar, ...]
    provider: str = "YAHOO_CHART_NON_SETTLEMENT"

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("Date", "Open", "High", "Low", "Close"))
            writer.writeheader()
            for bar in self.bars:
                writer.writerow({"Date": bar.date, "Open": bar.open, "High": bar.high, "Low": bar.low, "Close": bar.close})


@dataclass(frozen=True)
class YahooIntradaySpotSeries:
    symbol: str
    points: tuple[tuple[datetime, float], ...]
    provider: str = "YAHOO_CHART_NON_SETTLEMENT"

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("DateTime", "Spot"))
            writer.writeheader()
            for observed_at, spot in self.points:
                writer.writerow({"DateTime": observed_at.isoformat(), "Spot": spot})


class YahooChartClient:
    def __init__(self, get_json_fn: Callable[..., object] = get_json) -> None:
        self._get_json = get_json_fn

    def daily_closes(self, symbol: str, *, start_date: date, end_date: date) -> YahooDailyCloseSeries:
        if not symbol.strip():
            raise ValueError("symbol is required")
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        start_at = datetime.combine(start_date, time.min, tzinfo=UTC)
        # Yahoo period2 is exclusive; include the requested end date.
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
        response = self._get_json(
            f"{YAHOO_CHART_URL}/{symbol.upper()}",
            {
                "period1": int(start_at.timestamp()),
                "period2": int(end_at.timestamp()),
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "false",
            },
        )
        return YahooDailyCloseSeries(symbol.upper(), _parse_chart_response(response))

    def daily_bars(self, symbol: str, *, start_date: date, end_date: date) -> YahooDailyBarSeries:
        if not symbol.strip():
            raise ValueError("symbol is required")
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        start_at = datetime.combine(start_date, time.min, tzinfo=UTC)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
        response = self._get_json(
            f"{YAHOO_CHART_URL}/{symbol.upper()}",
            {
                "period1": int(start_at.timestamp()),
                "period2": int(end_at.timestamp()),
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "false",
            },
        )
        return YahooDailyBarSeries(symbol.upper(), _parse_bar_chart_response(response))

    def intraday_spots(self, symbol: str, *, start_at: datetime, end_at: datetime) -> YahooIntradaySpotSeries:
        if not symbol.strip():
            raise ValueError("symbol is required")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("intraday timestamps must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be after start_at")
        if end_at - start_at > timedelta(days=8):
            raise ValueError("Yahoo intraday request must cover at most eight days")
        response = self._get_json(
            f"{YAHOO_CHART_URL}/{symbol.upper()}",
            {
                "period1": int(start_at.timestamp()), "period2": int(end_at.timestamp()),
                "interval": "1m", "events": "history", "includePrePost": "false",
            },
        )
        return YahooIntradaySpotSeries(symbol.upper(), _parse_intraday_chart_response(response, start_at, end_at))


def _parse_chart_response(payload: object) -> tuple[DailyClose, ...]:
    try:
        result = payload["chart"]["result"][0]  # type: ignore[index]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as error:
        raise YahooPayloadError("Yahoo chart response is invalid") from error
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise YahooPayloadError("Yahoo chart timestamps and closes must be same-length arrays")
    values = []
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, (int, float)) or not isinstance(close, (int, float)) or close <= 0:
            continue
        values.append(DailyClose(datetime.fromtimestamp(float(timestamp), tz=UTC).date().isoformat(), float(close)))
    if not values:
        raise YahooPayloadError("Yahoo chart response has no usable closes")
    return tuple(values)


def _parse_bar_chart_response(payload: object) -> tuple[DailyBar, ...]:
    try:
        result = payload["chart"]["result"][0]  # type: ignore[index]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        opens, highs, lows, closes = quote["open"], quote["high"], quote["low"], quote["close"]
    except (KeyError, IndexError, TypeError) as error:
        raise YahooPayloadError("Yahoo chart OHLC response is invalid") from error
    arrays = (timestamps, opens, highs, lows, closes)
    if not all(isinstance(values, list) for values in arrays) or len({len(values) for values in arrays}) != 1:
        raise YahooPayloadError("Yahoo chart OHLC arrays must be same-length lists")
    bars = []
    for timestamp, open_, high, low, close in zip(timestamps, opens, highs, lows, closes):
        values = (open_, high, low, close)
        if not isinstance(timestamp, (int, float)) or not all(isinstance(value, (int, float)) and value > 0 for value in values):
            continue
        if low > high or not low <= open_ <= high or not low <= close <= high:
            continue
        bars.append(DailyBar(datetime.fromtimestamp(float(timestamp), tz=UTC).date().isoformat(), float(open_), float(high), float(low), float(close)))
    if not bars:
        raise YahooPayloadError("Yahoo chart response has no usable OHLC bars")
    return tuple(bars)


def _parse_intraday_chart_response(payload: object, start_at: datetime, end_at: datetime) -> tuple[tuple[datetime, float], ...]:
    try:
        result = payload["chart"]["result"][0]  # type: ignore[index]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as error:
        raise YahooPayloadError("Yahoo intraday chart response is invalid") from error
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise YahooPayloadError("Yahoo intraday timestamps and closes must be same-length arrays")
    points = []
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, (int, float)) or not isinstance(close, (int, float)) or close <= 0:
            continue
        observed_at = datetime.fromtimestamp(float(timestamp), tz=UTC)
        if start_at <= observed_at <= end_at:
            points.append((observed_at, float(close)))
    if not points:
        raise YahooPayloadError("Yahoo intraday chart response has no usable closes")
    return tuple(points)
