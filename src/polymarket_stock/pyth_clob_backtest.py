"""Non-leaking batch replay from local Pyth minute spots and CLOB histories."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
from pathlib import Path
import csv
import json
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .baseline import DailyClose, annualized_realized_volatility
from .buffer_sweep import BufferSweepReport, WalkForwardReport, run_buffer_sweep, walk_forward_buffer_sweep
from .clob_history import PriceHistoryPoint
from .fees import estimate_taker_fee_usdc
from .historical_backtest import UnderlyingSpotPoint
from .journal import BufferSweepObservation
from .pricing import digital_up_probability


NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_CHECKPOINTS = ("1200_EDT", "1400_EDT", "1530_EDT")


@dataclass(frozen=True)
class PythClobBacktestReport:
    source: str
    execution_price_assumption: str
    fee_rate_assumption: float
    discovered_market_days: int
    eligible_market_days: int
    skipped_market_days: int
    observation_count: int
    checkpoints: tuple[str, ...]
    sweep: BufferSweepReport
    walk_forward: WalkForwardReport

    def as_payload(self) -> Mapping[str, object]:
        return {
            "source": self.source,
            "execution_price_assumption": self.execution_price_assumption,
            "fee_rate_assumption": self.fee_rate_assumption,
            "discovered_market_days": self.discovered_market_days,
            "eligible_market_days": self.eligible_market_days,
            "skipped_market_days": self.skipped_market_days,
            "observation_count": self.observation_count,
            "checkpoints": list(self.checkpoints),
            "sweep": self.sweep.as_payload(),
            "walk_forward": self.walk_forward.as_payload(),
        }


def run_pyth_clob_backtest(
    *, data_dir: Path, buffers: Iterable[float], minimum_edge: float = 0.02,
    lookback_days: int = 20, training_days: int = 20, validation_days: int = 5,
    minimum_training_trades: int = 10, fee_rate: float = 0.0,
    checkpoint_names: tuple[str, ...] = DEFAULT_CHECKPOINTS,
) -> PythClobBacktestReport:
    """Replay one entry per market-day from immutable local historical files.

    CLOB's price-history endpoint stores a historical price, not a historical
    executable ask. The report intentionally exposes that limitation.
    """

    if minimum_edge < 0 or fee_rate < 0:
        raise ValueError("minimum_edge and fee_rate must be non-negative")
    checkpoints = _checkpoint_times(checkpoint_names)
    records = _load_records(data_dir)
    closes_by_symbol: dict[str, list[DailyClose]] = {}
    for record in records:
        closes_by_symbol.setdefault(record.symbol, []).append(DailyClose(record.market_day, record.final_price))
    for closes in closes_by_symbol.values():
        closes.sort(key=lambda item: item.date)

    observations = []
    eligible_market_days = 0
    for record in records:
        close_history = [close for close in closes_by_symbol[record.symbol] if close.date < record.market_day]
        if len(close_history) < lookback_days + 1:
            continue
        eligible_market_days += 1
        volatility = annualized_realized_volatility(close_history, lookback_days)
        up = _read_price_history(record.up_clob_path)
        down = _read_price_history(record.down_clob_path)
        spots = _read_spots(record.spot_path)
        for name, checkpoint_time in checkpoints:
            evaluated_at = datetime.combine(datetime.fromisoformat(record.market_day).date(), checkpoint_time, tzinfo=NEW_YORK).astimezone(UTC)
            up_point = _latest_at_or_before(up, evaluated_at)
            down_point = _latest_at_or_before(down, evaluated_at)
            spot = _latest_at_or_before(spots, evaluated_at)
            if up_point is None or down_point is None or spot is None:
                continue
            seconds_to_resolution = (record.resolves_at - evaluated_at).total_seconds()
            if seconds_to_resolution <= 0:
                continue
            fair_up = digital_up_probability(
                spot=spot.spot, threshold=record.price_to_beat, annual_volatility=volatility,
                time_to_resolution_seconds=seconds_to_resolution,
            )
            observations.append(BufferSweepObservation(
                market_id=record.market_id, symbol=record.symbol, checkpoint_date=record.market_day,
                checkpoint_name=name, evaluated_at=evaluated_at, fair_up_probability=fair_up,
                up_ask=up_point.price, down_ask=down_point.price,
                up_taker_fee=estimate_taker_fee_usdc(shares=1, price=up_point.price, fee_rate=fee_rate),
                down_taker_fee=estimate_taker_fee_usdc(shares=1, price=down_point.price, fee_rate=fee_rate),
                winning_outcome=record.winning_outcome,
            ))
    buffer_values = tuple(buffers)
    sweep = run_buffer_sweep(observations, buffers=buffer_values, minimum_edge=minimum_edge)
    walk_forward = walk_forward_buffer_sweep(
        observations, buffers=buffer_values, minimum_edge=minimum_edge, training_days=training_days,
        validation_days=validation_days, minimum_training_trades=minimum_training_trades,
    )
    return PythClobBacktestReport(
        source="PYTH_PRO_HISTORY + POLYMARKET_CLOB_PRICE_HISTORY + POLYMARKET_GAMMA_SETTLEMENT",
        execution_price_assumption="CLOB_HISTORY_PRICE_PROXY_NOT_HISTORICAL_EXECUTABLE_ASK",
        fee_rate_assumption=fee_rate, discovered_market_days=len(records), eligible_market_days=eligible_market_days,
        skipped_market_days=len(records) - eligible_market_days, observation_count=len(observations),
        checkpoints=tuple(name for name, _ in checkpoints), sweep=sweep, walk_forward=walk_forward,
    )


@dataclass(frozen=True)
class _Record:
    market_id: str
    symbol: str
    market_day: str
    price_to_beat: float
    final_price: float
    resolves_at: datetime
    winning_outcome: str
    up_clob_path: Path
    down_clob_path: Path
    spot_path: Path


def _load_records(data_dir: Path) -> tuple[_Record, ...]:
    records = []
    for reference_path in sorted(data_dir.glob("*_pyth_references.json")):
        payload = json.loads(reference_path.read_text(encoding="utf-8"))
        stem = reference_path.name.removesuffix("_pyth_references.json")
        settlement_path = data_dir / f"{stem}_settlement.json"
        up_path = data_dir / f"{stem}_up_clob.csv"
        down_path = data_dir / f"{stem}_down_clob.csv"
        spot_path = data_dir / f"{stem}_pyth_intraday.csv"
        if not all(path.exists() for path in (settlement_path, up_path, down_path, spot_path)):
            continue
        settlement = json.loads(settlement_path.read_text(encoding="utf-8"))
        winner = str(settlement.get("winning_outcome", "")).upper()
        if winner not in {"UP", "DOWN"}:
            continue
        try:
            records.append(_Record(
                market_id=str(payload["market_id"]), symbol=str(payload["symbol"]).upper(), market_day=str(payload["market_day"]),
                price_to_beat=float(payload["price_to_beat"]["price"]), final_price=float(payload["final_price"]["price"]),
                resolves_at=datetime.fromisoformat(str(payload["final_price"]["requested_at"]).replace("Z", "+00:00")).astimezone(UTC),
                winning_outcome=winner, up_clob_path=up_path, down_clob_path=down_path, spot_path=spot_path,
            ))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid historical record {reference_path.name}") from error
    return tuple(sorted(records, key=lambda record: (record.market_day, record.market_id)))


def _checkpoint_times(names: tuple[str, ...]) -> tuple[tuple[str, time], ...]:
    supported = {"1200_EDT": time(12), "1400_EDT": time(14), "1530_EDT": time(15, 30)}
    if not names or any(name not in supported for name in names):
        raise ValueError("checkpoints must be drawn from 1200_EDT, 1400_EDT, 1530_EDT")
    return tuple((name, supported[name]) for name in names)


def _read_price_history(path: Path) -> tuple[PriceHistoryPoint, ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple(PriceHistoryPoint(datetime.fromisoformat(row["DateTime"]), float(row["Price"])) for row in csv.DictReader(handle))


def _read_spots(path: Path) -> tuple[UnderlyingSpotPoint, ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple(UnderlyingSpotPoint(datetime.fromisoformat(row["DateTime"]), float(row["Spot"])) for row in csv.DictReader(handle))


def _latest_at_or_before(items, target: datetime):
    selected = None
    for item in items:
        if item.observed_at <= target:
            selected = item
        else:
            break
    return selected
