"""Exact-close Pyth versus Finnhub calibration for Pyth-resolved daily contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from statistics import mean
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .trading_calendar import previous_nyse_trading_day
from .pyth_history import PythHistoryClient, PythHistoryError
from .streaming import SpotQuote

NEW_YORK = ZoneInfo("America/New_York")
CLOSE_WINDOW_START = time(15, 59)
CLOSE_WINDOW_END = time(16, 0)


@dataclass(frozen=True)
class CloseSourceCalibration:
    market_date: str
    symbol: str
    pyth_close: float | None
    pyth_close_at: datetime | None
    finnhub_close: float | None
    finnhub_observed_at: datetime | None
    pyth_live_close: float | None
    pyth_live_observed_at: datetime | None
    prior_pyth_close: float | None
    pyth_direction: str | None
    finnhub_direction: str | None
    direction_flipped: bool | None
    difference_bps: float | None
    status: str
    detail: str | None = None
    source_estimates: Mapping[str, float] = field(default_factory=dict)
    source_errors_bps: Mapping[str, float] = field(default_factory=dict)

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        for timestamp_field in ("pyth_close_at", "finnhub_observed_at", "pyth_live_observed_at"):
            value = payload[timestamp_field]
            payload[timestamp_field] = value.isoformat() if value else None
        return payload


@dataclass(frozen=True)
class CloseSourceCalibrationReport:
    market_date: str
    observations: tuple[CloseSourceCalibration, ...]

    def as_payload(self) -> Mapping[str, object]:
        complete = [item for item in self.observations if item.status == "COMPLETE"]
        diffs = sorted(abs(item.difference_bps) for item in complete if item.difference_bps is not None)
        return {
            "market_date": self.market_date,
            "symbols_requested": len(self.observations),
            "complete_symbols": len(complete),
            "direction_flips": sum(item.direction_flipped is True for item in complete),
            "mean_absolute_difference_bps": mean(diffs) if diffs else None,
            "max_absolute_difference_bps": max(diffs) if diffs else None,
            "observations": [item.as_payload() for item in self.observations],
        }


def calibrate_close_sources(
    *, client: PythHistoryClient, market_date: date, symbols: Iterable[str],
    finnhub_spots: Iterable[SpotQuote], pyth_live_spots: Iterable[SpotQuote] = (),
    supplemental_closes: Mapping[str, Mapping[str, float]] | None = None,
) -> CloseSourceCalibrationReport:
    """Compare exact Pyth 15:59 candle close with locally captured close-window quotes.

    The historical Pyth candle is the contract-rule truth.  Finnhub is deliberately
    measured only, never substituted into a settlement label.
    """
    finnhub_by_symbol = _group_by_symbol(finnhub_spots)
    pyth_live_by_symbol = _group_by_symbol(pyth_live_spots)
    supplemental_closes = supplemental_closes or {}
    observations = []
    for raw_symbol in sorted({item.strip().upper() for item in symbols if item.strip()}):
        finnhub = _latest_in_close_window(finnhub_by_symbol.get(raw_symbol, ()), market_date)
        pyth_live = _latest_in_close_window(pyth_live_by_symbol.get(raw_symbol, ()), market_date)
        if finnhub is None:
            observations.append(CloseSourceCalibration(
                market_date.isoformat(), raw_symbol, None, None, None, None,
                pyth_live.price if pyth_live else None, pyth_live.observed_at if pyth_live else None, None,
                None, None, None, None, "FINNHUB_CLOSE_UNAVAILABLE",
                "no locally captured Finnhub quote in the 15:59-16:00 ET window",
            ))
            continue
        try:
            pyth_close, pyth_close_at = official_pyth_final_minute_close(client, raw_symbol, market_date)
            prior_close, _ = official_pyth_final_minute_close(client, raw_symbol, previous_nyse_trading_day(market_date))
        except (PythHistoryError, OSError, ValueError) as error:
            observations.append(CloseSourceCalibration(
                market_date.isoformat(), raw_symbol, None, None, finnhub.price, finnhub.observed_at,
                pyth_live.price if pyth_live else None, pyth_live.observed_at if pyth_live else None, None,
                None, None, None, None, "PYTH_CLOSE_UNAVAILABLE", str(error),
            ))
            continue
        difference_bps = (finnhub.price - pyth_close) / pyth_close * 10_000
        source_estimates = {"FINNHUB_CLOSE_WINDOW": finnhub.price}
        for source, values in supplemental_closes.items():
            value = values.get(raw_symbol)
            if isinstance(value, (int, float)) and value > 0:
                source_estimates[str(source)] = float(value)
        source_errors = {source: (value - pyth_close) / pyth_close * 10_000 for source, value in source_estimates.items()}
        pyth_direction = _direction(pyth_close, prior_close)
        finnhub_direction = _direction(finnhub.price, prior_close)
        observations.append(CloseSourceCalibration(
            market_date.isoformat(), raw_symbol, pyth_close, pyth_close_at, finnhub.price, finnhub.observed_at,
            pyth_live.price if pyth_live else None, pyth_live.observed_at if pyth_live else None, prior_close,
            pyth_direction, finnhub_direction, pyth_direction != finnhub_direction, difference_bps, "COMPLETE",
            source_estimates=source_estimates, source_errors_bps=source_errors,
        ))
    return CloseSourceCalibrationReport(market_date.isoformat(), tuple(observations))


def official_pyth_final_minute_close(client: PythHistoryClient, symbol: str, market_date: date) -> tuple[float, datetime]:
    start = datetime.combine(market_date, CLOSE_WINDOW_START, tzinfo=NEW_YORK).astimezone(UTC)
    end = datetime.combine(market_date, CLOSE_WINDOW_END, tzinfo=NEW_YORK).astimezone(UTC)
    points = client.intraday_spots(symbol, start_at=start, end_at=end).points
    eligible = [point for point in points if point[0] <= end]
    if not eligible:
        raise PythHistoryError("Pyth History response has no final regular-session candle")
    return eligible[-1][1], eligible[-1][0]


def _group_by_symbol(items: Iterable[SpotQuote]) -> dict[str, list[SpotQuote]]:
    result: dict[str, list[SpotQuote]] = {}
    for item in items:
        result.setdefault(item.symbol.upper(), []).append(item)
    return result


def _latest_in_close_window(items: Iterable[SpotQuote], market_date: date) -> SpotQuote | None:
    start = datetime.combine(market_date, CLOSE_WINDOW_START, tzinfo=NEW_YORK).astimezone(UTC)
    end = datetime.combine(market_date, CLOSE_WINDOW_END, tzinfo=NEW_YORK).astimezone(UTC)
    eligible = [item for item in items if start <= item.observed_at.astimezone(UTC) <= end]
    return max(eligible, key=lambda item: item.observed_at) if eligible else None


def _direction(price: float, threshold: float) -> str:
    if price > threshold:
        return "UP"
    if price < threshold:
        return "DOWN"
    return "FIFTY_FIFTY"
