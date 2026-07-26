"""Dependency-free NYSE regular-session and core-holiday calendar."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def nyse_holiday_name(day: date) -> str | None:
    year = day.year
    holidays = {
        _observed(date(year, 1, 1)): "NEW_YEAR",
        _nth_weekday(year, 1, 0, 3): "MLK_DAY",
        _nth_weekday(year, 2, 0, 3): "PRESIDENTS_DAY",
        _easter_sunday(year) - timedelta(days=2): "GOOD_FRIDAY",
        _last_weekday(year, 5, 0): "MEMORIAL_DAY",
        _observed(date(year, 6, 19)): "JUNETEENTH",
        _observed(date(year, 7, 4)): "INDEPENDENCE_DAY",
        _nth_weekday(year, 9, 0, 1): "LABOR_DAY",
        _nth_weekday(year, 11, 3, 4): "THANKSGIVING",
        _observed(date(year, 12, 25)): "CHRISTMAS",
    }
    return holidays.get(day)


def is_nyse_regular_session(now: datetime) -> bool:
    return nyse_holiday_name(now.date()) is None


def previous_nyse_trading_day(day: date) -> date:
    """Return the prior full NYSE trading day for daily-close contracts."""

    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5 or nyse_holiday_name(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _observed(day: date) -> date:
    return day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    value = date(year, month, 1)
    return value + timedelta(days=(weekday - value.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    value = next_month - timedelta(days=1)
    return value - timedelta(days=(value.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)
