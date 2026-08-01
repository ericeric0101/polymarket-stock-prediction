"""Non-leaking walk-forward evaluation for a capped daily checkpoint policy."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from statistics import mean
from typing import Iterable, Mapping

from .buffer_sweep import BufferSweepTrade, _candidate_trade
from .journal import BufferSweepObservation
from .metrics import calibration_metrics


@dataclass(frozen=True)
class TopFivePolicy:
    checkpoints: tuple[str, ...]
    buffer: float
    minimum_edge: float
    max_daily_entries: int

    def as_payload(self) -> Mapping[str, object]:
        return {
            "checkpoints": list(self.checkpoints),
            "buffer": self.buffer,
            "minimum_edge": self.minimum_edge,
            "max_daily_entries": self.max_daily_entries,
        }


@dataclass(frozen=True)
class TopFivePolicyResult:
    policy: TopFivePolicy
    eligible_market_days: int
    selected_trades: int
    wins: int
    win_rate: float | None
    total_realized_pnl: float
    average_realized_pnl: float | None
    average_entry_edge: float | None
    worst_daily_realized_pnl: float | None
    brier_score: float | None
    log_loss: float | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "policy": self.policy.as_payload(),
            "eligible_market_days": self.eligible_market_days,
            "selected_trades": self.selected_trades,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "total_realized_pnl": self.total_realized_pnl,
            "average_realized_pnl": self.average_realized_pnl,
            "average_entry_edge": self.average_entry_edge,
            "worst_daily_realized_pnl": self.worst_daily_realized_pnl,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
        }


@dataclass(frozen=True)
class TopFiveWalkForwardWindow:
    training_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    selected_policy: TopFivePolicy | None
    training_result: TopFivePolicyResult | None
    validation_result: TopFivePolicyResult | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "training_dates": list(self.training_dates),
            "validation_dates": list(self.validation_dates),
            "selected_policy": self.selected_policy.as_payload() if self.selected_policy else None,
            "training_result": self.training_result.as_payload() if self.training_result else None,
            "validation_result": self.validation_result.as_payload() if self.validation_result else None,
        }


@dataclass(frozen=True)
class TopFiveWalkForwardReport:
    status: str
    distinct_dates: int
    training_days: int
    validation_days: int
    candidate_policies: int
    windows: tuple[TopFiveWalkForwardWindow, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "distinct_dates": self.distinct_dates,
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "candidate_policies": self.candidate_policies,
            "windows": [window.as_payload() for window in self.windows],
        }


def checkpoint_sets(checkpoints: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Return chronological non-empty subsets, used only as a small fixed search grid."""

    items = tuple(checkpoints)
    if not items or len(set(items)) != len(items):
        raise ValueError("checkpoints must be non-empty and unique")
    return tuple(combo for size in range(1, len(items) + 1) for combo in combinations(items, size))


def parse_checkpoint_sets(value: str, *, allowed: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    allowed_tuple = tuple(allowed)
    if not value.strip():
        return checkpoint_sets(allowed_tuple)
    sets = []
    for group in value.split(";"):
        item = tuple(name.strip() for name in group.split(",") if name.strip())
        if not item or any(name not in allowed_tuple for name in item):
            raise ValueError("checkpoint sets must use the supplied checkpoint names")
        if len(set(item)) != len(item):
            raise ValueError("checkpoint set contains a duplicate checkpoint")
        sets.append(item)
    return tuple(sets)


def parse_probability_values(value: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if not values or any(item < 0 or item >= 1 for item in values):
        raise ValueError("probability values must be between 0 and 1")
    return values


def top_five_policies(
    *, checkpoint_groups: Iterable[tuple[str, ...]], buffers: Iterable[float], minimum_edges: Iterable[float],
    max_daily_entries: int,
) -> tuple[TopFivePolicy, ...]:
    if max_daily_entries < 1:
        raise ValueError("max_daily_entries must be positive")
    policies = tuple(
        TopFivePolicy(checkpoints=checkpoints, buffer=buffer, minimum_edge=minimum_edge,
                      max_daily_entries=max_daily_entries)
        for checkpoints in checkpoint_groups
        for buffer in buffers
        for minimum_edge in minimum_edges
    )
    if not policies or any(policy.minimum_edge < 0 for policy in policies):
        raise ValueError("at least one non-negative minimum edge is required")
    return policies


def run_top_five_policy(
    observations: Iterable[BufferSweepObservation], *, policy: TopFivePolicy,
) -> TopFivePolicyResult:
    """Replay an executable, chronological, maximum-five-entries-per-day policy."""

    items = tuple(item for item in observations if item.checkpoint_name in policy.checkpoints)
    market_days = {(item.checkpoint_date, item.market_id) for item in items}
    by_date_and_checkpoint: dict[tuple[str, str], list[BufferSweepObservation]] = {}
    for item in items:
        by_date_and_checkpoint.setdefault((item.checkpoint_date, item.checkpoint_name), []).append(item)
    trades: list[BufferSweepTrade] = []
    for checkpoint_date in sorted({item.checkpoint_date for item in items}):
        entered_markets: set[str] = set()
        remaining = policy.max_daily_entries
        for checkpoint_name in policy.checkpoints:
            if remaining == 0:
                break
            candidates = []
            for item in by_date_and_checkpoint.get((checkpoint_date, checkpoint_name), ()):
                if item.market_id in entered_markets:
                    continue
                trade = _candidate_trade(item, policy.buffer, policy.minimum_edge)
                if trade is not None:
                    candidates.append(trade)
            # The order-book snapshot is simultaneous within a checkpoint, so ranking does not leak later data.
            for trade in sorted(candidates, key=lambda item: (-item.edge, item.market_id))[:remaining]:
                trades.append(trade)
                entered_markets.add(trade.market_id)
                remaining -= 1
    if not trades:
        return TopFivePolicyResult(policy, len(market_days), 0, 0, None, 0.0, None, None, None, None, None)
    wins = sum(trade.won for trade in trades)
    daily_pnl: dict[str, float] = {}
    for trade in trades:
        daily_pnl[trade.checkpoint_date] = daily_pnl.get(trade.checkpoint_date, 0.0) + trade.realized_pnl
    metrics = calibration_metrics(tuple((trade.raw_probability, trade.won) for trade in trades))
    return TopFivePolicyResult(
        policy=policy, eligible_market_days=len(market_days), selected_trades=len(trades), wins=wins,
        win_rate=wins / len(trades), total_realized_pnl=sum(trade.realized_pnl for trade in trades),
        average_realized_pnl=mean(trade.realized_pnl for trade in trades),
        average_entry_edge=mean(trade.edge for trade in trades),
        worst_daily_realized_pnl=min(daily_pnl.values()), brier_score=metrics.brier_score,
        log_loss=metrics.log_loss,
    )


def walk_forward_top_five_policy(
    observations: Iterable[BufferSweepObservation], *, policies: Iterable[TopFivePolicy], training_days: int,
    validation_days: int, minimum_training_trades: int = 5,
) -> TopFiveWalkForwardReport:
    if training_days < 1 or validation_days < 1 or minimum_training_trades < 1:
        raise ValueError("walk-forward windows and minimum_training_trades must be positive")
    policy_values = tuple(policies)
    items = tuple(observations)
    dates = tuple(sorted({item.checkpoint_date for item in items}))
    if len(dates) < training_days + validation_days:
        return TopFiveWalkForwardReport("INSUFFICIENT_DISTINCT_DAYS", len(dates), training_days, validation_days,
                                        len(policy_values), ())
    windows = []
    for start in range(0, len(dates) - training_days - validation_days + 1, validation_days):
        training_dates = dates[start:start + training_days]
        validation_dates = dates[start + training_days:start + training_days + validation_days]
        training_items = tuple(item for item in items if item.checkpoint_date in training_dates)
        validation_items = tuple(item for item in items if item.checkpoint_date in validation_dates)
        training_results = [run_top_five_policy(training_items, policy=policy) for policy in policy_values]
        eligible = [result for result in training_results if result.selected_trades >= minimum_training_trades]
        if not eligible:
            windows.append(TopFiveWalkForwardWindow(training_dates, validation_dates, None, None, None))
            continue
        selected = max(
            eligible,
            key=lambda result: (
                result.total_realized_pnl,
                result.average_realized_pnl or float("-inf"),
                result.policy.minimum_edge,
                result.policy.buffer,
                -len(result.policy.checkpoints),
            ),
        )
        validation_result = run_top_five_policy(validation_items, policy=selected.policy)
        windows.append(TopFiveWalkForwardWindow(
            training_dates, validation_dates, selected.policy, selected, validation_result,
        ))
    return TopFiveWalkForwardReport("READY", len(dates), training_days, validation_days, len(policy_values), tuple(windows))


__all__ = [
    "TopFivePolicy", "TopFivePolicyResult", "checkpoint_sets", "parse_checkpoint_sets", "parse_probability_values",
    "run_top_five_policy", "top_five_policies", "walk_forward_top_five_policy",
]
