"""Versioned contract for persisted real-time evaluation payloads."""

from __future__ import annotations

from typing import Mapping, NotRequired, TypedDict

PAYLOAD_VERSION = 3
V1_REQUIRED_KEYS = frozenset(
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
V2_REQUIRED_KEYS = V1_REQUIRED_KEYS.union(
    {
        "price_to_beat_distance_bps",
        "market_up_probability",
        "market_model_divergence",
        "model_majority_outcome",
        "entry_diagnostic_flags",
    }
)
CURRENT_REQUIRED_KEYS = V2_REQUIRED_KEYS.union({"entry_policy_category"})
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
    price_to_beat_distance_bps: float | None
    market_up_probability: float | None
    market_model_divergence: float | None
    model_majority_outcome: str | None
    entry_diagnostic_flags: list[str]
    entry_policy_category: str


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
    elif version == 1:
        _require(payload, V1_REQUIRED_KEYS)
    elif version == 2:
        _require(payload, V2_REQUIRED_KEYS)
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


def read_outcome(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    return value if value in {"UP", "DOWN"} else None


def read_model_outcome(payload: Mapping[str, object]) -> str | None:
    return read_outcome(payload, "model_outcome")


def read_paper_outcome(payload: Mapping[str, object]) -> str | None:
    return read_outcome(payload, "paper_outcome")


def read_entry_diagnostic_flags(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("entry_diagnostic_flags")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def read_model_majority_outcome(payload: Mapping[str, object]) -> str | None:
    """Read the primary direction, deriving it for journal rows before v2."""
    value = read_outcome(payload, "model_majority_outcome")
    if value is not None:
        return value
    fair_up = read_float(payload, "fair_up_probability")
    return None if fair_up is None else ("UP" if fair_up >= 0.5 else "DOWN")


def read_entry_policy_category(payload: Mapping[str, object]) -> str:
    """Read or derive the non-gating entry-policy research category."""
    value = payload.get("entry_policy_category")
    if value in {"NO_EDGE", "MODEL_ALIGNED", "CONTRARIAN_VALUE", "UNKNOWN"}:
        return value
    selected = read_model_outcome(payload)
    majority = read_model_majority_outcome(payload)
    if selected is None:
        return "NO_EDGE"
    if majority is None:
        return "UNKNOWN"
    return "MODEL_ALIGNED" if selected == majority else "CONTRARIAN_VALUE"
