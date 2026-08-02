"""Market-session and cross-source data-quality checks for US equity contracts."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .trading_calendar import next_nyse_trading_day, nyse_holiday_name


NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def us_equity_session(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    holiday = nyse_holiday_name(local.date())
    if holiday:
        return f"HOLIDAY:{holiday}"
    if local.weekday() >= 5:
        return "WEEKEND"
    if REGULAR_OPEN <= local.time() < REGULAR_CLOSE:
        return "REGULAR"
    if local.time() < REGULAR_OPEN:
        return "PREMARKET"
    return "AFTER_HOURS"


def observable_equity_market_date(now: datetime) -> date:
    """Choose the contract date worth observing even when US equities are closed."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    if us_equity_session(now) in {"PREMARKET", "REGULAR"}:
        return local.date()
    return next_nyse_trading_day(local.date())


def relative_price_difference(primary: float, reference: float) -> float:
    if primary <= 0 or reference <= 0:
        raise ValueError("prices must be positive")
    return abs(primary - reference) / primary
