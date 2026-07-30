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


@dataclass(frozen=True)
class DailyBar:
    """Verified non-settlement OHLC bar used by research volatility estimators."""

    date: str
    open: float
    high: float
    low: float
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


def load_daily_bars_csv(path: Path) -> list[DailyBar]:
    """Load a portable Date,Open,High,Low,Close research CSV."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"Date", "Open", "High", "Low", "Close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV must contain Date, Open, High, Low, and Close columns")
        bars = [
            DailyBar(
                row["Date"], float(row["Open"]), float(row["High"]),
                float(row["Low"]), float(row["Close"]),
            )
            for row in reader
            if row.get("Close")
        ]
    if len(bars) < 3 or any(
        bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0
        or bar.low > bar.high or bar.open > bar.high or bar.open < bar.low
        or bar.close > bar.high or bar.close < bar.low
        for bar in bars
    ):
        raise ValueError("CSV requires at least three valid positive OHLC bars")
    return bars


def annualized_realized_volatility(closes: list[DailyClose], lookback_days: int = 20) -> float:
    return annualized_volatility(closes, lookback_days=lookback_days, estimator="CLOSE_TO_CLOSE")


def annualized_volatility(
    observations: list[DailyClose] | list[DailyBar],
    *,
    lookback_days: int = 20,
    estimator: str = "CLOSE_TO_CLOSE",
    decay: float = 0.94,
) -> float:
    """Calculate annualized volatility for shadow research.

    ``CLOSE_TO_CLOSE`` preserves the original baseline. ``EWMA`` uses zero-mean
    RiskMetrics-style weighting. The OHLC estimators are opt-in until their
    out-of-sample performance is validated.
    """
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2")
    estimator = estimator.upper()
    if estimator not in {"CLOSE_TO_CLOSE", "EWMA", "GARMAN_KLASS", "YANG_ZHANG"}:
        raise ValueError(f"unknown volatility estimator: {estimator}")
    if not 0 < decay < 1:
        raise ValueError("decay must be between zero and one")
    sample = observations[-(lookback_days + 1):]
    if len(sample) < lookback_days + 1:
        raise ValueError("insufficient daily observations for requested lookback")
    if estimator in {"GARMAN_KLASS", "YANG_ZHANG"} and not all(isinstance(item, DailyBar) for item in sample):
        raise ValueError(f"{estimator} requires DailyBar observations")
    if estimator == "GARMAN_KLASS":
        bars = sample[1:]
        values = [
            0.5 * log(bar.high / bar.low) ** 2
            - (2 * log(2) - 1) * log(bar.close / bar.open) ** 2
            for bar in bars
        ]
        return sqrt(max(sum(values) / len(values), 0.0) * TRADING_DAYS_PER_YEAR)
    if estimator == "YANG_ZHANG":
        bars = sample
        overnight = [log(current.open / previous.close) for previous, current in zip(bars, bars[1:])]
        open_to_close = [log(bar.close / bar.open) for bar in bars[1:]]
        rogers_satchell = [
            log(bar.high / bar.close) * log(bar.high / bar.open)
            + log(bar.low / bar.close) * log(bar.low / bar.open)
            for bar in bars[1:]
        ]
        k = 0.34 / (1.34 + (len(overnight) + 1) / (len(overnight) - 1))
        variance = _sample_variance(overnight) + k * _sample_variance(open_to_close)
        variance += (1 - k) * sum(rogers_satchell) / len(rogers_satchell)
        return sqrt(max(variance, 0.0) * TRADING_DAYS_PER_YEAR)
    returns = [log(current.close / previous.close) for previous, current in zip(sample, sample[1:])]
    if estimator == "EWMA":
        variance = 0.0
        for value in returns:
            variance = decay * variance + (1 - decay) * value**2
        return sqrt(variance * TRADING_DAYS_PER_YEAR)
    return sqrt(_sample_variance(returns) * TRADING_DAYS_PER_YEAR)


def _sample_variance(values: list[float]) -> float:
    if len(values) < 2:
        raise ValueError("at least two observations are required")
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


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
    volatility_estimator: str
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
    up_fee_rate: float,
    down_fee_rate: float,
    base_model_error_buffer: float,
    fallback_buffer: float,
    minimum_edge: float,
    data_is_fresh: bool,
    lookback_days: int = 20,
    volatility_estimator: str = "CLOSE_TO_CLOSE",
    volatility_decay: float = 0.94,
    volatility_observations: list[DailyClose] | list[DailyBar] | None = None,
    annualized_volatility_override: float | None = None,
    additional_model_error_buffer: float = 0.0,
    price_to_beat_override: float | None = None,
) -> BaselineAssessment:
    if spot <= 0 or seconds_to_resolution <= 0:
        raise ValueError("spot and seconds_to_resolution must be positive")
    if additional_model_error_buffer < 0:
        raise ValueError("additional_model_error_buffer cannot be negative")
    if price_to_beat_override is not None and price_to_beat_override <= 0:
        raise ValueError("price_to_beat_override must be positive")
    observations = volatility_observations if volatility_observations is not None else closes
    realized_volatility = annualized_volatility(
        observations,
        lookback_days=lookback_days,
        estimator=volatility_estimator,
        decay=volatility_decay,
    )
    volatility = annualized_volatility_override if annualized_volatility_override is not None else realized_volatility
    if volatility <= 0:
        raise ValueError("annualized_volatility_override must be positive")
    prior_close = price_to_beat_override if price_to_beat_override is not None else closes[-1].close
    fair_up = digital_up_probability(spot, prior_close, volatility, seconds_to_resolution)
    model_error_buffer = base_model_error_buffer + fallback_buffer + additional_model_error_buffer
    return BaselineAssessment(
        fair_up_probability=fair_up,
        annualized_realized_volatility=volatility,
        volatility_estimator=volatility_estimator.upper(),
        prior_close=prior_close,
        up_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="YES", executable_ask=up_ask, fee_rate=up_fee_rate, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        down_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="NO", executable_ask=down_ask, fee_rate=down_fee_rate, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        data_is_fresh=data_is_fresh,
        model_error_buffer=model_error_buffer,
    )
