"""Authenticated Pyth Pro one-minute equity history for offline replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import csv
from typing import Callable

from .http import get_json


PYTH_HISTORY_URL = "https://pyth.dourolabs.app/v1/fixed_rate@200ms/history"


class PythHistoryError(ValueError):
    pass


@dataclass(frozen=True)
class PythIntradaySpotSeries:
    symbol: str
    points: tuple[tuple[datetime, float], ...]
    provider: str = "PYTH_PRO_HISTORY_FIXED_RATE_200MS"

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("DateTime", "Spot"))
            writer.writeheader()
            for observed_at, spot in self.points:
                writer.writerow({"DateTime": observed_at.isoformat(), "Spot": spot})


class PythHistoryClient:
    """Read one-minute OHLC closes from the authenticated Pyth Pro History API."""

    def __init__(self, api_key: str, get_json_fn: Callable[..., object] = get_json) -> None:
        if not api_key.strip():
            raise ValueError("Pyth Pro API key is required")
        self._api_key = api_key
        self._get_json = get_json_fn

    def intraday_spots(self, symbol: str, *, start_at: datetime, end_at: datetime) -> PythIntradaySpotSeries:
        if not symbol.strip():
            raise ValueError("symbol is required")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("intraday timestamps must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be after start_at")
        response = self._get_json(
            PYTH_HISTORY_URL,
            {
                "symbol": f"Equity.US.{symbol.upper()}/USD",
                "from": int(start_at.astimezone(UTC).timestamp()),
                "to": int(end_at.astimezone(UTC).timestamp()),
                "resolution": "1",
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        return PythIntradaySpotSeries(symbol.upper(), _parse_history_response(response, start_at, end_at))


def _parse_history_response(
    payload: object, start_at: datetime, end_at: datetime
) -> tuple[tuple[datetime, float], ...]:
    try:
        status = payload["s"]  # type: ignore[index]
        timestamps = payload["t"]  # type: ignore[index]
        closes = payload["c"]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise PythHistoryError("Pyth History response is invalid") from error
    if status != "ok":
        raise PythHistoryError(f"Pyth History returned status {status!r}")
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise PythHistoryError("Pyth History timestamps and closes must be same-length arrays")
    points = []
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, (int, float)) or not isinstance(close, (int, float)) or close <= 0:
            continue
        observed_at = datetime.fromtimestamp(float(timestamp), tz=UTC)
        if start_at <= observed_at <= end_at:
            points.append((observed_at, float(close)))
    if not points:
        raise PythHistoryError("Pyth History response has no usable one-minute candles")
    return tuple(points)
