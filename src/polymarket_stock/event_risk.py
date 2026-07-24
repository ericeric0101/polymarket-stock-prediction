"""Structured local event calendar used as a hard gate for daily direction research."""

from __future__ import annotations

from datetime import UTC, datetime, time
import json
from pathlib import Path
import time as clock

from .research import ScheduledRiskEvent
from .http import PublicApiError, get_json


class EventCalendarError(ValueError):
    pass


class EventCalendarUnavailable(EventCalendarError):
    """The remote calendar could not be fetched; entries must remain gated."""


class FinnhubEarningsCalendarClient:
    """Read-only earnings calendar; macro events remain versioned local data."""

    def __init__(self, api_key: str, get_json_fn=get_json) -> None:
        self.api_key = api_key.strip()
        self._get_json = get_json_fn
        self._unavailable_until = 0.0
        self._unavailable_message = "Finnhub earnings calendar is unavailable"

    def events(self, symbol: str, now: datetime, resolves_at: datetime) -> tuple[ScheduledRiskEvent, ...]:
        if not self.api_key:
            return ()
        if clock.monotonic() < self._unavailable_until:
            raise EventCalendarUnavailable(self._unavailable_message)
        try:
            payload = self._get_json(
                "https://finnhub.io/api/v1/calendar/earnings",
                {"from": now.date().isoformat(), "to": resolves_at.date().isoformat(), "symbol": symbol.upper(), "token": self.api_key},
                timeout_seconds=5.0,
            )
        except PublicApiError as error:
            self._unavailable_until = clock.monotonic() + 60.0
            raise EventCalendarUnavailable(self._unavailable_message) from error
        rows = payload.get("earningsCalendar", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise EventCalendarError("Finnhub earnings calendar is invalid")
        events = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            try:
                date_value = datetime.fromisoformat(str(row["date"]))
                hour = str(row.get("hour", "")).lower()
                event_time = time(13, 0) if hour == "bmo" else time(20, 5) if hour == "amc" else time(16, 0)
                starts_at = datetime.combine(date_value.date(), event_time, tzinfo=UTC)
                if now <= starts_at <= resolves_at:
                    events.append(ScheduledRiskEvent("EARNINGS", starts_at, True))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(events)


def combined_risk_events(
    path: Path,
    symbol: str,
    now: datetime,
    resolves_at: datetime,
    finnhub_api_key: str,
    earnings_client: FinnhubEarningsCalendarClient | None = None,
) -> tuple[ScheduledRiskEvent, ...]:
    local = load_risk_events(path, symbol, now, resolves_at)
    earnings = (earnings_client or FinnhubEarningsCalendarClient(finnhub_api_key)).events(symbol, now, resolves_at)
    return tuple({(event.kind, event.starts_at, event.blocking): event for event in (*local, *earnings)}.values())


def load_risk_events(path: Path, symbol: str, now: datetime, resolves_at: datetime) -> tuple[ScheduledRiskEvent, ...]:
    """Load earnings/FOMC/CPI-style events from a versioned local JSON calendar.

    Rows use: {"kind", "starts_at", "symbols": ["TSLA"] | ["*"], "blocking": true}.
    """

    if now.tzinfo is None or resolves_at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EventCalendarError(f"cannot parse event calendar: {error}") from error
    if not isinstance(payload, list):
        raise EventCalendarError("event calendar must be a JSON list")
    events: list[ScheduledRiskEvent] = []
    for row in payload:
        if not isinstance(row, dict):
            raise EventCalendarError("event calendar row must be an object")
        try:
            starts_at = datetime.fromisoformat(str(row["starts_at"]).replace("Z", "+00:00"))
            symbols = row.get("symbols", ["*"])
            if not isinstance(symbols, list) or starts_at.tzinfo is None:
                raise ValueError
            if symbol.upper() not in {str(value).upper() for value in symbols} and "*" not in symbols:
                continue
            if now <= starts_at <= resolves_at:
                events.append(ScheduledRiskEvent(str(row["kind"]), starts_at, bool(row.get("blocking", True))))
        except (KeyError, TypeError, ValueError) as error:
            raise EventCalendarError("event calendar row is invalid") from error
    return tuple(events)
