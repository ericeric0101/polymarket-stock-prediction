"""Resumable Pyth Pro one-minute spot backfill for discovered daily markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
import json
import time as time_module
from typing import Mapping
from zoneinfo import ZoneInfo

from .pyth_history import PythHistoryClient


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class IntradaySpotBackfillReport:
    requested: int
    completed: int
    skipped: int
    failed: int
    failures: tuple[Mapping[str, object], ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "requested": self.requested,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": list(self.failures),
        }


def backfill_pyth_intraday_spots(
    *,
    discovery_path: Path,
    output_dir: Path,
    api_key: str,
    symbols: tuple[str, ...] = ("NVDA", "TSLA"),
    pause_seconds: float = 0.25,
) -> IntradaySpotBackfillReport:
    """Write one `DateTime,Spot` file per settled market day, resuming on rerun."""

    if pause_seconds < 0:
        raise ValueError("pause_seconds must be non-negative")
    allowed_symbols = {symbol.upper().strip() for symbol in symbols if symbol.strip()}
    if not allowed_symbols:
        raise ValueError("at least one symbol is required")
    payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("discovery JSON must be a list")
    items = [
        item
        for item in payload
        if isinstance(item, Mapping)
        and item.get("status") == "FOUND"
        and str(item.get("symbol", "")).upper() in allowed_symbols
    ]
    items.sort(key=lambda item: (str(item.get("market_day", "")), str(item.get("market_id", ""))))
    output_dir.mkdir(parents=True, exist_ok=True)
    client = PythHistoryClient(api_key)
    completed = skipped = 0
    failures: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        market_id = str(item.get("market_id", ""))
        symbol = str(item.get("symbol", "")).upper()
        market_day = str(item.get("market_day", ""))
        path = output_dir / f"{market_id}_{symbol}_{market_day}_pyth_intraday.csv"
        if path.exists():
            skipped += 1
            continue
        try:
            day = datetime.fromisoformat(market_day).date()
            start_at = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK).astimezone(UTC)
            end_at = datetime.combine(day, time(16), tzinfo=NEW_YORK).astimezone(UTC)
            series = client.intraday_spots(symbol, start_at=start_at, end_at=end_at)
            series.write_csv(path)
            completed += 1
        except Exception as error:
            failures.append({"market_id": market_id, "symbol": symbol, "market_day": market_day, "error": str(error)})
        if pause_seconds and index + 1 < len(items):
            time_module.sleep(pause_seconds)
    return IntradaySpotBackfillReport(len(items), completed, skipped, len(failures), tuple(failures))
