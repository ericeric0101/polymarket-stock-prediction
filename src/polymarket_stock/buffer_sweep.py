"""Non-leaking replay tools for conservative probability-buffer research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Iterable, Mapping

from .journal import BufferSweepObservation
from .metrics import calibration_metrics


@dataclass(frozen=True)
class BufferSweepTrade:
    market_id: str
    symbol: str
    checkpoint_date: str
    checkpoint_name: str
    outcome: str
    raw_probability: float
    conservative_probability: float
    entry_ask: float
    entry_fee: float
    edge: float
    won: bool
    realized_pnl: float


@dataclass(frozen=True)
class BufferSweepResult:
    buffer: float
    minimum_edge: float
    eligible_market_days: int
    selected_trades: int
    wins: int
    win_rate: float | None
    coverage: float
    total_realized_pnl: float
    average_realized_pnl: float | None
    average_entry_edge: float | None
    brier_score: float | None
    log_loss: float | None

    def as_payload(self) -> Mapping[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BufferSweepReport:
    observation_count: int
    eligible_market_days: int
    checkpoints: tuple[str, ...]
    results: tuple[BufferSweepResult, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "observation_count": self.observation_count,
            "eligible_market_days": self.eligible_market_days,
            "checkpoints": list(self.checkpoints),
            "results": [result.as_payload() for result in self.results],
        }


@dataclass(frozen=True)
class WalkForwardWindow:
    training_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    selected_buffer: float | None
    training_result: BufferSweepResult | None
    validation_result: BufferSweepResult | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "training_dates": list(self.training_dates),
            "validation_dates": list(self.validation_dates),
            "selected_buffer": self.selected_buffer,
            "training_result": self.training_result.as_payload() if self.training_result else None,
            "validation_result": self.validation_result.as_payload() if self.validation_result else None,
        }


@dataclass(frozen=True)
class WalkForwardReport:
    status: str
    distinct_dates: int
    training_days: int
    validation_days: int
    windows: tuple[WalkForwardWindow, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "distinct_dates": self.distinct_dates,
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "windows": [window.as_payload() for window in self.windows],
        }


def buffer_values(minimum: float, maximum: float, step: float) -> tuple[float, ...]:
    if minimum < 0 or maximum < minimum or step <= 0:
        raise ValueError("invalid buffer range")
    values = []
    index = 0
    while True:
        value = round(minimum + index * step, 12)
        if value > maximum + 1e-12:
            break
        values.append(value)
        index += 1
    return tuple(values)


def run_buffer_sweep(
    observations: Iterable[BufferSweepObservation],
    *,
    buffers: Iterable[float],
    minimum_edge: float,
    checkpoint_name: str | None = None,
) -> BufferSweepReport:
    if minimum_edge < 0:
        raise ValueError("minimum_edge must be non-negative")
    buffer_values_tuple = tuple(buffers)
    if not buffer_values_tuple or any(buffer < 0 or buffer >= 1 for buffer in buffer_values_tuple):
        raise ValueError("buffers must contain values between 0 and 1")
    items = tuple(item for item in observations if checkpoint_name is None or item.checkpoint_name == checkpoint_name)
    market_days = {(item.checkpoint_date, item.market_id) for item in items}
    checkpoints = tuple(sorted({item.checkpoint_name for item in items}))
    return BufferSweepReport(
        observation_count=len(items),
        eligible_market_days=len(market_days),
        checkpoints=checkpoints,
        results=tuple(_run_buffer(items, buffer, minimum_edge) for buffer in buffer_values_tuple),
    )


def walk_forward_buffer_sweep(
    observations: Iterable[BufferSweepObservation],
    *,
    buffers: Iterable[float],
    minimum_edge: float,
    training_days: int,
    validation_days: int,
    minimum_training_trades: int = 10,
    checkpoint_name: str | None = None,
) -> WalkForwardReport:
    if training_days < 1 or validation_days < 1 or minimum_training_trades < 1:
        raise ValueError("walk-forward windows and minimum_training_trades must be positive")
    buffer_values_tuple = tuple(buffers)
    items = tuple(item for item in observations if checkpoint_name is None or item.checkpoint_name == checkpoint_name)
    dates = tuple(sorted({item.checkpoint_date for item in items}))
    if len(dates) < training_days + validation_days:
        return WalkForwardReport("INSUFFICIENT_DISTINCT_DAYS", len(dates), training_days, validation_days, ())
    windows = []
    for start in range(0, len(dates) - training_days - validation_days + 1, validation_days):
        training_dates = dates[start : start + training_days]
        validation_dates = dates[start + training_days : start + training_days + validation_days]
        training_items = tuple(item for item in items if item.checkpoint_date in training_dates)
        validation_items = tuple(item for item in items if item.checkpoint_date in validation_dates)
        training_report = run_buffer_sweep(training_items, buffers=buffer_values_tuple, minimum_edge=minimum_edge)
        eligible = [result for result in training_report.results if result.selected_trades >= minimum_training_trades]
        if not eligible:
            windows.append(WalkForwardWindow(training_dates, validation_dates, None, None, None))
            continue
        selected = max(
            eligible,
            key=lambda result: (
                result.total_realized_pnl,
                result.average_realized_pnl or float("-inf"),
                -result.buffer,
            ),
        )
        validation_result = _run_buffer(validation_items, selected.buffer, minimum_edge)
        windows.append(
            WalkForwardWindow(training_dates, validation_dates, selected.buffer, selected, validation_result)
        )
    return WalkForwardReport(
        "READY" if windows else "INSUFFICIENT_DISTINCT_DAYS", len(dates), training_days, validation_days, tuple(windows)
    )


def _run_buffer(
    observations: tuple[BufferSweepObservation, ...],
    buffer: float,
    minimum_edge: float,
) -> BufferSweepResult:
    grouped: dict[tuple[str, str], list[BufferSweepObservation]] = {}
    for item in observations:
        grouped.setdefault((item.checkpoint_date, item.market_id), []).append(item)
    trades = []
    for items in grouped.values():
        for item in sorted(items, key=lambda value: value.evaluated_at):
            trade = _candidate_trade(item, buffer, minimum_edge)
            if trade is not None:
                trades.append(trade)
                break
    if not trades:
        return BufferSweepResult(buffer, minimum_edge, len(grouped), 0, 0, None, 0.0, 0.0, None, None, None, None)
    predictions = [(trade.raw_probability, trade.won) for trade in trades]
    metrics = calibration_metrics(predictions)
    wins = sum(trade.won for trade in trades)
    return BufferSweepResult(
        buffer=buffer,
        minimum_edge=minimum_edge,
        eligible_market_days=len(grouped),
        selected_trades=len(trades),
        wins=wins,
        win_rate=wins / len(trades),
        coverage=len(trades) / len(grouped),
        total_realized_pnl=sum(trade.realized_pnl for trade in trades),
        average_realized_pnl=mean(trade.realized_pnl for trade in trades),
        average_entry_edge=mean(trade.edge for trade in trades),
        brier_score=metrics.brier_score,
        log_loss=metrics.log_loss,
    )


def _candidate_trade(
    item: BufferSweepObservation,
    buffer: float,
    minimum_edge: float,
) -> BufferSweepTrade | None:
    candidates = []
    for outcome, raw_probability, ask, fee in (
        ("UP", item.fair_up_probability, item.up_ask, item.up_taker_fee),
        ("DOWN", 1.0 - item.fair_up_probability, item.down_ask, item.down_taker_fee),
    ):
        if ask is None or fee is None:
            continue
        conservative_probability = max(0.0, raw_probability - buffer)
        edge = conservative_probability - ask - fee
        if edge < minimum_edge:
            continue
        won = outcome == item.winning_outcome
        candidates.append(
            BufferSweepTrade(
                market_id=item.market_id,
                symbol=item.symbol,
                checkpoint_date=item.checkpoint_date,
                checkpoint_name=item.checkpoint_name,
                outcome=outcome,
                raw_probability=raw_probability,
                conservative_probability=conservative_probability,
                entry_ask=ask,
                entry_fee=fee,
                edge=edge,
                won=won,
                realized_pnl=(1.0 if won else 0.0) - ask - fee,
            )
        )
    return max(candidates, key=lambda candidate: candidate.edge) if candidates else None
