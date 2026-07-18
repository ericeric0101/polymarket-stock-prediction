"""Provider-independent realized-volatility fallback for shadow research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import log, sqrt
from pathlib import Path
import csv

from .edge import EdgeAssessment, assess_buy_edge
from .pricing import digital_up_probability


TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class DailyClose:
    date: str
    close: float


def load_daily_closes_csv(path: Path) -> list[DailyClose]:
    """Load a portable Date,Close CSV exported from any verified data provider."""

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "Date" not in reader.fieldnames or "Close" not in reader.fieldnames:
            raise ValueError("CSV must contain Date and Close columns")
        closes = [DailyClose(row["Date"], float(row["Close"])) for row in reader if row.get("Close")]
    if len(closes) < 3 or any(close.close <= 0 for close in closes):
        raise ValueError("CSV requires at least three positive daily closes")
    return closes


def annualized_realized_volatility(closes: list[DailyClose], lookback_days: int = 20) -> float:
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2")
    sample = closes[-(lookback_days + 1):]
    if len(sample) < lookback_days + 1:
        raise ValueError("insufficient daily closes for requested lookback")
    returns = [log(current.close / previous.close) for previous, current in zip(sample, sample[1:])]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return sqrt(variance * TRADING_DAYS_PER_YEAR)


def daily_close_data_is_fresh(closes: list[DailyClose], now: datetime, maximum_age_days: int = 4) -> bool:
    if now.tzinfo is None or maximum_age_days < 0:
        raise ValueError("now must be timezone-aware and maximum_age_days non-negative")
    try:
        latest_date = datetime.fromisoformat(closes[-1].date).date()
    except ValueError as error:
        raise ValueError("latest Date must be ISO-8601") from error
    return latest_date >= (now - timedelta(days=maximum_age_days)).date()


@dataclass(frozen=True)
class BaselineAssessment:
    fair_up_probability: float
    annualized_realized_volatility: float
    prior_close: float
    up_edge: EdgeAssessment
    down_edge: EdgeAssessment
    data_is_fresh: bool
    model_error_buffer: float

    @property
    def paper_outcome(self) -> str | None:
        if not self.data_is_fresh:
            return None
        choices = (("UP", self.up_edge), ("DOWN", self.down_edge))
        eligible = [choice for choice in choices if choice[1].should_record_paper_trade]
        return max(eligible, key=lambda choice: choice[1].edge)[0] if eligible else None


def evaluate_realized_vol_baseline(
    *,
    spot: float,
    closes: list[DailyClose],
    seconds_to_resolution: float,
    up_ask: float,
    down_ask: float,
    fee_rate: float,
    slippage: float,
    base_model_error_buffer: float,
    fallback_buffer: float,
    minimum_edge: float,
    data_is_fresh: bool,
    lookback_days: int = 20,
) -> BaselineAssessment:
    if spot <= 0 or seconds_to_resolution <= 0:
        raise ValueError("spot and seconds_to_resolution must be positive")
    volatility = annualized_realized_volatility(closes, lookback_days)
    prior_close = closes[-1].close
    fair_up = digital_up_probability(spot, prior_close, volatility, seconds_to_resolution)
    model_error_buffer = base_model_error_buffer + fallback_buffer
    return BaselineAssessment(
        fair_up_probability=fair_up,
        annualized_realized_volatility=volatility,
        prior_close=prior_close,
        up_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="YES", executable_ask=up_ask, fee_rate=fee_rate, slippage=slippage, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        down_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="NO", executable_ask=down_ask, fee_rate=fee_rate, slippage=slippage, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        data_is_fresh=data_is_fresh,
        model_error_buffer=model_error_buffer,
    )
