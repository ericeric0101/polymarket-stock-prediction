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

from .baseline import DailyClose
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
