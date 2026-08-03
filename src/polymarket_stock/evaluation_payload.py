"""Versioned contract for persisted real-time evaluation payloads."""

from __future__ import annotations

from typing import Mapping, NotRequired, TypedDict

PAYLOAD_VERSION = 1
CURRENT_REQUIRED_KEYS = frozenset(
    {
        "payload_version",
        "evaluated_at",
        "market_id",
        "symbol",
        "signal_status",
        "skip_reasons",
        "spot",
        "fair_up_probability",
        "up_ask",
        "down_ask",
    }
)
LEGACY_REQUIRED_KEYS = frozenset({"evaluated_at", "market_id", "symbol", "signal_status", "skip_reasons"})


class EvaluationPayload(TypedDict):
    payload_version: int
    evaluated_at: str
    market_id: str
    symbol: str
    signal_status: str
    skip_reasons: list[str]
    spot: float | None
    prior_close: float | None
    price_to_beat: NotRequired[float | None]
    fair_up_probability: float | None
    up_ask: float | None
    down_ask: float | None
    up_bid: float | None
    down_bid: float | None
    market_session: str
    model_version: str
    volatility_estimator: str
    comparison_models: list[Mapping[str, object]]
    option_iv_status: NotRequired[str]
    cross_source_difference: NotRequired[float | None]
    threshold_quality: NotRequired[str]


def _require(payload: Mapping[str, object], required: frozenset[str]) -> None:
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"evaluation payload is missing: {', '.join(sorted(missing))}")


def validate_for_write(payload: Mapping[str, object]) -> None:
    """Require every newly persisted evaluation to use the current schema."""
    if payload.get("payload_version") != PAYLOAD_VERSION:
        raise ValueError(f"writes must use payload_version={PAYLOAD_VERSION}")
    _require(payload, CURRENT_REQUIRED_KEYS)


def validate_for_read(payload: Mapping[str, object]) -> None:
    """Accept legacy journal rows, but validate versioned rows strictly."""
    version = payload.get("payload_version", 0)
    if version == 0:
        _require(payload, LEGACY_REQUIRED_KEYS)
    elif version == PAYLOAD_VERSION:
        _require(payload, CURRENT_REQUIRED_KEYS)
    else:
        raise ValueError(f"unsupported evaluation payload version: {version!r}")


def validate(payload: Mapping[str, object]) -> None:
    """Backward-compatible read validation alias."""
    validate_for_read(payload)


def read_float(payload: Mapping[str, object], name: str) -> float | None:
    value = payload.get(name)
    return float(value) if isinstance(value, (int, float)) else None


def read_spot(payload: Mapping[str, object]) -> float | None:
    return read_float(payload, "spot")


def read_threshold(payload: Mapping[str, object]) -> float | None:
    threshold = read_float(payload, "price_to_beat")
    return threshold if threshold is not None else read_float(payload, "prior_close")
