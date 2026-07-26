"""Reproducible backfill inputs for an already-settled daily equity market."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
import json
from typing import Mapping
from zoneinfo import ZoneInfo

from .clob_history import ClobPriceHistoryClient, PriceHistoryPoint
from .equity_contracts import DailyEquityCloseContract
from .market_discovery import MarketCandidate
from .pyth_benchmarks import PythBenchmarkPrice, PythBenchmarksClient
from .trading_calendar import previous_nyse_trading_day
from .yahoo_data import YahooChartClient


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SettledMarketDataFiles:
    market_id: str
    symbol: str
    price_to_beat: PythBenchmarkPrice
    final_price: PythBenchmarkPrice
    daily_closes_csv: Path
    intraday_spots_csv: Path
    reference_prices_json: Path
    up_clob_history_csv: Path
    down_clob_history_csv: Path
    daily_close_rows: int
    intraday_spot_rows: int
    up_clob_rows: int
    down_clob_rows: int

    def as_payload(self) -> Mapping[str, object]:
        return {
            "market_id": self.market_id,
            "symbol": self.symbol,
            "price_to_beat": self.price_to_beat.as_payload(),
            "final_price": self.final_price.as_payload(),
            "daily_closes_csv": str(self.daily_closes_csv),
            "intraday_spots_csv": str(self.intraday_spots_csv),
            "reference_prices_json": str(self.reference_prices_json),
            "up_clob_history_csv": str(self.up_clob_history_csv),
            "down_clob_history_csv": str(self.down_clob_history_csv),
            "daily_close_rows": self.daily_close_rows,
            "intraday_spot_rows": self.intraday_spot_rows,
            "up_clob_rows": self.up_clob_rows,
            "down_clob_rows": self.down_clob_rows,
            "spot_provider": "YAHOO_CHART_NON_SETTLEMENT",
            "reference_provider": "PYTH_BENCHMARKS",
        }


def backfill_settled_market_data(
    *, candidate: MarketCandidate, contract: DailyEquityCloseContract, output_dir: Path,
    lookback_calendar_days: int = 45, pyth_client: PythBenchmarksClient | None = None,
    yahoo_client: YahooChartClient | None = None, clob_client: ClobPriceHistoryClient | None = None,
) -> SettledMarketDataFiles:
    """Write Pyth references and Yahoo research inputs for one settled contract."""

    if lookback_calendar_days < 25:
        raise ValueError("lookback_calendar_days must be at least 25")
    resolves_at = contract.resolves_at.astimezone(UTC)
    market_day = resolves_at.astimezone(NEW_YORK).date()
    prior_day = previous_nyse_trading_day(market_day)
    pyth = pyth_client or PythBenchmarksClient()
    yahoo = yahoo_client or YahooChartClient()
    clob = clob_client or ClobPriceHistoryClient()
    feed_id = pyth.equity_feed_id(contract.symbol)
    price_to_beat = pyth.price_at(
        symbol=contract.symbol, feed_id=feed_id, observed_at=_regular_close_at(prior_day),
    )
    final_price = pyth.price_at(symbol=contract.symbol, feed_id=feed_id, observed_at=resolves_at)
    daily = yahoo.daily_closes(
        contract.symbol, start_date=market_day - timedelta(days=lookback_calendar_days), end_date=market_day,
    )
    intraday = yahoo.intraday_spots(
        contract.symbol, start_at=_regular_open_at(market_day), end_at=resolves_at,
    )
    history_start = datetime.combine(market_day, time.min, tzinfo=NEW_YORK).astimezone(UTC)
    up_history = clob.prices_history(candidate.outcome_a_token_id, start_at=history_start, end_at=resolves_at)
    down_history = clob.prices_history(candidate.outcome_b_token_id, start_at=history_start, end_at=resolves_at)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{candidate.market_id}_{contract.symbol}_{market_day.isoformat()}"
    daily_path = output_dir / f"{stem}_daily.csv"
    intraday_path = output_dir / f"{stem}_intraday.csv"
    references_path = output_dir / f"{stem}_pyth_references.json"
    up_clob_path = output_dir / f"{stem}_up_clob.csv"
    down_clob_path = output_dir / f"{stem}_down_clob.csv"
    daily.write_csv(daily_path)
    intraday.write_csv(intraday_path)
    _write_clob_history_csv(up_clob_path, up_history)
    _write_clob_history_csv(down_clob_path, down_history)
    references_path.write_text(json.dumps({
        "market_id": candidate.market_id,
        "symbol": contract.symbol,
        "market_day": market_day.isoformat(),
        "pyth_feed": contract.pyth_feed,
        "price_to_beat": price_to_beat.as_payload(),
        "final_price": final_price.as_payload(),
        "settlement_source": "PYTH_BENCHMARKS",
        "spot_source": "YAHOO_CHART_NON_SETTLEMENT",
    }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return SettledMarketDataFiles(
        candidate.market_id, contract.symbol, price_to_beat, final_price, daily_path, intraday_path,
        references_path, up_clob_path, down_clob_path, len(daily.closes), len(intraday.points), len(up_history), len(down_history),
    )


def _regular_open_at(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=NEW_YORK).astimezone(UTC)


def _regular_close_at(day: date) -> datetime:
    return datetime.combine(day, time(16), tzinfo=NEW_YORK).astimezone(UTC)


def _write_clob_history_csv(path: Path, points: tuple[PriceHistoryPoint, ...]) -> None:
    path.write_text(
        "DateTime,Price\n" + "".join(f"{point.observed_at.isoformat()},{point.price}\n" for point in points),
        encoding="utf-8",
    )
