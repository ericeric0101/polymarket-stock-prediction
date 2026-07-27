"""Freshness-gated real-time baseline evaluations for shadow research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Mapping

from .baseline import DailyClose, annualized_realized_volatility, daily_close_data_is_fresh, evaluate_realized_vol_baseline
from .quality import relative_price_difference, us_equity_session


@dataclass(frozen=True)
class RealtimeEvaluation:
    evaluated_at: datetime
    market_id: str
    symbol: str
    spot_provider: str
    spot: float | None
    reference_spot: float | None
    reference_spot_age_seconds: float | None
    cross_source_difference: float | None
    option_iv: float | None
    option_skew: float | None
    option_iv_provider: str | None
    option_iv_age_seconds: float | None
    option_iv_status: str
    up_ask: float | None
    down_ask: float | None
    up_bid: float | None
    down_bid: float | None
    up_fee_rate: float | None
    down_fee_rate: float | None
    up_taker_fee: float | None
    down_taker_fee: float | None
    spot_age_seconds: float | None
    book_age_seconds: float | None
    stream_ready: bool
    market_session: str
    daily_data_is_fresh: bool
    fair_up_probability: float | None
    annualized_realized_volatility: float | None
    prior_close: float | None
    model_error_buffer: float
    up_edge: float | None
    down_edge: float | None
    model_outcome: str | None
    paper_outcome: str | None
    paper_entry_eligible: bool
    paper_entry_block_reasons: tuple[str, ...]
    quality_flags: tuple[str, ...]
    trigger_reasons: tuple[str, ...]
    skip_reasons: tuple[str, ...]

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["trigger_reasons"] = list(self.trigger_reasons)
        payload["quality_flags"] = list(self.quality_flags)
        payload["skip_reasons"] = list(self.skip_reasons)
        payload["paper_entry_block_reasons"] = list(self.paper_entry_block_reasons)
        if self.paper_outcome:
            payload["signal_status"] = f"PAPER_{self.paper_outcome}"
        elif self.model_outcome:
            payload["signal_status"] = f"OBSERVATION_ONLY_{self.model_outcome}"
        else:
            payload["signal_status"] = "NO_PAPER_TRADE"
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
        up_fee_rate: float | None = None,
        down_fee_rate: float | None = None,
        base_model_error_buffer: float = 0.02,
        fallback_buffer: float = 0.0,
        minimum_edge: float = 0.02,
        price_to_beat: float | None = None,
    ) -> None:
        if resolves_at.tzinfo is None:
            raise ValueError("resolves_at must be timezone-aware")
        self._market_id = market_id
        self._symbol = symbol.upper()
        self._resolves_at = resolves_at
        self._closes = closes
        self._spot_provider = spot_provider
        self._up_fee_rate = up_fee_rate
        self._down_fee_rate = down_fee_rate
        self._base_model_error_buffer = base_model_error_buffer
        self._fallback_buffer = fallback_buffer
        self._minimum_edge = minimum_edge
        if price_to_beat is not None and price_to_beat <= 0:
            raise ValueError("price_to_beat must be positive")
        self._price_to_beat = price_to_beat

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
        up_bid: float | None = None,
        down_bid: float | None = None,
        reference_spot: float | None = None,
        reference_spot_age_seconds: float | None = None,
        maximum_cross_source_difference: float = 0.005,
        option_iv: float | None = None,
        option_skew: float | None = None,
        option_iv_provider: str | None = None,
        option_iv_age_seconds: float | None = None,
        option_quality_flags: tuple[str, ...] = (),
        risk_reasons: tuple[str, ...] = (),
        additional_model_error_buffer: float = 0.0,
    ) -> RealtimeEvaluation:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        daily_data_is_fresh = daily_close_data_is_fresh(self._closes, now)
        skip_reasons: list[str] = []
        quality_flags: list[str] = []
        market_session = us_equity_session(now)
        cross_source_difference = None
        if market_session != "REGULAR":
            skip_reasons.append(f"NON_REGULAR_SESSION:{market_session}")
        if now >= self._resolves_at:
            skip_reasons.append("MARKET_PAST_RESOLUTION")
        if not stream_ready:
            skip_reasons.append("STALE_OR_INCOMPLETE_STREAM")
        if spot is None or spot <= 0:
            skip_reasons.append("MISSING_SPOT")
        if up_ask is None or down_ask is None:
            skip_reasons.append("MISSING_EXECUTABLE_ASK")
        if up_bid is None:
            skip_reasons.append("EMPTY_UP_BOOK")
        if down_bid is None:
            skip_reasons.append("EMPTY_DOWN_BOOK")
        if self._up_fee_rate is None or self._down_fee_rate is None:
            skip_reasons.append("FEE_RATE_UNAVAILABLE")
        if up_bid is not None and up_ask is not None and up_bid > up_ask:
            skip_reasons.append("CROSSED_UP_BOOK")
        if down_bid is not None and down_ask is not None and down_bid > down_ask:
            skip_reasons.append("CROSSED_DOWN_BOOK")
        if not daily_data_is_fresh:
            skip_reasons.append("STALE_DAILY_BASELINE")
        skip_reasons.extend(f"RISK_GATE:{reason}" for reason in risk_reasons)
        quality_flags.extend(option_quality_flags)
        if option_iv is None:
            quality_flags.append("OPTION_IV_FALLBACK_REALIZED_VOL")
        elif option_iv <= 0:
            skip_reasons.append("INVALID_OPTION_IV")
        option_iv_status = "IV_VALID" if option_iv is not None and option_iv > 0 else "IV_FALLBACK_REALIZED_VOL"
        if option_iv is None and any("OPTION_IV_STALE" in flag for flag in quality_flags):
            option_iv_status = "IV_STALE"
        elif option_iv is None and any(
            "OPTION_IV_UNAVAILABLE" in flag or "OPTION_IV_PROVIDER_UNCONFIGURED" in flag for flag in quality_flags
        ):
            option_iv_status = "IV_UNAVAILABLE"
        # Shadow collection uses the reviewed 2% buffer consistently. Missing or stale IV
        # remains an entry gate; it does not silently widen the probability haircut.
        active_fallback_buffer = self._fallback_buffer
        active_model_error_buffer = self._base_model_error_buffer + active_fallback_buffer + additional_model_error_buffer
        if reference_spot is not None and reference_spot_age_seconds is not None and reference_spot_age_seconds <= 15 and spot is not None:
            cross_source_difference = relative_price_difference(spot, reference_spot)
            if cross_source_difference > maximum_cross_source_difference:
                skip_reasons.append("CROSS_SOURCE_SPOT_DIVERGENCE")
        elif reference_spot is not None:
            quality_flags.append("REFERENCE_SPOT_NOT_FRESH")

        common = {
            "evaluated_at": now,
            "market_id": self._market_id,
            "symbol": self._symbol,
            "spot_provider": self._spot_provider,
            "spot": spot,
            "reference_spot": reference_spot,
            "reference_spot_age_seconds": reference_spot_age_seconds,
            "cross_source_difference": cross_source_difference,
            "option_iv": option_iv,
            "option_skew": option_skew,
            "option_iv_provider": option_iv_provider,
            "option_iv_age_seconds": option_iv_age_seconds,
            "option_iv_status": option_iv_status,
            "up_ask": up_ask,
            "down_ask": down_ask,
            "up_bid": up_bid,
            "down_bid": down_bid,
            "up_fee_rate": self._up_fee_rate,
            "down_fee_rate": self._down_fee_rate,
            "spot_age_seconds": spot_age_seconds,
            "book_age_seconds": book_age_seconds,
            "stream_ready": stream_ready,
            "market_session": market_session,
            "daily_data_is_fresh": daily_data_is_fresh,
            "model_error_buffer": active_model_error_buffer,
        }
        if skip_reasons:
            return RealtimeEvaluation(
                **common,
                fair_up_probability=None,
                annualized_realized_volatility=None,
                prior_close=None,
                up_edge=None,
                down_edge=None,
                up_taker_fee=None,
                down_taker_fee=None,
                model_outcome=None,
                paper_outcome=None,
                paper_entry_eligible=False,
                paper_entry_block_reasons=("EVALUATION_SKIPPED",),
                quality_flags=tuple(sorted(set(quality_flags))),
                trigger_reasons=trigger_reasons,
                skip_reasons=tuple(sorted(set(skip_reasons))),
            )

        realized_volatility = annualized_realized_volatility(self._closes, 20)
        # IV controls the distribution only when the provider passed its quote-quality checks.
        combined_volatility = 0.75 * option_iv + 0.25 * realized_volatility if option_iv is not None else None
        assessment = evaluate_realized_vol_baseline(
            spot=spot,
            closes=self._closes,
            seconds_to_resolution=(self._resolves_at - now).total_seconds(),
            up_ask=up_ask,
            down_ask=down_ask,
            up_fee_rate=self._up_fee_rate,
            down_fee_rate=self._down_fee_rate,
            base_model_error_buffer=self._base_model_error_buffer,
            fallback_buffer=active_fallback_buffer,
            minimum_edge=self._minimum_edge,
            data_is_fresh=daily_data_is_fresh,
            lookback_days=20,
            annualized_volatility_override=combined_volatility,
            additional_model_error_buffer=additional_model_error_buffer,
            price_to_beat_override=self._price_to_beat,
        )
        model_outcome = assessment.paper_outcome
        entry_block_reasons: list[str] = []
        if model_outcome and option_iv_status != "IV_VALID":
            entry_block_reasons.append("OPTION_IV_REQUIRED_FOR_PAPER_ENTRY")
        return RealtimeEvaluation(
            **common,
            fair_up_probability=assessment.fair_up_probability,
            annualized_realized_volatility=assessment.annualized_realized_volatility,
            prior_close=assessment.prior_close,
            up_edge=assessment.up_edge.edge,
            down_edge=assessment.down_edge.edge,
            up_taker_fee=assessment.up_edge.estimated_taker_fee,
            down_taker_fee=assessment.down_edge.estimated_taker_fee,
            model_outcome=model_outcome,
            paper_outcome=model_outcome if not entry_block_reasons else None,
            # Eligibility means an actionable paper entry, not merely that IV was valid.
            paper_entry_eligible=model_outcome is not None and not entry_block_reasons,
            paper_entry_block_reasons=tuple(entry_block_reasons),
            quality_flags=tuple(sorted(set(quality_flags))),
            trigger_reasons=trigger_reasons,
            skip_reasons=(),
        )
