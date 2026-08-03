"""Read-only option-IV surface adapters for shadow fair-value research."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import log
from typing import Callable, Mapping

from .http import PublicApiError, get_json


TRADIER_MARKETS_URL = "https://api.tradier.com/v1/markets"
MASSIVE_OPTIONS_SNAPSHOT_URL = "https://api.massive.com/v3/snapshot/options"
MASSIVE_FREE_MINIMUM_REQUEST_INTERVAL_SECONDS = 12.0


class OptionSurfaceError(ValueError):
    pass


@dataclass(frozen=True)
class OptionIvPoint:
    symbol: str
    option_type: str
    strike: float
    bid: float
    ask: float
    mid_iv: float
    observed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OptionIvSurface:
    symbol: str
    expiration: str
    observed_at: datetime
    atm_iv: float
    call_iv: float
    put_iv: float
    put_call_skew: float
    point_count: int
    provider: str
    quality_flags: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return not self.quality_flags

    def as_payload(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "expiration": self.expiration,
            "observed_at": self.observed_at.isoformat(),
            "atm_iv": self.atm_iv,
            "call_iv": self.call_iv,
            "put_iv": self.put_iv,
            "put_call_skew": self.put_call_skew,
            "point_count": self.point_count,
            "provider": self.provider,
            "quality_flags": list(self.quality_flags),
        }


class TradierOptionIvClient:
    """Read-only options-chain adapter. A production token is required for live data."""

    def __init__(self, token: str, get_json_fn: Callable[..., object] = get_json) -> None:
        self._token = token.strip()
        self._get_json = get_json_fn

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def option_surface(self, symbol: str, spot: float, now: datetime, resolves_at: datetime) -> OptionIvSurface:
        if not self.configured:
            raise OptionSurfaceError("TRADIER_API_TOKEN is not configured")
        if now.tzinfo is None or resolves_at.tzinfo is None or spot <= 0:
            raise ValueError("spot and timestamps must be valid")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        expirations_payload = self._get_json(
            f"{TRADIER_MARKETS_URL}/options/expirations",
            {"symbol": symbol.upper(), "includeAllRoots": "false"},
            headers=headers,
        )
        expiration = _nearest_usable_expiration(expirations_payload, resolves_at)
        chain_payload = self._get_json(
            f"{TRADIER_MARKETS_URL}/options/chains",
            {"symbol": symbol.upper(), "expiration": expiration, "greeks": "true"},
            headers=headers,
        )
        points = _parse_chain(chain_payload, now)
        return build_option_iv_surface(symbol.upper(), spot, expiration, now, points)


class PolygonOptionIvClient:
    """Read-only Massive (formerly Polygon) option-chain adapter.

    The free Currencies plan has no U.S. options entitlement. This adapter makes
    at most five requests per minute and disables itself after a 403 response,
    so an unsupported account cannot create a retry loop. Delayed or unlabelled
    quotes are retained as diagnostics but never become an IV-valid surface.
    """

    def __init__(self, api_key: str, get_json_fn: Callable[..., object] = get_json) -> None:
        self._api_key = api_key.strip()
        self._get_json = get_json_fn
        self._last_request_at: datetime | None = None
        self._entitlement_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def option_surface(self, symbol: str, spot: float, now: datetime, resolves_at: datetime) -> OptionIvSurface:
        if not self.configured:
            raise OptionSurfaceError("POLYGON_API_KEY is not configured")
        if now.tzinfo is None or resolves_at.tzinfo is None or spot <= 0:
            raise ValueError("spot and timestamps must be valid")
        if self._entitlement_error:
            raise OptionSurfaceError(self._entitlement_error)
        if self._last_request_at is not None:
            elapsed = (now - self._last_request_at).total_seconds()
            if elapsed < MASSIVE_FREE_MINIMUM_REQUEST_INTERVAL_SECONDS:
                raise OptionSurfaceError("POLYGON_FREE_TIER_RATE_LIMITED")

        self._last_request_at = now
        try:
            payload = self._get_json(
                f"{MASSIVE_OPTIONS_SNAPSHOT_URL}/{symbol.upper()}",
                {
                    "expiration_date.gte": resolves_at.date().isoformat(),
                    "limit": 250,
                    "order": "asc",
                    "sort": "expiration_date",
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except PublicApiError as error:
            if "HTTP 403" in str(error):
                self._entitlement_error = "POLYGON_OPTIONS_NOT_ENTITLED"
                raise OptionSurfaceError(self._entitlement_error) from error
            raise OptionSurfaceError("POLYGON_OPTIONS_REQUEST_FAILED") from error

        points, quality_flags = _parse_polygon_chain(payload, now)
        expiration = _earliest_expiration(points, resolves_at)
        surface = build_option_iv_surface(symbol.upper(), spot, expiration, now, points)
        return replace(
            surface,
            provider="MASSIVE_OPTIONS",
            quality_flags=tuple(sorted(set((*surface.quality_flags, *quality_flags)))),
        )


def build_option_iv_surface(
    symbol: str,
    spot: float,
    expiration: str,
    now: datetime,
    points: list[OptionIvPoint],
    *,
    max_age_seconds: float = 900,
    max_relative_spread: float = 0.25,
) -> OptionIvSurface:
    """Select liquid near-ATM call/put IVs and expose skew without directional alpha."""

    if spot <= 0 or now.tzinfo is None:
        raise ValueError("spot must be positive and now timezone-aware")
    flags: list[str] = []
    eligible = []
    for point in points:
        age = (now - point.observed_at).total_seconds()
        midpoint = (point.bid + point.ask) / 2
        if age < 0 or age > max_age_seconds:
            continue
        if point.option_type not in {"call", "put"} or point.bid <= 0 or point.ask < point.bid or point.mid_iv <= 0:
            continue
        if (point.ask - point.bid) / midpoint > max_relative_spread:
            continue
        eligible.append(point)
    if not eligible:
        raise OptionSurfaceError("no current liquid option IV quotes")
    by_type: dict[str, OptionIvPoint] = {}
    for option_type in ("call", "put"):
        choices = [point for point in eligible if point.option_type == option_type]
        if choices:
            by_type[option_type] = min(choices, key=lambda point: abs(log(point.strike / spot)))
    if "call" not in by_type or "put" not in by_type:
        flags.append("INCOMPLETE_ATM_PUT_CALL_SURFACE")
    call_iv = by_type.get("call", next(iter(eligible))).mid_iv
    put_iv = by_type.get("put", next(iter(eligible))).mid_iv
    observed_at = min(point.observed_at for point in by_type.values())
    return OptionIvSurface(
        symbol=symbol,
        expiration=expiration,
        observed_at=observed_at,
        atm_iv=(call_iv + put_iv) / 2,
        call_iv=call_iv,
        put_iv=put_iv,
        put_call_skew=put_iv - call_iv,
        point_count=len(eligible),
        provider="TRADIER_ORATS",
        quality_flags=tuple(flags),
    )


def _nearest_usable_expiration(payload: object, resolves_at: datetime) -> str:
    try:
        dates = payload["expirations"]["date"]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise OptionSurfaceError("Tradier expiration response is invalid") from error
    values = [dates] if isinstance(dates, str) else dates
    if not isinstance(values, list):
        raise OptionSurfaceError("Tradier expiration response has no dates")
    eligible = sorted(date for date in values if isinstance(date, str) and date >= resolves_at.date().isoformat())
    if not eligible:
        raise OptionSurfaceError("no option expiry at or after market resolution")
    return eligible[0]


def _parse_chain(payload: object, now: datetime) -> list[OptionIvPoint]:
    try:
        raw_options = payload["options"]["option"]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise OptionSurfaceError("Tradier option-chain response is invalid") from error
    rows = raw_options if isinstance(raw_options, list) else [raw_options]
    points: list[OptionIvPoint] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            greeks = row["greeks"]
            if not isinstance(greeks, Mapping):
                continue
            updated_at = _parse_provider_time(greeks.get("updated_at"), now)
            expires_at = datetime.fromisoformat(str(row["expiration_date"]) + "T20:00:00+00:00")
            points.append(
                OptionIvPoint(
                    symbol=str(row["symbol"]),
                    option_type=str(row["option_type"]).lower(),
                    strike=float(row["strike"]),
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    mid_iv=float(greeks["mid_iv"]),
                    observed_at=updated_at,
                    expires_at=expires_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points


def _parse_polygon_chain(payload: object, now: datetime) -> tuple[list[OptionIvPoint], tuple[str, ...]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise OptionSurfaceError("Massive option-chain response is invalid")
    points: list[OptionIvPoint] = []
    quality_flags: set[str] = set()
    for row in payload["results"]:
        if not isinstance(row, Mapping):
            continue
        details = row.get("details")
        quote = row.get("last_quote")
        if not isinstance(details, Mapping) or not isinstance(quote, Mapping):
            continue
        try:
            timeframe = str(quote.get("timeframe", "")).upper()
            if timeframe != "REAL-TIME":
                quality_flags.add(
                    "POLYGON_OPTION_QUOTES_DELAYED"
                    if timeframe == "DELAYED"
                    else "POLYGON_OPTION_QUOTE_TIMEFRAME_UNKNOWN"
                )
            expiration = datetime.fromisoformat(str(details["expiration_date"]) + "T20:00:00+00:00")
            points.append(
                OptionIvPoint(
                    symbol=str(details["ticker"]),
                    option_type=str(details["contract_type"]).lower(),
                    strike=float(details["strike_price"]),
                    bid=float(quote["bid"]),
                    ask=float(quote["ask"]),
                    mid_iv=float(row["implied_volatility"]),
                    observed_at=_parse_polygon_time(quote.get("last_updated"), now),
                    expires_at=expiration,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points, tuple(sorted(quality_flags))


def _earliest_expiration(points: list[OptionIvPoint], resolves_at: datetime) -> str:
    eligible = sorted(point.expires_at.date().isoformat() for point in points if point.expires_at >= resolves_at)
    if not eligible:
        raise OptionSurfaceError("no option expiry at or after market resolution")
    return eligible[0]


def _parse_polygon_time(value: object, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)):
        # Massive timestamps are nanoseconds; tolerate millisecond data in mocks.
        timestamp = float(value)
        if timestamp > 10_000_000_000_000:
            timestamp /= 1_000_000_000
        elif timestamp > 10_000_000_000:
            timestamp /= 1_000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return fallback


def _parse_provider_time(value: object, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    if isinstance(value, str):
        for candidate in (value.replace(" ", "T") + "+00:00", value):
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
    return fallback
