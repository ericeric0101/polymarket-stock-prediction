"""Isolated replay for Pyth-resolved closes-above (Above-X) markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
import csv
import json
from math import log
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .baseline import DailyClose, annualized_realized_volatility
from .pricing import digital_up_probability

NEW_YORK = ZoneInfo("America/New_York")
CHECKPOINTS = {"1200_EDT": time(12), "1400_EDT": time(14), "1530_EDT": time(15, 30)}


@dataclass(frozen=True)
class AboveXBacktestReport:
    source: str
    execution_price_assumption: str
    contract_count: int
    observation_count: int
    trade_count: int
    wins: int
    total_pnl: float
    brier_score: float | None
    log_loss: float | None
    skipped_contracts: int
    checkpoints: tuple[str, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "market_type": "ABOVE_X",
            "source": self.source,
            "execution_price_assumption": self.execution_price_assumption,
            "contract_count": self.contract_count,
            "observation_count": self.observation_count,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "win_rate": self.wins / self.trade_count if self.trade_count else None,
            "total_pnl": round(self.total_pnl, 8),
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "skipped_contracts": self.skipped_contracts,
            "checkpoints": list(self.checkpoints),
        }


@dataclass(frozen=True)
class _Contract:
    market_id: str
    symbol: str
    market_date: str
    strike: float
    resolves_at: datetime


def run_above_x_backtest(
    *,
    discovery_path: Path,
    data_dir: Path,
    spot_data_dir: Path,
    minimum_edge: float = 0.02,
    lookback_days: int = 20,
    checkpoints: tuple[str, ...] = ("1200_EDT", "1400_EDT", "1530_EDT"),
) -> AboveXBacktestReport:
    if minimum_edge < 0 or lookback_days < 2 or not checkpoints:
        raise ValueError("invalid Above-X backtest parameters")
    if any(item not in CHECKPOINTS for item in checkpoints):
        raise ValueError("unsupported Above-X checkpoint")
    raw = json.loads(discovery_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Above-X discovery JSON must be a list")
    contracts = tuple(_contract(item) for item in raw if isinstance(item, Mapping))
    closes = _daily_closes(spot_data_dir)
    observations = 0
    trades = wins = 0
    total_pnl = 0.0
    probabilities: list[tuple[float, int]] = []
    skipped = 0
    for contract in contracts:
        stem = f"{contract.market_id}_{contract.symbol}_{contract.market_date}_above_x"
        yes_path, no_path, settlement_path = (
            data_dir / f"{stem}_yes_clob.csv",
            data_dir / f"{stem}_no_clob.csv",
            data_dir / f"{stem}_settlement.json",
        )
        spot_path = next(
            iter(sorted(spot_data_dir.glob(f"*_{contract.symbol}_{contract.market_date}_pyth_intraday.csv"))), None
        )
        if not all(path.is_file() for path in (yes_path, no_path, settlement_path)) or spot_path is None:
            skipped += 1
            continue
        winner = str(json.loads(settlement_path.read_text(encoding="utf-8")).get("winning_outcome", "")).upper()
        if winner not in {"YES", "NO"}:
            skipped += 1
            continue
        history = [item for item in closes.get(contract.symbol, ()) if item.date < contract.market_date]
        if len(history) < lookback_days + 1:
            skipped += 1
            continue
        volatility = annualized_realized_volatility(history, lookback_days)
        yes = _read_prices(yes_path)
        no = _read_prices(no_path)
        spots = _read_spots(spot_path)
        for name in checkpoints:
            evaluated_at = datetime.combine(
                datetime.fromisoformat(contract.market_date).date(), CHECKPOINTS[name], tzinfo=NEW_YORK
            ).astimezone(UTC)
            yes_price = _latest(yes, evaluated_at)
            no_price = _latest(no, evaluated_at)
            spot = _latest(spots, evaluated_at)
            if yes_price is None or no_price is None or spot is None or contract.resolves_at <= evaluated_at:
                continue
            fair = digital_up_probability(
                spot=spot,
                threshold=contract.strike,
                annual_volatility=volatility,
                time_to_resolution_seconds=(contract.resolves_at - evaluated_at).total_seconds(),
            )
            market_yes = (yes_price + (1.0 - no_price)) / 2.0
            market_yes = min(max(market_yes, 0.000001), 0.999999)
            outcome = 1 if winner == "YES" else 0
            probabilities.append((fair, outcome))
            observations += 1
            if fair - market_yes >= minimum_edge:
                price, selected = yes_price, "YES"
            elif market_yes - fair >= minimum_edge:
                price, selected = no_price, "NO"
            else:
                continue
            trades += 1
            won = selected == winner
            wins += won
            total_pnl += (1.0 - price) if won else -price
    brier = (
        sum((prob - outcome) ** 2 for prob, outcome in probabilities) / len(probabilities) if probabilities else None
    )
    loss = (
        sum(
            -(outcome * log(min(max(prob, 1e-9), 1 - 1e-9)) + (1 - outcome) * log(1 - min(max(prob, 1e-9), 1 - 1e-9)))
            for prob, outcome in probabilities
        )
        / len(probabilities)
        if probabilities
        else None
    )
    return AboveXBacktestReport(
        source="PYTH_PRO_INTRADAY + POLYMARKET_CLOB_PRICE_HISTORY + POLYMARKET_GAMMA_SETTLEMENT",
        execution_price_assumption="CLOB_HISTORY_PRICE_PROXY_NOT_HISTORICAL_EXECUTABLE_ASK",
        contract_count=len(contracts),
        observation_count=observations,
        trade_count=trades,
        wins=wins,
        total_pnl=total_pnl,
        brier_score=brier,
        log_loss=loss,
        skipped_contracts=skipped,
        checkpoints=checkpoints,
    )


def _contract(item: Mapping[str, object]) -> _Contract:
    return _Contract(
        str(item["market_id"]),
        str(item["symbol"]).upper(),
        str(item["market_date"]),
        float(item["strike"]),
        datetime.fromisoformat(str(item["resolves_at"]).replace("Z", "+00:00")).astimezone(UTC),
    )


def _daily_closes(data_dir: Path) -> dict[str, tuple[DailyClose, ...]]:
    result: dict[str, list[DailyClose]] = {}
    for path in data_dir.glob("*_pyth_references.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            symbol = str(payload["symbol"]).upper()
            result.setdefault(symbol, []).append(
                DailyClose(str(payload["market_day"]), float(payload["final_price"]["price"]))
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return {key: tuple(sorted(value, key=lambda item: item.date)) for key, value in result.items()}


def _read_prices(path: Path) -> tuple[tuple[datetime, float], ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple((datetime.fromisoformat(row["DateTime"]), float(row["Price"])) for row in csv.DictReader(handle))


def _read_spots(path: Path) -> tuple[tuple[datetime, float], ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple((datetime.fromisoformat(row["DateTime"]), float(row["Spot"])) for row in csv.DictReader(handle))


def _latest(items, target: datetime):
    selected = None
    for observed_at, value in items:
        if observed_at <= target:
            selected = value
        else:
            break
    return selected
