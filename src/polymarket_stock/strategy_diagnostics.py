"""Unified diagnostics for model value, execution quality, data risk, and exits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import log, sqrt
from statistics import mean
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .fees import estimate_taker_fee_usdc
from .journal import BufferSweepObservation, ExecutionObservation, SpotSourceComparison, StoredSpotObservation


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DirectionBenchmark:
    name: str
    observations: int
    wins: int
    win_rate: float | None
    total_pnl_per_share: float
    average_pnl_per_share: float | None


@dataclass(frozen=True)
class ExecutionQualitySummary:
    signals: int
    requested_shares: float
    depth_fillable_signals: int
    average_depth_slippage: float | None
    delayed_entry_slippage: Mapping[str, float | None]


@dataclass(frozen=True)
class SpotDivergenceSummary:
    total_observations: int
    excluded_stale_or_unstamped: int
    observations: int
    symbols: int
    median_absolute_bps: float | None
    p95_absolute_bps: float | None
    p99_absolute_bps: float | None
    above_25_bps: int
    above_50_bps: int
    by_symbol: Mapping[str, Mapping[str, float | int | None]]


@dataclass(frozen=True)
class VolatilityComparisonSummary:
    observations: int
    direction_disagreements: int
    large_probability_disagreements: int
    mean_absolute_probability_difference: float | None
    by_estimator: Mapping[str, Mapping[str, float | int | None]]


@dataclass(frozen=True)
class IntradayVolatilitySummary:
    checkpoint_paths: int
    history_comparisons: int
    high_regime_count: int
    median_intraday_annualized_volatility: float | None
    mean_intraday_to_daily_model_ratio: float | None
    by_checkpoint: Mapping[str, Mapping[str, float | int | None]]


@dataclass(frozen=True)
class ExitHorizonSummary:
    horizon: str
    positions: int
    liquid_positions: int
    total_exit_pnl: float
    total_hold_pnl: float
    exit_minus_hold: float


@dataclass(frozen=True)
class StrategyDiagnosticsReport:
    benchmarks: tuple[DirectionBenchmark, ...]
    model_incremental_win_rate_vs_market: float | None
    model_incremental_pnl_vs_market: float | None
    execution: ExecutionQualitySummary
    spot_divergence: SpotDivergenceSummary
    volatility: VolatilityComparisonSummary
    intraday_volatility: IntradayVolatilitySummary
    exits: tuple[ExitHorizonSummary, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "benchmarks": [asdict(item) for item in self.benchmarks],
            "model_incremental_win_rate_vs_market": self.model_incremental_win_rate_vs_market,
            "model_incremental_pnl_vs_market": self.model_incremental_pnl_vs_market,
            "execution": asdict(self.execution),
            "spot_divergence": asdict(self.spot_divergence),
            "volatility": asdict(self.volatility),
            "intraday_volatility": asdict(self.intraday_volatility),
            "exits": [asdict(item) for item in self.exits],
        }


def strategy_diagnostics(
    checkpoints: Iterable[BufferSweepObservation], executions: Iterable[ExecutionObservation],
    comparisons: Iterable[SpotSourceComparison], *, spots: Iterable[StoredSpotObservation] = (),
    requested_shares: float = 10,
) -> StrategyDiagnosticsReport:
    checkpoint_items = tuple(checkpoints)
    benchmarks = direction_benchmarks(checkpoint_items)
    by_name = {item.name: item for item in benchmarks}
    model = by_name.get("MODEL_DIRECTION")
    market = by_name.get("MARKET_FAVORITE")
    win_delta = (
        model.win_rate - market.win_rate
        if model and market and model.win_rate is not None and market.win_rate is not None else None
    )
    pnl_delta = (
        model.average_pnl_per_share - market.average_pnl_per_share
        if model and market and model.average_pnl_per_share is not None and market.average_pnl_per_share is not None
        else None
    )
    execution_items = tuple(executions)
    return StrategyDiagnosticsReport(
        benchmarks=benchmarks,
        model_incremental_win_rate_vs_market=win_delta,
        model_incremental_pnl_vs_market=pnl_delta,
        execution=execution_quality(execution_items, requested_shares=requested_shares),
        spot_divergence=spot_divergence_summary(comparisons),
        volatility=volatility_comparison_summary(checkpoint_items),
        intraday_volatility=intraday_volatility_summary(spots, checkpoint_items),
        exits=exit_horizon_replay(execution_items, checkpoint_items, requested_shares=requested_shares),
    )


def direction_benchmarks(observations: Iterable[BufferSweepObservation]) -> tuple[DirectionBenchmark, ...]:
    items = tuple(item for item in observations if item.up_ask is not None and item.down_ask is not None)
    majority: dict[tuple[str, str], str] = {}
    groups: dict[tuple[str, str], list[BufferSweepObservation]] = {}
    for item in items:
        groups.setdefault((item.checkpoint_date, item.checkpoint_name), []).append(item)
    for key, values in groups.items():
        up_votes = sum(float(item.up_ask) >= float(item.down_ask) for item in values)
        majority[key] = "UP" if up_votes >= len(values) / 2 else "DOWN"
    selectors = (
        ("MODEL_DIRECTION", lambda item: "UP" if item.fair_up_probability >= 0.5 else "DOWN"),
        ("MARKET_FAVORITE", lambda item: "UP" if float(item.up_ask) >= float(item.down_ask) else "DOWN"),
        ("SPOT_VS_THRESHOLD", _spot_direction),
        ("MARKET_MAJORITY", lambda item: majority[(item.checkpoint_date, item.checkpoint_name)]),
    )
    results = []
    for name, selector in selectors:
        scored = []
        for item in items:
            outcome = selector(item)
            if outcome is None:
                continue
            ask = item.up_ask if outcome == "UP" else item.down_ask
            fee = item.up_taker_fee if outcome == "UP" else item.down_taker_fee
            if ask is None or fee is None:
                continue
            won = outcome == item.winning_outcome
            scored.append((won, (1.0 if won else 0.0) - ask - fee))
        wins = sum(item[0] for item in scored)
        results.append(DirectionBenchmark(
            name, len(scored), wins, wins / len(scored) if scored else None,
            sum(item[1] for item in scored), mean(item[1] for item in scored) if scored else None,
        ))
    return tuple(results)


def execution_quality(
    observations: Iterable[ExecutionObservation], *, requested_shares: float,
) -> ExecutionQualitySummary:
    if requested_shares <= 0:
        raise ValueError("requested_shares must be positive")
    items = tuple(observations)
    entries = _selected_entries(items)
    slippages = []
    delayed: dict[str, list[float]] = {name: [] for name in ("MARKOUT_60S", "MARKOUT_300S", "MARKOUT_900S", "MARKOUT_1800S")}
    by_signal_kind = {(item.signal_id, item.observation_kind, item.outcome): item for item in items if item.signal_id}
    for entry in entries:
        initial_vwap = book_vwap(entry.book_payload, side="asks", shares=requested_shares)
        if initial_vwap is not None and entry.best_ask is not None:
            slippages.append(initial_vwap - entry.best_ask)
        for kind in delayed:
            later = by_signal_kind.get((entry.signal_id, kind, entry.outcome))
            if later is None or entry.best_ask is None:
                continue
            later_vwap = book_vwap(later.book_payload, side="asks", shares=requested_shares)
            if later_vwap is not None:
                delayed[kind].append(later_vwap - entry.best_ask)
    return ExecutionQualitySummary(
        signals=len(entries), requested_shares=requested_shares, depth_fillable_signals=len(slippages),
        average_depth_slippage=mean(slippages) if slippages else None,
        delayed_entry_slippage={name: mean(values) if values else None for name, values in delayed.items()},
    )


def spot_divergence_summary(comparisons: Iterable[SpotSourceComparison]) -> SpotDivergenceSummary:
    all_items = tuple(comparisons)
    items = tuple(item for item in all_items if _comparison_is_fresh(item))
    absolute = sorted(abs(item.difference_bps) for item in items)
    by_symbol_values: dict[str, list[float]] = {}
    for item in items:
        by_symbol_values.setdefault(item.symbol, []).append(abs(item.difference_bps))
    by_symbol = {
        symbol: {
            "observations": len(values), "mean_absolute_bps": mean(values),
            "p95_absolute_bps": _percentile(sorted(values), 0.95),
            "above_50_bps": sum(value > 50 for value in values),
        }
        for symbol, values in sorted(by_symbol_values.items())
    }
    return SpotDivergenceSummary(
        total_observations=len(all_items), excluded_stale_or_unstamped=len(all_items) - len(items),
        observations=len(items), symbols=len(by_symbol), median_absolute_bps=_percentile(absolute, 0.5),
        p95_absolute_bps=_percentile(absolute, 0.95), p99_absolute_bps=_percentile(absolute, 0.99),
        above_25_bps=sum(value > 25 for value in absolute), above_50_bps=sum(value > 50 for value in absolute),
        by_symbol=by_symbol,
    )


def volatility_comparison_summary(
    observations: Iterable[BufferSweepObservation], *, large_difference: float = 0.10,
) -> VolatilityComparisonSummary:
    differences = []
    direction_disagreements = 0
    by_estimator_values: dict[str, list[tuple[float, bool]]] = {}
    for item in observations:
        for comparison in item.comparison_models:
            try:
                probability = float(comparison["fair_up_probability"])
            except (KeyError, TypeError, ValueError):
                continue
            difference = abs(probability - item.fair_up_probability)
            disagreed = (probability >= 0.5) != (item.fair_up_probability >= 0.5)
            differences.append(difference)
            direction_disagreements += int(disagreed)
            estimator = str(comparison.get("volatility_estimator") or "UNKNOWN")
            by_estimator_values.setdefault(estimator, []).append((difference, disagreed))
    return VolatilityComparisonSummary(
        observations=len(differences), direction_disagreements=direction_disagreements,
        large_probability_disagreements=sum(value >= large_difference for value in differences),
        mean_absolute_probability_difference=mean(differences) if differences else None,
        by_estimator={
            estimator: {
                "observations": len(values),
                "mean_absolute_probability_difference": mean(value[0] for value in values),
                "direction_disagreements": sum(value[1] for value in values),
            }
            for estimator, values in sorted(by_estimator_values.items())
        },
    )


def intraday_volatility_summary(
    spots: Iterable[StoredSpotObservation], checkpoints: Iterable[BufferSweepObservation], *,
    high_regime_ratio: float = 1.5,
) -> IntradayVolatilitySummary:
    """Compare partial-session Pyth realized volatility with prior matching checkpoints."""

    paths: dict[tuple[str, str], list[StoredSpotObservation]] = {}
    for spot in spots:
        local_date = spot.observed_at.astimezone(NEW_YORK).date().isoformat()
        paths.setdefault((spot.symbol.upper(), local_date), []).append(spot)
    for values in paths.values():
        values.sort(key=lambda item: item.observed_at)

    history: dict[tuple[str, str], list[tuple[str, float]]] = {}
    checkpoint_values: dict[str, list[tuple[float, float | None, float | None, bool]]] = {}
    all_intraday: list[float] = []
    all_model_ratios: list[float] = []
    history_comparisons = 0
    high_regime_count = 0
    ordered = sorted(
        checkpoints,
        key=lambda item: (item.checkpoint_date, item.checkpoint_name, item.symbol, item.evaluated_at),
    )
    for item in ordered:
        samples = [
            sample for sample in paths.get((item.symbol.upper(), item.checkpoint_date), ())
            if sample.observed_at <= item.evaluated_at
        ]
        prices = [sample.price for sample in samples if sample.price > 0]
        if len(prices) < 3:
            continue
        realized_variance = sum(log(current / previous) ** 2 for previous, current in zip(prices, prices[1:]))
        intraday_annualized = sqrt(realized_variance * 252)
        model_ratio = (
            intraday_annualized / item.annualized_volatility
            if item.annualized_volatility is not None and item.annualized_volatility > 0 else None
        )
        history_key = (item.symbol.upper(), item.checkpoint_name)
        prior = [value for date, value in history.get(history_key, ()) if date < item.checkpoint_date]
        prior_mean = mean(prior) if prior else None
        history_ratio = intraday_annualized / prior_mean if prior_mean is not None and prior_mean > 0 else None
        high_regime = history_ratio is not None and history_ratio >= high_regime_ratio
        if history_ratio is not None:
            history_comparisons += 1
            high_regime_count += int(high_regime)
        if not any(date == item.checkpoint_date for date, _ in history.get(history_key, ())):
            history.setdefault(history_key, []).append((item.checkpoint_date, intraday_annualized))
        checkpoint_values.setdefault(item.checkpoint_name, []).append(
            (intraday_annualized, model_ratio, history_ratio, high_regime)
        )
        all_intraday.append(intraday_annualized)
        if model_ratio is not None:
            all_model_ratios.append(model_ratio)

    by_checkpoint = {}
    for checkpoint_name, values in sorted(checkpoint_values.items()):
        model_ratios = [value[1] for value in values if value[1] is not None]
        history_ratios = [value[2] for value in values if value[2] is not None]
        by_checkpoint[checkpoint_name] = {
            "paths": len(values),
            "mean_intraday_annualized_volatility": mean(value[0] for value in values),
            "mean_intraday_to_daily_model_ratio": mean(model_ratios) if model_ratios else None,
            "history_comparisons": len(history_ratios),
            "mean_same_checkpoint_history_ratio": mean(history_ratios) if history_ratios else None,
            "high_regime_count": sum(value[3] for value in values),
        }
    return IntradayVolatilitySummary(
        checkpoint_paths=len(all_intraday), history_comparisons=history_comparisons,
        high_regime_count=high_regime_count,
        median_intraday_annualized_volatility=_percentile(sorted(all_intraday), 0.5),
        mean_intraday_to_daily_model_ratio=mean(all_model_ratios) if all_model_ratios else None,
        by_checkpoint=by_checkpoint,
    )


def exit_horizon_replay(
    observations: Iterable[ExecutionObservation], checkpoints: Iterable[BufferSweepObservation], *, requested_shares: float,
) -> tuple[ExitHorizonSummary, ...]:
    items = tuple(observations)
    entries = _selected_entries(items)
    settlements = {item.market_id: item.winning_outcome for item in checkpoints}
    by_signal_kind = {(item.signal_id, item.observation_kind, item.outcome): item for item in observations if item.signal_id}
    reports = []
    for kind in ("MARKOUT_60S", "MARKOUT_300S", "MARKOUT_900S", "MARKOUT_1800S"):
        exit_total = 0.0
        hold_total = 0.0
        liquid = 0
        for entry in entries:
            later = by_signal_kind.get((entry.signal_id, kind, entry.outcome))
            entry_vwap = book_vwap(entry.book_payload, side="asks", shares=requested_shares)
            exit_vwap = book_vwap(later.book_payload, side="bids", shares=requested_shares) if later else None
            winning_outcome = settlements.get(entry.market_id)
            if entry_vwap is None or exit_vwap is None or winning_outcome is None or entry.fee_rate is None:
                continue
            buy_fee = estimate_taker_fee_usdc(shares=requested_shares, price=entry_vwap, fee_rate=entry.fee_rate)
            sell_rate = later.fee_rate if later and later.fee_rate is not None else entry.fee_rate
            sell_fee = estimate_taker_fee_usdc(shares=requested_shares, price=exit_vwap, fee_rate=sell_rate)
            entry_cost = entry_vwap * requested_shares + buy_fee
            exit_total += exit_vwap * requested_shares - sell_fee - entry_cost
            hold_total += (requested_shares if entry.outcome == winning_outcome else 0.0) - entry_cost
            liquid += 1
        reports.append(ExitHorizonSummary(
            kind, len(entries), liquid, exit_total, hold_total, exit_total - hold_total,
        ))
    return tuple(reports)


def book_vwap(book: Mapping[str, object], *, side: str, shares: float) -> float | None:
    if side not in {"asks", "bids"} or shares <= 0:
        raise ValueError("side must be asks or bids and shares must be positive")
    levels = book.get(side)
    if not isinstance(levels, list):
        return None
    parsed = []
    for item in levels:
        if not isinstance(item, Mapping):
            continue
        try:
            price, size = float(item["price"]), float(item["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= price <= 1 and size > 0:
            parsed.append((price, size))
    parsed.sort(key=lambda item: item[0], reverse=side == "bids")
    remaining = shares
    notional = 0.0
    for price, size in parsed:
        filled = min(remaining, size)
        notional += filled * price
        remaining -= filled
        if remaining <= 1e-12:
            return notional / shares
    return None


def _selected_entries(observations: Iterable[ExecutionObservation]) -> tuple[ExecutionObservation, ...]:
    selected = []
    for item in observations:
        if item.signal_id is None or item.observation_kind != "PAPER_ENTRY":
            continue
        paper_outcome = str(item.evaluation_payload.get("paper_outcome") or "")
        if item.outcome == paper_outcome:
            selected.append(item)
    return tuple(selected)


def _comparison_is_fresh(item: SpotSourceComparison, maximum_age_seconds: float = 15.0) -> bool:
    if item.primary_published_at is None or item.pyth_published_at is None:
        return False
    primary_age = (item.observed_at - item.primary_published_at).total_seconds()
    pyth_age = (item.observed_at - item.pyth_published_at).total_seconds()
    return 0 <= primary_age <= maximum_age_seconds and 0 <= pyth_age <= maximum_age_seconds


def _spot_direction(item: BufferSweepObservation) -> str | None:
    if item.spot is None or item.price_to_beat is None:
        return None
    return "UP" if item.spot > item.price_to_beat else "DOWN"


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * quantile)))
    return values[index]
