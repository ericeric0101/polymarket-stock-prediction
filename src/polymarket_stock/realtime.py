"""Freshness-gated real-time baseline evaluations for shadow research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Mapping

from .baseline import DailyClose, daily_close_data_is_fresh, evaluate_realized_vol_baseline


@dataclass(frozen=True)
class RealtimeEvaluation:
    evaluated_at: datetime
    market_id: str
    symbol: str
    spot_provider: str
    spot: float | None
    up_ask: float | None
    down_ask: float | None
    spot_age_seconds: float | None
    book_age_seconds: float | None
    stream_ready: bool
    daily_data_is_fresh: bool
    fair_up_probability: float | None
    annualized_realized_volatility: float | None
    prior_close: float | None
    model_error_buffer: float
    up_edge: float | None
    down_edge: float | None
    paper_outcome: str | None
    trigger_reasons: tuple[str, ...]
    skip_reasons: tuple[str, ...]

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["trigger_reasons"] = list(self.trigger_reasons)
        payload["skip_reasons"] = list(self.skip_reasons)
        payload["signal_status"] = f"PAPER_{self.paper_outcome}" if self.paper_outcome else "NO_PAPER_TRADE"
        return payload


class RealtimeBaselineEvaluator:
    """Apply the existing realized-volatility fallback only to fresh WS state."""

    def __init__(
        self,
        *,
        market_id: str,
        symbol: str,
        resolves_at: datetime,
        closes: list[DailyClose],
        spot_provider: str,
        fee_rate: float = 0.01,
        slippage: float = 0.001,
        base_model_error_buffer: float = 0.02,
        fallback_buffer: float = 0.05,
        minimum_edge: float = 0.02,
    ) -> None:
        if resolves_at.tzinfo is None:
            raise ValueError("resolves_at must be timezone-aware")
        self._market_id = market_id
        self._symbol = symbol.upper()
        self._resolves_at = resolves_at
        self._closes = closes
        self._spot_provider = spot_provider
        self._fee_rate = fee_rate
        self._slippage = slippage
        self._base_model_error_buffer = base_model_error_buffer
        self._fallback_buffer = fallback_buffer
        self._minimum_edge = minimum_edge

    def evaluate(
        self,
        *,
        now: datetime,
        spot: float | None,
        up_ask: float | None,
        down_ask: float | None,
        spot_age_seconds: float | None,
        book_age_seconds: float | None,
        stream_ready: bool,
        trigger_reasons: tuple[str, ...],
    ) -> RealtimeEvaluation:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        daily_data_is_fresh = daily_close_data_is_fresh(self._closes, now)
        skip_reasons: list[str] = []
        if now >= self._resolves_at:
            skip_reasons.append("MARKET_PAST_RESOLUTION")
        if not stream_ready:
            skip_reasons.append("STALE_OR_INCOMPLETE_STREAM")
        if spot is None or spot <= 0:
            skip_reasons.append("MISSING_SPOT")
        if up_ask is None or down_ask is None:
            skip_reasons.append("MISSING_EXECUTABLE_ASK")
        if not daily_data_is_fresh:
            skip_reasons.append("STALE_DAILY_BASELINE")

        common = {
            "evaluated_at": now,
            "market_id": self._market_id,
            "symbol": self._symbol,
            "spot_provider": self._spot_provider,
            "spot": spot,
            "up_ask": up_ask,
            "down_ask": down_ask,
            "spot_age_seconds": spot_age_seconds,
            "book_age_seconds": book_age_seconds,
            "stream_ready": stream_ready,
            "daily_data_is_fresh": daily_data_is_fresh,
            "model_error_buffer": self._base_model_error_buffer + self._fallback_buffer,
        }
        if skip_reasons:
            return RealtimeEvaluation(
                **common,
                fair_up_probability=None,
                annualized_realized_volatility=None,
                prior_close=None,
                up_edge=None,
                down_edge=None,
                paper_outcome=None,
                trigger_reasons=trigger_reasons,
                skip_reasons=tuple(sorted(set(skip_reasons))),
            )

        assessment = evaluate_realized_vol_baseline(
            spot=spot,
            closes=self._closes,
            seconds_to_resolution=(self._resolves_at - now).total_seconds(),
            up_ask=up_ask,
            down_ask=down_ask,
            fee_rate=self._fee_rate,
            slippage=self._slippage,
            base_model_error_buffer=self._base_model_error_buffer,
            fallback_buffer=self._fallback_buffer,
            minimum_edge=self._minimum_edge,
            data_is_fresh=daily_data_is_fresh,
            lookback_days=20,
        )
        return RealtimeEvaluation(
            **common,
            fair_up_probability=assessment.fair_up_probability,
            annualized_realized_volatility=assessment.annualized_realized_volatility,
            prior_close=assessment.prior_close,
            up_edge=assessment.up_edge.edge,
            down_edge=assessment.down_edge.edge,
            paper_outcome=assessment.paper_outcome,
            trigger_reasons=trigger_reasons,
            skip_reasons=(),
        )
