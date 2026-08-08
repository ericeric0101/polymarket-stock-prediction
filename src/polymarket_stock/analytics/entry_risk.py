"""Counterfactual entry-policy diagnostics for shadow research."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean

from ..evaluation_payload import read_entry_policy_category, read_model_outcome
from ..journal import BufferSweepObservation
from ..quality import executable_market_up_probability


@dataclass(frozen=True)
class EntryRiskCohort:
    """Historical outcome statistics for one diagnostic cohort."""

    name: str
    candidates: int
    wins: int
    win_rate: float | None
    priced_candidates: int
    total_pnl_per_share: float
    average_pnl_per_share: float | None


@dataclass(frozen=True)
class EntryRiskDiagnostics:
    """Read-only comparison of baseline and model-alignment policies."""

    candidates: int
    wins: int
    win_rate: float | None
    total_pnl_per_share: float
    average_pnl_per_share: float | None
    policy_comparison: tuple[EntryRiskCohort, ...]
    by_selected_probability: tuple[EntryRiskCohort, ...]
    by_threshold_distance: tuple[EntryRiskCohort, ...]
    by_market_divergence: tuple[EntryRiskCohort, ...]
    by_model_alignment: tuple[EntryRiskCohort, ...]


Candidate = tuple[bool, float | None, str, str, str, str]


def entry_risk_summary(observations: Iterable[BufferSweepObservation]) -> EntryRiskDiagnostics:
    """Compare a no-change baseline with a model-aligned counterfactual.

    This never changes a paper entry. It is a historical reporting policy used
    to determine, with future walk-forward data, whether contrarian value
    signals merit a production gate.
    """
    candidates: list[Candidate] = []
    for item in observations:
        selected_outcome = read_model_outcome(item.payload)
        if selected_outcome is None:
            continue
        selected_probability = item.fair_up_probability if selected_outcome == "UP" else 1.0 - item.fair_up_probability
        selected_ask = item.up_ask if selected_outcome == "UP" else item.down_ask
        selected_fee = item.up_taker_fee if selected_outcome == "UP" else item.down_taker_fee
        market_up = executable_market_up_probability(
            up_bid=item.up_bid,
            up_ask=item.up_ask,
            down_bid=item.down_bid,
            down_ask=item.down_ask,
        )
        threshold_distance_bps = (
            abs(item.spot - item.price_to_beat) / item.price_to_beat * 10_000.0
            if item.spot is not None and item.price_to_beat is not None and item.price_to_beat > 0
            else None
        )
        divergence = abs(item.fair_up_probability - market_up) if market_up is not None else None
        category = read_entry_policy_category(item.payload)
        if category == "UNKNOWN":
            model_majority = "UP" if item.fair_up_probability >= 0.5 else "DOWN"
            category = "MODEL_ALIGNED" if selected_outcome == model_majority else "CONTRARIAN_VALUE"
        alignment = "ALIGNS_MODEL_MAJORITY" if category == "MODEL_ALIGNED" else "CONTRADICTS_MODEL_MAJORITY"
        candidates.append(
            (
                selected_outcome == item.winning_outcome,
                None
                if selected_ask is None or selected_fee is None
                else (1.0 if selected_outcome == item.winning_outcome else 0.0) - selected_ask - selected_fee,
                _selected_probability_bucket(selected_probability),
                _threshold_distance_bucket(threshold_distance_bps),
                _market_divergence_bucket(divergence),
                alignment,
            )
        )
    baseline = _cohort("BASELINE_ALL_POSITIVE_EDGE", candidates)
    aligned = [item for item in candidates if item[5] == "ALIGNS_MODEL_MAJORITY"]
    contrarian = [item for item in candidates if item[5] == "CONTRADICTS_MODEL_MAJORITY"]
    return EntryRiskDiagnostics(
        candidates=baseline.candidates,
        wins=baseline.wins,
        win_rate=baseline.win_rate,
        total_pnl_per_share=baseline.total_pnl_per_share,
        average_pnl_per_share=baseline.average_pnl_per_share,
        policy_comparison=(
            baseline,
            _cohort("MODEL_ALIGNED_ONLY", aligned),
            _cohort("CONTRARIAN_VALUE", contrarian),
        ),
        by_selected_probability=_entry_risk_cohorts(candidates, index=2),
        by_threshold_distance=_entry_risk_cohorts(candidates, index=3),
        by_market_divergence=_entry_risk_cohorts(candidates, index=4),
        by_model_alignment=_entry_risk_cohorts(candidates, index=5),
    )


def _cohort(name: str, values: Iterable[Candidate]) -> EntryRiskCohort:
    entries = tuple(values)
    pnl = [item[1] for item in entries if item[1] is not None]
    wins = sum(item[0] for item in entries)
    return EntryRiskCohort(
        name=name,
        candidates=len(entries),
        wins=wins,
        win_rate=wins / len(entries) if entries else None,
        priced_candidates=len(pnl),
        total_pnl_per_share=sum(pnl),
        average_pnl_per_share=mean(pnl) if pnl else None,
    )


def _entry_risk_cohorts(candidates: Iterable[Candidate], *, index: int) -> tuple[EntryRiskCohort, ...]:
    grouped: dict[str, list[Candidate]] = {}
    for item in candidates:
        grouped.setdefault(item[index], []).append(item)
    return tuple(_cohort(name, values) for name, values in sorted(grouped.items()))


def _selected_probability_bucket(value: float) -> str:
    if value < 0.60:
        return "LT_60_PCT"
    if value < 0.85:
        return "60_TO_85_PCT"
    return "GTE_85_PCT"


def _threshold_distance_bucket(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value <= 25.0:
        return "LE_25_BPS"
    if value <= 100.0:
        return "26_TO_100_BPS"
    return "GT_100_BPS"


def _market_divergence_bucket(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value < 0.25:
        return "LT_25_PP"
    if value < 0.50:
        return "25_TO_50_PP"
    return "GTE_50_PP"
