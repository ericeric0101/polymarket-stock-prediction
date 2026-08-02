"""Versioned contract for persisted real-time evaluation payloads."""
from __future__ import annotations
from typing import Mapping, NotRequired, TypedDict

PAYLOAD_VERSION = 1

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
    threshold_quality: NotRequired[str]

def validate(payload: Mapping[str, object]) -> None:
    version = payload.get("payload_version", 0)
    if version == 0:
        # Existing journals predate the contract. Keep their research data readable.
        required = {"evaluated_at", "market_id", "symbol", "signal_status", "skip_reasons"}
    elif version == PAYLOAD_VERSION:
        required = {"payload_version", "evaluated_at", "market_id", "symbol", "signal_status", "skip_reasons", "spot", "prior_close", "fair_up_probability", "up_ask", "down_ask", "up_bid", "down_bid"}
    else:
        raise ValueError(f"unsupported evaluation payload version: {version!r}")
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"evaluation payload is missing: {', '.join(sorted(missing))}")

def read_float(payload: Mapping[str, object], name: str) -> float | None:
    value = payload.get(name)
    return float(value) if isinstance(value, (int, float)) else None

def read_spot(payload: Mapping[str, object]) -> float | None:
    return read_float(payload, "spot")

def read_threshold(payload: Mapping[str, object]) -> float | None:
    return read_float(payload, "price_to_beat") or read_float(payload, "prior_close")
