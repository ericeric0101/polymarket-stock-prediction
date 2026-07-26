"""Offline historical replay and close-risk analysis for daily Up/Down markets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import csv
from statistics import mean
from typing import Iterable, Mapping

from .baseline import DailyClose, annualized_realized_volatility
from .clob_history import PriceHistoryPoint
from .market_discovery import MarketCandidate, MarketSettlement
from .pricing import digital_up_probability


@dataclass(frozen=True)
class UnderlyingSpotPoint:
    observed_at: datetime
    spot: float


def load_intraday_spots_csv(path: Path) -> tuple[UnderlyingSpotPoint, ...]:
    """Load a DateTime,Spot CSV from a verified historical intraday source."""

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "DateTime" not in reader.fieldnames or "Spot" not in reader.fieldnames:
            raise ValueError("spot CSV must contain DateTime and Spot columns")
        points = []
        for row in reader:
            if not row.get("DateTime") or not row.get("Spot"):
                continue
            observed_at = datetime.fromisoformat(str(row["DateTime"]).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                raise ValueError("spot CSV DateTime values must be timezone-aware")
            spot = float(row["Spot"])
            if spot <= 0:
                raise ValueError("spot CSV Spot values must be positive")
            points.append(UnderlyingSpotPoint(observed_at, spot))
    if not points:
        raise ValueError("spot CSV has no usable rows")
    return tuple(sorted(points, key=lambda item: item.observed_at))


@dataclass(frozen=True)
class HistoricalPriceGap:
    market_id: str
    symbol: str
    price_to_beat: float
    final_price: float
    gap: float
    gap_bps: float
    winning_outcome: str

    def as_payload(self) -> Mapping[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalReplayTrade:
    market_id: str
    symbol: str
    evaluated_at: datetime
    outcome: str
    fair_probability: float
    market_price: float
    edge_before_costs: float
    won: bool
    realized_pnl_before_fees: float
    minutes_to_resolution: float

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        return payload


@dataclass(frozen=True)
class CloseRiskWindow:
    minutes_before_resolution: int
    observed_at: datetime | None
    up_price: float | None
    absolute_move_from_previous_window: float | None
    distance_to_price_to_beat_bps: float | None

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        return payload


@dataclass(frozen=True)
class HistoricalReplayReport:
    market_id: str
    symbol: str
    observation_count: int
    selected_trades: int
    wins: int
    total_realized_pnl_before_fees: float
    average_edge_before_costs: float | None
    price_gap: HistoricalPriceGap
    close_risk_windows: tuple[CloseRiskWindow, ...]
    trades: tuple[HistoricalReplayTrade, ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "market_id": self.market_id,
            "symbol": self.symbol,
            "observation_count": self.observation_count,
            "selected_trades": self.selected_trades,
            "wins": self.wins,
            "win_rate": self.wins / self.selected_trades if self.selected_trades else None,
            "total_realized_pnl_before_fees": self.total_realized_pnl_before_fees,
            "average_edge_before_costs": self.average_edge_before_costs,
            "price_gap": self.price_gap.as_payload(),
            "close_risk_windows": [window.as_payload() for window in self.close_risk_windows],
            "trades": [trade.as_payload() for trade in self.trades],
        }


def price_gap_from_daily_closes(
    *, market_id: str, symbol: str, prior_close: DailyClose, final_close: DailyClose,
) -> HistoricalPriceGap:
    price_to_beat = prior_close.close
    final_price = final_close.close
    winning_outcome = "UP" if final_price > price_to_beat else "DOWN" if final_price < price_to_beat else "TIE"
    gap = final_price - price_to_beat
    return HistoricalPriceGap(
        market_id=market_id,
        symbol=symbol.upper(),
        price_to_beat=price_to_beat,
        final_price=final_price,
        gap=gap,
        gap_bps=(gap / price_to_beat) * 10_000,
        winning_outcome=winning_outcome,
    )


def replay_daily_up_down_market(
    *,
    candidate: MarketCandidate,
    symbol: str,
    resolves_at: datetime,
    closes_before_market: list[DailyClose],
    final_close: DailyClose,
    up_history: Iterable[PriceHistoryPoint],
    down_history: Iterable[PriceHistoryPoint],
    settlement: MarketSettlement,
    spot_history: Iterable[UnderlyingSpotPoint] = (),
    minimum_edge: float = 0.02,
    model_error_buffer: float = 0.07,
    lookback_days: int = 20,
    close_risk_windows_minutes: tuple[int, ...] = (60, 30, 15, 5, 1),
) -> HistoricalReplayReport:
    if resolves_at.tzinfo is None:
        raise ValueError("resolves_at must be timezone-aware")
    if minimum_edge < 0 or model_error_buffer < 0:
        raise ValueError("edge and buffer inputs must be non-negative")
    if len(closes_before_market) < lookback_days + 1:
        raise ValueError("insufficient closes_before_market for requested lookback")
    if settlement.winning_outcome is None:
        raise ValueError("settlement must include a winning outcome")

    prior_close = closes_before_market[-1]
    gap = price_gap_from_daily_closes(
        market_id=candidate.market_id, symbol=symbol, prior_close=prior_close, final_close=final_close
    )
    realized_volatility = annualized_realized_volatility(closes_before_market, lookback_days)
    up_points = tuple(sorted(up_history, key=lambda item: item.observed_at))
    down_points = tuple(sorted(down_history, key=lambda item: item.observed_at))
    spot_points = tuple(sorted(spot_history, key=lambda item: item.observed_at))
    down_by_time = {point.observed_at: point for point in down_points}
    trades = []
    for up_point in up_points:
        if up_point.observed_at >= resolves_at:
            continue
        down_point = down_by_time.get(up_point.observed_at)
        if down_point is None:
            continue
        spot_point = _latest_spot_at_or_before(spot_points, up_point.observed_at)
        if spot_point is None:
            continue
        seconds_to_resolution = (resolves_at - up_point.observed_at).total_seconds()
        fair_up = digital_up_probability(
            spot=spot_point.spot,
            threshold=prior_close.close,
            annual_volatility=realized_volatility,
            time_to_resolution_seconds=seconds_to_resolution,
        )
        candidates = (
            ("UP", fair_up, up_point.price),
            ("DOWN", 1 - fair_up, down_point.price),
        )
        eligible = [
            (outcome, probability, price, probability - model_error_buffer - price)
            for outcome, probability, price in candidates
            if probability - model_error_buffer - price >= minimum_edge
        ]
        if not eligible:
            continue
        outcome, probability, price, edge = max(eligible, key=lambda item: item[3])
        won = settlement.winning_outcome.upper() == outcome
        trades.append(HistoricalReplayTrade(
            market_id=candidate.market_id,
            symbol=symbol.upper(),
            evaluated_at=up_point.observed_at,
            outcome=outcome,
            fair_probability=probability,
            market_price=price,
            edge_before_costs=edge,
            won=won,
            realized_pnl_before_fees=(1.0 if won else 0.0) - price,
            minutes_to_resolution=seconds_to_resolution / 60,
        ))

    close_windows = close_risk_profile(
        resolves_at=resolves_at,
        up_history=up_points,
        price_to_beat=gap.price_to_beat,
        final_price=gap.final_price,
        windows_minutes=close_risk_windows_minutes,
    )
    return HistoricalReplayReport(
        market_id=candidate.market_id,
        symbol=symbol.upper(),
        observation_count=min(len(up_points), len(down_points)),
        selected_trades=len(trades),
        wins=sum(trade.won for trade in trades),
        total_realized_pnl_before_fees=sum(trade.realized_pnl_before_fees for trade in trades),
        average_edge_before_costs=mean(trade.edge_before_costs for trade in trades) if trades else None,
        price_gap=gap,
        close_risk_windows=close_windows,
        trades=tuple(trades),
    )


def close_risk_profile(
    *,
    resolves_at: datetime,
    up_history: Iterable[PriceHistoryPoint],
    price_to_beat: float,
    final_price: float,
    windows_minutes: tuple[int, ...] = (60, 30, 15, 5, 1),
) -> tuple[CloseRiskWindow, ...]:
    points = tuple(sorted(up_history, key=lambda item: item.observed_at))
    previous_price: float | None = None
    windows = []
    for minutes in windows_minutes:
        target = resolves_at - timedelta(minutes=minutes)
        point = _latest_at_or_before(points, target)
        up_price = point.price if point else None
        move = abs(up_price - previous_price) if up_price is not None and previous_price is not None else None
        previous_price = up_price if up_price is not None else previous_price
        windows.append(CloseRiskWindow(
            minutes_before_resolution=minutes,
            observed_at=point.observed_at if point else None,
            up_price=up_price,
            absolute_move_from_previous_window=move,
            distance_to_price_to_beat_bps=((final_price - price_to_beat) / price_to_beat) * 10_000,
        ))
    return tuple(windows)


def _latest_at_or_before(points: tuple[PriceHistoryPoint, ...], target: datetime) -> PriceHistoryPoint | None:
    selected = None
    for point in points:
        if point.observed_at <= target:
            selected = point
        else:
            break
    return selected


def _latest_spot_at_or_before(points: tuple[UnderlyingSpotPoint, ...], target: datetime) -> UnderlyingSpotPoint | None:
    selected = None
    for point in points:
        if point.observed_at <= target:
            selected = point
        else:
            break
    return selected
