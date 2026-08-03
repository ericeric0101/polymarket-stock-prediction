"""Non-leaking Above-X confirmation/veto research for Core Up/Down entries."""

from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
import csv, json
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo
from .baseline import DailyClose, annualized_realized_volatility
from .price_ladder import LadderProbabilityPoint, diagnose_cross_market, fit_monotonic_curve, probability_point
from .pyth_clob_backtest import _load_records, _read_price_history, _read_spots, _latest_at_or_before
from .storage.sqlite import DatabaseOperationalError, database_connection
from .price_ladder_journal import PriceLadderJournal
from .pricing import digital_up_probability

NY = ZoneInfo("America/New_York")
CHECKPOINTS = {"1200_EDT": time(12), "1400_EDT": time(14), "1530_EDT": time(15, 30)}


@dataclass(frozen=True)
class AboveXVetoPolicy:
    mode: str
    minimum_strikes: int
    maximum_width: float = 0.30

    def __post_init__(self):
        if self.mode not in {"BASELINE", "VETO_DISAGREEMENT", "REQUIRE_CONFIRMATION"}:
            raise ValueError("invalid veto mode")
        if self.minimum_strikes < 1 or not 0 < self.maximum_width <= 1:
            raise ValueError("invalid veto policy")

    @property
    def policy_id(self):
        return f"{self.mode}:strikes={self.minimum_strikes}:width={self.maximum_width:.2f}"


@dataclass(frozen=True)
class VetoObservation:
    market_id: str
    symbol: str
    market_date: str
    checkpoint_name: str
    evaluated_at: datetime
    core_outcome: str | None
    core_edge: float | None
    winning_outcome: str
    price_to_beat: float
    fair_up_probability: float
    ladder_probability: float | None
    strikes: int
    reliable: bool
    ladder_outcome: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PolicyResult:
    policy_id: str
    trades: int
    wins: int
    pnl: float
    vetoed: int

    def as_payload(self):
        return {**asdict(self), "win_rate": self.wins / self.trades if self.trades else None}


@dataclass(frozen=True)
class WalkForwardReport:
    status: str
    distinct_dates: int
    checkpoint_name: str
    windows: tuple[Mapping[str, object], ...]

    def as_payload(self):
        return {
            "status": self.status,
            "distinct_dates": self.distinct_dates,
            "checkpoint_name": self.checkpoint_name,
            "windows": [dict(x) for x in self.windows],
            "historical_execution_assumption": "CLOB_HISTORY_PRICE_PROXY_NOT_EXECUTABLE_ASK",
            "historical_width_available": False,
        }


def default_policies():
    return tuple(
        [AboveXVetoPolicy("BASELINE", 3)]
        + [AboveXVetoPolicy(mode, n) for n in (3, 4, 5) for mode in ("VETO_DISAGREEMENT", "REQUIRE_CONFIRMATION")]
    )


def historical_observations(
    *,
    core_dir: Path,
    above_x_dir: Path,
    discovery_path: Path,
    checkpoint_name="1200_EDT",
    buffer=0.02,
    minimum_edge=0.02,
    lookback_days=20,
):
    if checkpoint_name not in CHECKPOINTS:
        raise ValueError("unsupported checkpoint")
    records = _load_records(core_dir)
    closes: dict[str, list[DailyClose]] = {}
    for r in records:
        closes.setdefault(r.symbol, []).append(DailyClose(r.market_day, r.final_price))
    for value in closes.values():
        value.sort(key=lambda x: x.date)
    contracts = json.loads(discovery_path.read_text())
    groups: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for c in contracts:
        if isinstance(c, dict):
            groups.setdefault((str(c["symbol"]).upper(), str(c["market_date"])), []).append(c)
    result = []
    for r in records:
        contracts_for_day = groups.get((r.symbol, r.market_day), [])
        if not contracts_for_day:
            continue
        history = [x for x in closes[r.symbol] if x.date < r.market_day]
        if len(history) < lookback_days + 1:
            continue
        at = datetime.combine(
            datetime.fromisoformat(r.market_day).date(), CHECKPOINTS[checkpoint_name], tzinfo=NY
        ).astimezone(UTC)
        up = _latest_at_or_before(_read_price_history(r.up_clob_path), at)
        down = _latest_at_or_before(_read_price_history(r.down_clob_path), at)
        spot = _latest_at_or_before(_read_spots(r.spot_path), at)
        if not up or not down or not spot:
            continue
        fair = digital_up_probability(
            spot.spot,
            r.price_to_beat,
            annualized_realized_volatility(history, lookback_days),
            (r.resolves_at - at).total_seconds(),
        )
        candidates = []
        for side, p, quote in (("UP", fair, up.price), ("DOWN", 1 - fair, down.price)):
            edge = max(0, p - buffer) - quote
            if edge >= minimum_edge:
                candidates.append((edge, side))
        core_edge, core_outcome = max(candidates) if candidates else (None, None)
        points = []
        for c in contracts_for_day:
            stem = f"{c['market_id']}_{r.symbol}_{r.market_day}_above_x"
            yes = _latest_price(above_x_dir / f"{stem}_yes_clob.csv", at)
            no = _latest_price(above_x_dir / f"{stem}_no_clob.csv", at)
            if yes is not None and no is not None:
                p = (yes + 1 - no) / 2
                points.append(LadderProbabilityPoint(float(c["strike"]), p, p, p, 0.0, 1.0, str(c["market_id"])))
        curve = fit_monotonic_curve(points)
        lp = curve.interpolate(r.price_to_beat)
        ladder = "UP" if lp is not None and lp >= 0.5 else "DOWN" if lp is not None else None
        reliable = lp is not None
        result.append(
            VetoObservation(
                r.market_id,
                r.symbol,
                r.market_day,
                checkpoint_name,
                at,
                core_outcome,
                core_edge,
                r.winning_outcome,
                r.price_to_beat,
                fair,
                lp,
                len(points),
                reliable,
                ladder,
                () if reliable else ("PRICE_TO_BEAT_NOT_BRACKETED",),
            )
        )
    return tuple(result)


def apply_policy(items: Iterable[VetoObservation], policy: AboveXVetoPolicy):
    trades = wins = vetoed = 0
    pnl = 0.0
    for x in items:
        if not x.core_outcome:
            continue
        veto = False
        if policy.mode == "VETO_DISAGREEMENT":
            veto = x.reliable and x.strikes >= policy.minimum_strikes and x.ladder_outcome != x.core_outcome
        elif policy.mode == "REQUIRE_CONFIRMATION":
            veto = not (x.reliable and x.strikes >= policy.minimum_strikes and x.ladder_outcome == x.core_outcome)
        if veto:
            vetoed += 1
            continue
        trades += 1
        win = x.core_outcome == x.winning_outcome
        wins += win
        # Historical Core price is already reflected only through selected edge; payout proxy avoids a second fitted price.
        pnl += 1.0 if win else -1.0
    return PolicyResult(policy.policy_id, trades, wins, pnl, vetoed)


def walk_forward(
    *,
    observations: Iterable[VetoObservation],
    training_days=6,
    validation_days=2,
    minimum_training_trades=3,
    policies: Iterable[AboveXVetoPolicy] | None = None,
):
    items = tuple(observations)
    dates = sorted({x.market_date for x in items})
    policies = tuple(policies or default_policies())
    if len(dates) < training_days + validation_days:
        return WalkForwardReport(
            "INSUFFICIENT_DISTINCT_DAYS", len(dates), items[0].checkpoint_name if items else "1200_EDT", ()
        )
    windows = []
    for i in range(0, len(dates) - training_days - validation_days + 1, validation_days):
        train = set(dates[i : i + training_days])
        valid = set(dates[i + training_days : i + training_days + validation_days])
        candidates = [apply_policy((x for x in items if x.market_date in train), p) for p in policies]
        eligible = [(p, r) for p, r in zip(policies, candidates) if r.trades >= minimum_training_trades]
        selected = max(eligible, key=lambda pair: (pair[1].pnl, pair[1].wins, -pair[1].vetoed))[0] if eligible else None
        windows.append(
            {
                "training_dates": sorted(train),
                "validation_dates": sorted(valid),
                "selected_policy": selected.policy_id if selected else None,
                "training_result": apply_policy((x for x in items if x.market_date in train), selected).as_payload()
                if selected
                else None,
                "validation_result": apply_policy((x for x in items if x.market_date in valid), selected).as_payload()
                if selected
                else None,
                "baseline_validation": apply_policy(
                    (x for x in items if x.market_date in valid), AboveXVetoPolicy("BASELINE", 3)
                ).as_payload(),
            }
        )
    return WalkForwardReport("READY", len(dates), items[0].checkpoint_name if items else "1200_EDT", tuple(windows))


def live_diagnostic(*, core_payload: Mapping[str, object], snapshots: Iterable[object], policy: AboveXVetoPolicy):
    core = str(core_payload.get("paper_outcome") or "").upper()
    if core not in {"UP", "DOWN"}:
        return {"shadow_action": "NO_CORE_ENTRY", "policy_id": policy.policy_id, "reasons": ["NO_CORE_ENTRY"]}
    beat = core_payload.get("price_to_beat")
    fair = core_payload.get("fair_up_probability")
    if beat is None or fair is None:
        return {"shadow_action": "UNRELIABLE", "policy_id": policy.policy_id, "reasons": ["MISSING_CORE_FIELDS"]}
    points = []
    for s in snapshots:
        point = probability_point(
            strike=s.strike,
            market_id=s.market_id,
            yes_bid=s.yes_bid,
            yes_ask=s.yes_ask,
            no_bid=s.no_bid,
            no_ask=s.no_ask,
            yes_depth=s.yes_bid_depth + s.yes_ask_depth,
            no_depth=s.no_bid_depth + s.no_ask_depth,
        )
        if point:
            points.append(point)
    diagnostic = diagnose_cross_market(
        symbol=str(core_payload.get("symbol", "")),
        market_date=str(core_payload.get("checkpoint_date", "")),
        checkpoint_name=str(core_payload.get("checkpoint_name", "")),
        price_to_beat=float(beat),
        model_up_probability=float(fair),
        up_down_market_probability=None,
        points=points,
        minimum_strikes=policy.minimum_strikes,
        maximum_bracket_width=policy.maximum_width,
    )
    ladder = (
        "UP"
        if diagnostic.ladder_up_probability is not None and diagnostic.ladder_up_probability >= 0.5
        else "DOWN"
        if diagnostic.ladder_up_probability is not None
        else None
    )
    reliable = diagnostic.status != "UNRELIABLE" and ladder is not None
    veto = (policy.mode == "VETO_DISAGREEMENT" and reliable and ladder != core) or (
        policy.mode == "REQUIRE_CONFIRMATION" and not (reliable and ladder == core)
    )
    return {
        "policy_id": policy.policy_id,
        "core_outcome": core,
        "ladder_outcome": ladder,
        "ladder_probability": diagnostic.ladder_up_probability,
        "lower_bound": diagnostic.ladder_lower_bound,
        "upper_bound": diagnostic.ladder_upper_bound,
        "strikes": diagnostic.strikes,
        "diagnostic_status": diagnostic.status,
        "reasons": list(diagnostic.reasons),
        "shadow_action": "VETO" if veto else "KEEP_CORE" if reliable else "UNRELIABLE",
    }


def sync_live_veto_shadow(
    *, journal_path: Path, policy: AboveXVetoPolicy = AboveXVetoPolicy("VETO_DISAGREEMENT", 3)
) -> tuple[Mapping[str, object], ...]:
    """Join immutable Core checkpoints to independently captured ladder books.

    This writes only the Above-X sidecar table; it never changes Core evaluation
    payloads, portfolio decisions, or paper positions.
    """
    ladder = PriceLadderJournal(journal_path)
    ladder.initialize()
    snapshots = ladder.list_snapshots(checkpoint_only=True)
    grouped: dict[tuple[str, str, str], list[object]] = {}
    for snapshot in snapshots:
        if snapshot.checkpoint_name:
            grouped.setdefault((snapshot.market_date, snapshot.checkpoint_name, snapshot.symbol), []).append(snapshot)
    try:
        with database_connection(journal_path) as connection:
            rows = connection.execute(
                "SELECT market_id, symbol, checkpoint_date, checkpoint_name, payload_json FROM checkpoint_observations"
            ).fetchall()
    except DatabaseOperationalError:
        # The independent collector may run before the Core journal is initialized.
        return ()
    written = []
    for row in rows:
        market_id = str(row["market_id"])
        symbol = str(row["symbol"])
        day = str(row["checkpoint_date"])
        checkpoint = str(row["checkpoint_name"])
        payload = json.loads(str(row["payload_json"]))
        key = (day, checkpoint, symbol.upper())
        books = grouped.get(key, ())
        if not books:
            continue
        payload = {
            **payload,
            "market_id": str(market_id),
            "symbol": str(symbol).upper(),
            "checkpoint_date": str(day),
            "checkpoint_name": str(checkpoint),
            "observed_at": datetime.now(UTC).isoformat(),
        }
        diagnostic = live_diagnostic(core_payload=payload, snapshots=books, policy=policy)
        record = {**payload, **diagnostic}
        if ladder.record_veto_observation(record):
            written.append(record)
    return tuple(written)


def _latest_price(path: Path, target: datetime):
    if not path.is_file():
        return None
    selected = None
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if datetime.fromisoformat(row["DateTime"]) <= target:
                selected = float(row["Price"])
            else:
                break
    return selected
