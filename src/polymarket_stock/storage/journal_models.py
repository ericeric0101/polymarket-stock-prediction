"""Typed records returned by the shadow SQLite journal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class StoredOutcomeToken:
    label: str
    token_id: str


@dataclass(frozen=True)
class StoredMarketCandidate:
    market_id: str
    question: str
    slug: str
    end_date: str
    outcome_a_label: str
    outcome_b_label: str
    review_status: str


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    opened_at: datetime
    market_id: str
    symbol: str
    outcome: str
    status: str
    contracts: float
    entry_ask: float
    entry_fee: float
    entry_slippage: float
    fair_probability: float
    model_version: str
    settled_at: datetime | None
    settlement_outcome: str | None
    payout: float | None
    realized_pnl: float | None
    included_in_calibration: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class PaperBatchEntry:
    market_id: str
    symbol: str
    outcome: str
    risk_group: str
    edge: float
    selected: bool
    reason: str
    payload: Mapping[str, object]
    entry_ask: float | None = None
    fair_probability: float | None = None
    model_version: str | None = None
    fee_rate: float | None = None


@dataclass(frozen=True)
class PaperBatchResult:
    market_id: str
    position: PaperPosition | None
    created: bool


@dataclass(frozen=True)
class MakerShadowQuote:
    quote_id: str
    created_at: datetime
    last_observed_at: datetime
    market_id: str
    symbol: str
    outcome: str
    status: str
    limit_price: float
    fair_probability: float
    theoretical_edge: float
    best_bid: float
    best_ask: float
    touch_count: int
    last_touched_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


@dataclass(frozen=True)
class ReplayObservation:
    market_id: str
    symbol: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    winning_outcome: str


@dataclass(frozen=True)
class FirstSignalCalibrationObservation:
    """One immutable first model-side signal per settled market."""

    market_id: str
    symbol: str
    evaluated_at: datetime
    model_outcome: str
    selected_fair_probability: float
    entry_ask: float
    entry_fee: float | None
    winning_outcome: str
    model_version: str
    option_iv_status: str
    iv_regime: str
    spot_provider: str
    threshold_distance_bps: float | None
    volatility_estimator: str = "CLOSE_TO_CLOSE"


@dataclass(frozen=True)
class CheckpointObservation:
    market_id: str
    symbol: str
    checkpoint_date: str
    checkpoint_name: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    model_version: str
    option_iv: float | None
    winning_outcome: str
    checkpoint_target_at: datetime
    checkpoint_delay_seconds: float
    eligible_for_calibration: bool


@dataclass(frozen=True)
class BufferSweepObservation:
    market_id: str
    symbol: str
    checkpoint_date: str
    checkpoint_name: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    up_taker_fee: float | None
    down_taker_fee: float | None
    winning_outcome: str
    spot: float | None = None
    price_to_beat: float | None = None
    up_bid: float | None = None
    down_bid: float | None = None
    annualized_volatility: float | None = None
    cross_source_difference: float | None = None
    comparison_models: tuple[Mapping[str, object], ...] = ()
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionObservation:
    observed_at: datetime
    signal_id: str | None
    observation_kind: str
    market_id: str
    symbol: str
    outcome: str
    token_id: str
    spot: float | None
    price_to_beat: float | None
    fair_probability: float | None
    best_bid: float | None
    best_ask: float | None
    fee_rate: float | None
    book_payload: Mapping[str, object]
    evaluation_payload: Mapping[str, object]


@dataclass(frozen=True)
class SpotSourceComparison:
    observed_at: datetime
    symbol: str
    primary_source: str
    primary_price: float
    pyth_price: float
    pyth_confidence: float | None
    difference_bps: float
    primary_published_at: datetime | None = None
    pyth_published_at: datetime | None = None


@dataclass(frozen=True)
class StoredSpotObservation:
    observed_at: datetime
    source: str
    symbol: str
    price: float
    published_at: datetime | None = None
