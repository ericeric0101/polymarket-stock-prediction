"""Independent option-pricing checks for offline research validation only.

This module deliberately has no provider client, journal write, or supervisor
dependency. It validates the numerical pricing assumptions around a quote; it
does not make a delayed or scraped quote suitable for an entry decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, log, sqrt
from typing import Literal

from .pricing import SECONDS_PER_YEAR, normal_cdf


OptionType = Literal["call", "put"]
ExerciseStyle = Literal["european", "american"]


@dataclass(frozen=True)
class OptionPricingInputs:
    spot: float
    strike: float
    annual_volatility: float
    seconds_to_expiry: float
    option_type: OptionType
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0

    @property
    def time_years(self) -> float:
        return self.seconds_to_expiry / SECONDS_PER_YEAR

    def validate(self) -> None:
        if self.spot <= 0 or self.strike <= 0 or self.annual_volatility <= 0 or self.seconds_to_expiry <= 0:
            raise ValueError("spot, strike, annual_volatility, and seconds_to_expiry must be positive")
        if self.option_type not in {"call", "put"}:
            raise ValueError("option_type must be call or put")


@dataclass(frozen=True)
class OptionPricingValidation:
    market_midpoint: float
    black_scholes_merton_price: float
    binomial_price: float
    implied_volatility: float
    absolute_model_difference: float
    status: str = "RESEARCH_ONLY_VALIDATED"

    def as_payload(self) -> dict[str, object]:
        return {
            "market_midpoint": round(self.market_midpoint, 6),
            "black_scholes_merton_price": round(self.black_scholes_merton_price, 6),
            "binomial_price": round(self.binomial_price, 6),
            "implied_volatility": round(self.implied_volatility, 6),
            "absolute_model_difference": round(self.absolute_model_difference, 6),
            "status": self.status,
            "entry_eligible": False,
        }


def black_scholes_merton_price(inputs: OptionPricingInputs) -> float:
    """European option price with continuously compounded rate and dividend yield."""

    inputs.validate()
    time_years = inputs.time_years
    volatility_term = inputs.annual_volatility * sqrt(time_years)
    d1 = (
        log(inputs.spot / inputs.strike)
        + (inputs.risk_free_rate - inputs.dividend_yield + 0.5 * inputs.annual_volatility**2) * time_years
    ) / volatility_term
    d2 = d1 - volatility_term
    discounted_spot = inputs.spot * exp(-inputs.dividend_yield * time_years)
    discounted_strike = inputs.strike * exp(-inputs.risk_free_rate * time_years)
    if inputs.option_type == "call":
        return discounted_spot * normal_cdf(d1) - discounted_strike * normal_cdf(d2)
    return discounted_strike * normal_cdf(-d2) - discounted_spot * normal_cdf(-d1)


def crr_binomial_price(inputs: OptionPricingInputs, *, style: ExerciseStyle = "american", steps: int = 500) -> float:
    """Cox-Ross-Rubinstein valuation, supporting an American early-exercise check."""

    inputs.validate()
    if style not in {"european", "american"} or steps < 10:
        raise ValueError("style must be european or american and steps must be at least 10")
    time_years = inputs.time_years
    dt = time_years / steps
    up = exp(inputs.annual_volatility * sqrt(dt))
    down = 1 / up
    growth = exp((inputs.risk_free_rate - inputs.dividend_yield) * dt)
    probability = (growth - down) / (up - down)
    if not 0 < probability < 1:
        raise ValueError("invalid binomial risk-neutral probability")
    discount = exp(-inputs.risk_free_rate * dt)
    values = [
        _intrinsic(inputs.option_type, inputs.spot * up ** (steps - index) * down**index, inputs.strike)
        for index in range(steps + 1)
    ]
    for level in range(steps - 1, -1, -1):
        next_values: list[float] = []
        for index in range(level + 1):
            continuation = discount * (probability * values[index] + (1 - probability) * values[index + 1])
            if style == "american":
                spot = inputs.spot * up ** (level - index) * down**index
                continuation = max(continuation, _intrinsic(inputs.option_type, spot, inputs.strike))
            next_values.append(continuation)
        values = next_values
    return values[0]


def implied_volatility_bsm(inputs: OptionPricingInputs, market_midpoint: float, *, iterations: int = 100) -> float:
    """Recover BSM IV from a midpoint without trusting a provider-supplied IV field."""

    inputs.validate()
    if market_midpoint <= _intrinsic(inputs.option_type, inputs.spot, inputs.strike):
        raise ValueError("market midpoint must exceed intrinsic value")
    lower, upper = 0.0001, 5.0
    for _ in range(iterations):
        midpoint = (lower + upper) / 2
        price = black_scholes_merton_price(_with_volatility(inputs, midpoint))
        if price > market_midpoint:
            upper = midpoint
        else:
            lower = midpoint
    return (lower + upper) / 2


def validate_option_quote(
    inputs: OptionPricingInputs, *, bid: float, ask: float, style: ExerciseStyle = "american", binomial_steps: int = 500
) -> OptionPricingValidation:
    if bid <= 0 or ask < bid:
        raise ValueError("bid and ask must be positive and ordered")
    midpoint = (bid + ask) / 2
    bsm_price = black_scholes_merton_price(inputs)
    binomial_price = crr_binomial_price(inputs, style=style, steps=binomial_steps)
    implied_volatility = implied_volatility_bsm(inputs, midpoint)
    return OptionPricingValidation(
        market_midpoint=midpoint,
        black_scholes_merton_price=bsm_price,
        binomial_price=binomial_price,
        implied_volatility=implied_volatility,
        absolute_model_difference=abs(bsm_price - binomial_price),
    )


def _intrinsic(option_type: OptionType, spot: float, strike: float) -> float:
    return max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot)


def _with_volatility(inputs: OptionPricingInputs, annual_volatility: float) -> OptionPricingInputs:
    return OptionPricingInputs(
        spot=inputs.spot,
        strike=inputs.strike,
        annual_volatility=annual_volatility,
        seconds_to_expiry=inputs.seconds_to_expiry,
        option_type=inputs.option_type,
        risk_free_rate=inputs.risk_free_rate,
        dividend_yield=inputs.dividend_yield,
    )
