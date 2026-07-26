"""Resumable Pyth, CLOB, and settlement backfill for discovered closed markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
import json
import time as time_module
from typing import Mapping
from zoneinfo import ZoneInfo

from .clob_history import ClobPriceHistoryClient, PriceHistoryPoint
from .market_discovery import GammaMarketClient
from .pyth_benchmarks import PythBenchmarksClient
from .trading_calendar import previous_nyse_trading_day


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BatchBackfillReport:
    requested: int
    completed: int
    skipped: int
    failed: int
    failures: tuple[Mapping[str, object], ...]

    def as_payload(self) -> Mapping[str, object]:
        return {"requested": self.requested, "completed": self.completed, "skipped": self.skipped, "failed": self.failed, "failures": list(self.failures)}


def backfill_discovered_markets(*, discovery_path: Path, output_dir: Path, start_offset: int = 0, maximum_markets: int | None = None, pause_seconds: float = 0.2, pyth_pause_seconds: float = 2.0) -> BatchBackfillReport:
    """Fetch only Pyth references, CLOB histories, and official Gamma settlement."""

    if pause_seconds < 0:
        raise ValueError("pause_seconds must be non-negative")
    if start_offset < 0:
        raise ValueError("start_offset must be non-negative")
    if pyth_pause_seconds < 0:
        raise ValueError("pyth_pause_seconds must be non-negative")
    payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("discovery JSON must be a list")
    items = [item for item in payload if isinstance(item, Mapping) and item.get("status") == "FOUND"]
    items = items[start_offset:]
    if maximum_markets is not None:
        if maximum_markets < 1:
            raise ValueError("maximum_markets must be positive")
        items = items[:maximum_markets]
    output_dir.mkdir(parents=True, exist_ok=True)
    gamma = GammaMarketClient()
    pyth = PythBenchmarksClient()
    clob = ClobPriceHistoryClient()
    feed_ids: dict[str, str] = {}
    price_cache = {}
    completed = skipped = 0
    failures: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        market_id = str(item.get("market_id", ""))
        symbol = str(item.get("symbol", "")).upper()
        market_day = str(item.get("market_day", ""))
        stem = f"{market_id}_{symbol}_{market_day}"
        settlement_path = output_dir / f"{stem}_settlement.json"
        if settlement_path.exists():
            skipped += 1
            continue
        try:
            _backfill_one(item=item, output_dir=output_dir, gamma=gamma, pyth=pyth, clob=clob, feed_ids=feed_ids, price_cache=price_cache, pyth_pause_seconds=pyth_pause_seconds)
            completed += 1
        except Exception as error:
            failures.append({"market_id": market_id, "symbol": symbol, "market_day": market_day, "error": str(error)})
        if pause_seconds and index + 1 < len(items):
            time_module.sleep(pause_seconds)
    return BatchBackfillReport(len(items), completed, skipped, len(failures), tuple(failures))


def _backfill_one(*, item: Mapping[str, object], output_dir: Path, gamma: GammaMarketClient, pyth: PythBenchmarksClient, clob: ClobPriceHistoryClient, feed_ids: dict[str, str], price_cache: dict[tuple[str, datetime], object], pyth_pause_seconds: float) -> None:
    market_id = str(item["market_id"])
    symbol = str(item["symbol"]).upper()
    market_day = datetime.fromisoformat(str(item["market_day"])).date()
    end_at = datetime.fromisoformat(str(item["end_date"]).replace("Z", "+00:00")).astimezone(UTC)
    token_ids = _string_list(item.get("clob_token_ids"), "clob_token_ids")
    outcomes = _string_list(item.get("outcomes"), "outcomes")
    if len(token_ids) != 2 or tuple(label.upper() for label in outcomes) != ("UP", "DOWN"):
        raise ValueError("discovery item is not an Up/Down binary CLOB market")
    settlement = gamma.get_market_settlement(market_id)
    winning_outcome = settlement.winning_outcome.upper() if settlement.winning_outcome else None
    if not settlement.closed or winning_outcome not in {"UP", "DOWN"}:
        raise ValueError("Gamma does not provide a closed single-outcome settlement")
    feed_id = feed_ids.get(symbol)
    if feed_id is None:
        feed_id = pyth.equity_feed_id(symbol)
        feed_ids[symbol] = feed_id
    prior_day = previous_nyse_trading_day(market_day)
    price_to_beat = _cached_pyth_price(pyth, price_cache, symbol, feed_id, _close_at(prior_day), pyth_pause_seconds)
    final_price = _cached_pyth_price(pyth, price_cache, symbol, feed_id, end_at, pyth_pause_seconds)
    history_start = datetime.combine(market_day, time.min, tzinfo=NEW_YORK).astimezone(UTC)
    up_history = clob.prices_history(token_ids[0], start_at=history_start, end_at=end_at)
    down_history = clob.prices_history(token_ids[1], start_at=history_start, end_at=end_at)
    stem = f"{market_id}_{symbol}_{market_day.isoformat()}"
    _write_history(output_dir / f"{stem}_up_clob.csv", up_history)
    _write_history(output_dir / f"{stem}_down_clob.csv", down_history)
    (output_dir / f"{stem}_pyth_references.json").write_text(json.dumps({
        "market_id": market_id, "symbol": symbol, "market_day": market_day.isoformat(),
        "price_to_beat": price_to_beat.as_payload(), "final_price": final_price.as_payload(),
        "settlement_source": "PYTH_BENCHMARKS",
    }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (output_dir / f"{stem}_settlement.json").write_text(json.dumps({
        "market_id": market_id, "winning_outcome": winning_outcome,
        "provider": "POLYMARKET_GAMMA", "payload": settlement.raw_payload,
    }, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def _close_at(day: datetime.date) -> datetime:
    return datetime.combine(day, time(16), tzinfo=NEW_YORK).astimezone(UTC)


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON list of strings")
    return tuple(value)


def _write_history(path: Path, points: tuple[PriceHistoryPoint, ...]) -> None:
    path.write_text("DateTime,Price\n" + "".join(f"{item.observed_at.isoformat()},{item.price}\n" for item in points), encoding="utf-8")


def _cached_pyth_price(pyth: PythBenchmarksClient, cache: dict[tuple[str, datetime], object], symbol: str, feed_id: str, observed_at: datetime, pause_seconds: float):
    key = (symbol, observed_at)
    cached = cache.get(key)
    if cached is not None:
        return cached
    value = pyth.price_at(symbol=symbol, feed_id=feed_id, observed_at=observed_at)
    cache[key] = value
    if pause_seconds:
        time_module.sleep(pause_seconds)
    return value
