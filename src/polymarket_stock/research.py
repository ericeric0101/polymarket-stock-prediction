"""Phase 2 fair-probability research for daily Up/Down markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, log, sqrt
from typing import Literal

from .edge import EdgeAssessment, assess_buy_edge
from .pricing import SECONDS_PER_YEAR, digital_up_probability, normal_cdf


OptionType = Literal["call", "put"]


@dataclass(frozen=True)
class VolatilityRegime:
    overnight_annual: float
    regular_annual: float

    def blended_annual(self, overnight_seconds: float, regular_seconds: float) -> float:
        if overnight_seconds < 0 or regular_seconds < 0:
            raise ValueError("session seconds cannot be negative")
        total = overnight_seconds + regular_seconds
        if total <= 0:
            raise ValueError("at least one session duration is required")
        if self.overnight_annual <= 0 or self.regular_annual <= 0:
            raise ValueError("annual volatilities must be positive")
        variance = (
            self.overnight_annual**2 * overnight_seconds
            + self.regular_annual**2 * regular_seconds
        ) / total
        return sqrt(variance)


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    option_type: OptionType
    strike: float
    bid: float
    ask: float
    observed_at: datetime
    expires_at: datetime

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2


def black_scholes_price(spot: float, strike: float, annual_volatility: float, seconds: float, option_type: OptionType) -> float:
    if spot <= 0 or strike <= 0 or annual_volatility <= 0 or seconds <= 0:
        raise ValueError("spot, strike, annual_volatility, and seconds must be positive")
    time_years = seconds / SECONDS_PER_YEAR
    volatility_term = annual_volatility * sqrt(time_years)
    d1 = (log(spot / strike) + 0.5 * annual_volatility**2 * time_years) / volatility_term
    d2 = d1 - volatility_term
    if option_type == "call":
        return spot * normal_cdf(d1) - strike * normal_cdf(d2)
    if option_type == "put":
        return strike * normal_cdf(-d2) - spot * normal_cdf(-d1)
    raise ValueError("option_type must be call or put")


def implied_volatility(spot: float, quote: OptionQuote) -> float:
    seconds = (quote.expires_at - quote.observed_at).total_seconds()
    if quote.observed_at.tzinfo is None or quote.expires_at.tzinfo is None:
        raise ValueError("option timestamps must be timezone-aware")
    if seconds <= 0:
        raise ValueError("option must expire in the future")
    intrinsic = max(0.0, spot - quote.strike) if quote.option_type == "call" else max(0.0, quote.strike - spot)
    if quote.midpoint <= intrinsic:
        raise ValueError("option midpoint must exceed intrinsic value")
    lower, upper = 0.0001, 5.0
    for _ in range(80):
        middle = (lower + upper) / 2
        price = black_scholes_price(spot, quote.strike, middle, seconds, quote.option_type)
        if price > quote.midpoint:
            upper = middle
        else:
            lower = middle
    return (lower + upper) / 2


def select_near_atm_option(spot: float, quotes: list[OptionQuote], now: datetime, max_age_seconds: float = 900, max_relative_spread: float = 0.25) -> OptionQuote:
    if spot <= 0 or now.tzinfo is None:
        raise ValueError("spot must be positive and now must be timezone-aware")
    eligible: list[OptionQuote] = []
    for quote in quotes:
        age = (now - quote.observed_at).total_seconds()
        if quote.observed_at.tzinfo is None or age < 0 or age > max_age_seconds:
            continue
        if quote.bid <= 0 or quote.ask < quote.bid or quote.expires_at <= now:
            continue
        if (quote.ask - quote.bid) / quote.midpoint > max_relative_spread:
            continue
        eligible.append(quote)
    if not eligible:
        raise ValueError("no liquid, current option quote is eligible")
    return min(eligible, key=lambda quote: abs(log(quote.strike / spot)))


@dataclass(frozen=True)
class ScheduledRiskEvent:
    kind: str
    starts_at: datetime
    blocking: bool


def risk_gate(now: datetime, resolves_at: datetime, events: list[ScheduledRiskEvent], halted: bool) -> tuple[bool, tuple[str, ...]]:
    if now.tzinfo is None or resolves_at.tzinfo is None:
        raise ValueError("risk timestamps must be timezone-aware")
    reasons: list[str] = []
    if halted:
        reasons.append("UNDERLYING_HALTED")
    for event in events:
        if event.starts_at.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        if event.blocking and now <= event.starts_at <= resolves_at:
            reasons.append(f"BLOCKING_EVENT:{event.kind.upper()}")
    return not reasons, tuple(reasons)


@dataclass(frozen=True)
class ShadowEvaluation:
    market_id: str
    fair_up_probability: float
    annual_volatility: float
    selected_option_symbol: str
    up_edge: EdgeAssessment
    down_edge: EdgeAssessment
    risk_passed: bool
    risk_reasons: tuple[str, ...]

    @property
    def paper_outcome(self) -> str | None:
        if not self.risk_passed:
            return None
        choices = (("UP", self.up_edge), ("DOWN", self.down_edge))
        eligible = [item for item in choices if item[1].should_record_paper_trade]
        return max(eligible, key=lambda item: item[1].edge)[0] if eligible else None


def evaluate_daily_direction(
    *,
    market_id: str,
    spot: float,
    prior_close: float,
    now: datetime,
    resolves_at: datetime,
    volatility_regime: VolatilityRegime,
    overnight_seconds: float,
    regular_seconds: float,
    option_quotes: list[OptionQuote],
    up_ask: float,
    down_ask: float,
    fee_rate: float,
    slippage: float,
    model_error_buffer: float,
    minimum_edge: float,
    events: list[ScheduledRiskEvent],
    halted: bool,
) -> ShadowEvaluation:
    if now.tzinfo is None or resolves_at.tzinfo is None or resolves_at <= now:
        raise ValueError("now and future resolves_at must be timezone-aware")
    selected_option = select_near_atm_option(spot, option_quotes, now)
    option_iv = implied_volatility(spot, selected_option)
    regime_iv = volatility_regime.blended_annual(overnight_seconds, regular_seconds)
    annual_volatility = (option_iv + regime_iv) / 2
    fair_up = digital_up_probability(spot, prior_close, annual_volatility, (resolves_at - now).total_seconds())
    risk_passed, risk_reasons = risk_gate(now, resolves_at, events, halted)
    return ShadowEvaluation(
        market_id=market_id,
        fair_up_probability=fair_up,
        annual_volatility=annual_volatility,
        selected_option_symbol=selected_option.symbol,
        up_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="YES", executable_ask=up_ask, fee_rate=fee_rate, slippage=slippage, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        down_edge=assess_buy_edge(fair_yes_probability=fair_up, outcome="NO", executable_ask=down_ask, fee_rate=fee_rate, slippage=slippage, model_error_buffer=model_error_buffer, minimum_edge=minimum_edge),
        risk_passed=risk_passed,
        risk_reasons=risk_reasons,
    )
