"""Append-only SQLite journal for reproducible shadow decisions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Mapping
import uuid
from zoneinfo import ZoneInfo

from .fees import estimate_taker_fee_usdc
from .checkpoints import DEFAULT_MAXIMUM_DELAY_SECONDS, checkpoint_target_at


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    fair_yes_probability REAL NOT NULL CHECK (fair_yes_probability >= 0 AND fair_yes_probability <= 1),
    executable_ask REAL NOT NULL CHECK (executable_ask >= 0 AND executable_ask <= 1),
    edge REAL NOT NULL,
    should_record_paper_trade INTEGER NOT NULL CHECK (should_record_paper_trade IN (0, 1)),
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_decisions_market_created
    ON shadow_decisions (market_id, created_at);
CREATE TABLE IF NOT EXISTS market_candidates (
    market_id TEXT PRIMARY KEY,
    discovered_at TEXT NOT NULL,
    question TEXT NOT NULL,
    slug TEXT NOT NULL,
    end_date TEXT NOT NULL,
    resolution_source TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    outcome_a_label TEXT NOT NULL DEFAULT '',
    outcome_b_label TEXT NOT NULL DEFAULT '',
    outcome_a_token_id TEXT NOT NULL DEFAULT '',
    outcome_b_token_id TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    midpoint REAL,
    raw_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_market_observed
    ON order_book_snapshots (market_id, observed_at);
CREATE TABLE IF NOT EXISTS execution_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    signal_id TEXT,
    observation_kind TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    token_id TEXT NOT NULL,
    spot REAL,
    price_to_beat REAL,
    fair_probability REAL,
    best_bid REAL,
    best_ask REAL,
    fee_rate REAL,
    book_payload_json TEXT NOT NULL,
    evaluation_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_observations_signal_observed
    ON execution_observations (signal_id, observed_at);
CREATE TABLE IF NOT EXISTS alpaca_indicative_option_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    bid_price REAL NOT NULL,
    ask_price REAL NOT NULL,
    feed TEXT NOT NULL,
    quality_label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alpaca_option_quotes_symbol_observed
    ON alpaca_indicative_option_quotes (option_symbol, observed_at);
CREATE TABLE IF NOT EXISTS realtime_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    spot REAL,
    up_ask REAL,
    down_ask REAL,
    fair_up_probability REAL,
    signal_status TEXT NOT NULL,
    skip_reasons_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realtime_evaluations_market_evaluated
    ON realtime_evaluations (market_id, evaluated_at);
CREATE TABLE IF NOT EXISTS spot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    observed_second TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    published_at TEXT,
    confidence REAL,
    feed_id TEXT,
    UNIQUE (source, symbol, observed_second)
);
CREATE INDEX IF NOT EXISTS idx_spot_observations_symbol_observed
    ON spot_observations (symbol, observed_at);
CREATE TABLE IF NOT EXISTS spot_source_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    observed_second TEXT NOT NULL,
    symbol TEXT NOT NULL,
    primary_source TEXT NOT NULL,
    primary_price REAL NOT NULL CHECK (primary_price > 0),
    primary_published_at TEXT,
    pyth_price REAL NOT NULL CHECK (pyth_price > 0),
    pyth_published_at TEXT,
    pyth_confidence REAL,
    pyth_feed_id TEXT,
    difference_bps REAL NOT NULL,
    UNIQUE (symbol, primary_source, observed_second)
);
CREATE INDEX IF NOT EXISTS idx_spot_source_comparisons_symbol_observed
    ON spot_source_comparisons (symbol, observed_at);
CREATE TABLE IF NOT EXISTS checkpoint_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    checkpoint_date TEXT NOT NULL,
    checkpoint_name TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    fair_up_probability REAL NOT NULL CHECK (fair_up_probability >= 0 AND fair_up_probability <= 1),
    up_ask REAL,
    down_ask REAL,
    model_version TEXT NOT NULL,
    option_iv REAL,
    payload_json TEXT NOT NULL,
    checkpoint_target_at TEXT,
    checkpoint_delay_seconds REAL,
    eligible_for_calibration INTEGER NOT NULL DEFAULT 1 CHECK (eligible_for_calibration IN (0, 1)),
    UNIQUE (market_id, checkpoint_date, checkpoint_name)
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_observations_market_checkpoint
    ON checkpoint_observations (market_id, checkpoint_date, checkpoint_name);
CREATE TABLE IF NOT EXISTS portfolio_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    risk_group TEXT NOT NULL,
    edge REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SELECTED', 'REJECTED')),
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_decisions_batch_created
    ON portfolio_decisions (batch_id, created_at);
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'SETTLED')),
    contracts REAL NOT NULL CHECK (contracts > 0),
    entry_ask REAL NOT NULL CHECK (entry_ask >= 0 AND entry_ask <= 1),
    entry_fee REAL NOT NULL CHECK (entry_fee >= 0),
    entry_slippage REAL NOT NULL CHECK (entry_slippage >= 0),
    fair_probability REAL NOT NULL CHECK (fair_probability >= 0 AND fair_probability <= 1),
    model_version TEXT NOT NULL,
    entry_payload_json TEXT NOT NULL,
    settled_at TEXT,
    settlement_outcome TEXT,
    payout REAL,
    realized_pnl REAL,
    settlement_payload_json TEXT,
    included_in_calibration INTEGER NOT NULL DEFAULT 1 CHECK (included_in_calibration IN (0, 1)),
    exclusion_reason TEXT,
    UNIQUE (market_id, outcome)
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status_opened
    ON paper_positions (status, opened_at);
CREATE TABLE IF NOT EXISTS maker_shadow_quotes (
    quote_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CANCELLED')),
    limit_price REAL NOT NULL CHECK (limit_price > 0 AND limit_price < 1),
    fair_probability REAL NOT NULL CHECK (fair_probability >= 0 AND fair_probability <= 1),
    theoretical_edge REAL NOT NULL,
    best_bid REAL NOT NULL CHECK (best_bid >= 0 AND best_bid <= 1),
    best_ask REAL NOT NULL CHECK (best_ask >= 0 AND best_ask <= 1),
    touch_count INTEGER NOT NULL DEFAULT 0 CHECK (touch_count >= 0),
    last_touched_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_maker_quote
    ON maker_shadow_quotes (market_id, outcome) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_maker_shadow_quotes_status_created
    ON maker_shadow_quotes (status, created_at);
CREATE TABLE IF NOT EXISTS market_contract_reviews (
    market_id TEXT PRIMARY KEY,
    reviewed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
    reason TEXT NOT NULL,
    contract_json TEXT
);
CREATE TABLE IF NOT EXISTS market_settlements (
    market_id TEXT PRIMARY KEY,
    settled_at TEXT NOT NULL,
    winning_outcome TEXT NOT NULL CHECK (winning_outcome IN ('UP', 'DOWN')),
    payload_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredOutcomeToken:
    label: str
    token_id: str


@dataclass(frozen=True)
class StoredMarketCandidate:
    market_id: str
    question: str
    slug: str
    end_date: str
    outcome_a_label: str
    outcome_b_label: str
    review_status: str


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    opened_at: datetime
    market_id: str
    symbol: str
    outcome: str
    status: str
    contracts: float
    entry_ask: float
    entry_fee: float
    entry_slippage: float
    fair_probability: float
    model_version: str
    settled_at: datetime | None
    settlement_outcome: str | None
    payout: float | None
    realized_pnl: float | None
    included_in_calibration: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class MakerShadowQuote:
    quote_id: str
    created_at: datetime
    last_observed_at: datetime
    market_id: str
    symbol: str
    outcome: str
    status: str
    limit_price: float
    fair_probability: float
    theoretical_edge: float
    best_bid: float
    best_ask: float
    touch_count: int
    last_touched_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


@dataclass(frozen=True)
class ReplayObservation:
    market_id: str
    symbol: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    winning_outcome: str


@dataclass(frozen=True)
class FirstSignalCalibrationObservation:
    """One immutable first model-side signal per settled market."""

    market_id: str
    symbol: str
    evaluated_at: datetime
    model_outcome: str
    selected_fair_probability: float
    entry_ask: float
    entry_fee: float | None
    winning_outcome: str
    model_version: str
    option_iv_status: str
    iv_regime: str
    spot_provider: str
    threshold_distance_bps: float | None
    volatility_estimator: str = "CLOSE_TO_CLOSE"


@dataclass(frozen=True)
class CheckpointObservation:
    market_id: str
    symbol: str
    checkpoint_date: str
    checkpoint_name: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    model_version: str
    option_iv: float | None
    winning_outcome: str
    checkpoint_target_at: datetime
    checkpoint_delay_seconds: float
    eligible_for_calibration: bool


@dataclass(frozen=True)
class BufferSweepObservation:
    market_id: str
    symbol: str
    checkpoint_date: str
    checkpoint_name: str
    evaluated_at: datetime
    fair_up_probability: float
    up_ask: float | None
    down_ask: float | None
    up_taker_fee: float | None
    down_taker_fee: float | None
    winning_outcome: str
    spot: float | None = None
    price_to_beat: float | None = None
    up_bid: float | None = None
    down_bid: float | None = None
    annualized_volatility: float | None = None
    cross_source_difference: float | None = None
    comparison_models: tuple[Mapping[str, object], ...] = ()
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionObservation:
    observed_at: datetime
    signal_id: str | None
    observation_kind: str
    market_id: str
    symbol: str
    outcome: str
    token_id: str
    spot: float | None
    price_to_beat: float | None
    fair_probability: float | None
    best_bid: float | None
    best_ask: float | None
    fee_rate: float | None
    book_payload: Mapping[str, object]
    evaluation_payload: Mapping[str, object]


@dataclass(frozen=True)
class SpotSourceComparison:
    observed_at: datetime
    symbol: str
    primary_source: str
    primary_price: float
    pyth_price: float
    pyth_confidence: float | None
    difference_bps: float
    primary_published_at: datetime | None = None
    pyth_published_at: datetime | None = None


@dataclass(frozen=True)
class StoredSpotObservation:
    observed_at: datetime
    source: str
    symbol: str
    price: float
    published_at: datetime | None = None


DATABASE_BUSY_TIMEOUT_MS = 5_000
DATABASE_COMMIT_RETRY_SECONDS = (0.05, 0.15, 0.45)


def _is_database_locked(error: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(error).lower() or "database is busy" in str(error).lower()


@contextmanager
def _database_connection(path: Path):
    """Use WAL plus bounded commit retries for independent dashboard/bot processes."""

    connection = sqlite3.connect(path, timeout=DATABASE_BUSY_TIMEOUT_MS / 1000)
    connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        for attempt, delay_seconds in enumerate((0.0, *DATABASE_COMMIT_RETRY_SECONDS)):
            if delay_seconds:
                time.sleep(delay_seconds)
            try:
                connection.commit()
                break
            except sqlite3.OperationalError as error:
                if not _is_database_locked(error) or attempt == len(DATABASE_COMMIT_RETRY_SECONDS):
                    raise
    finally:
        connection.close()


class ShadowJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _database_connection(self.path) as connection:
            current_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if current_mode != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._migrate_market_candidate_columns(connection)
            self._migrate_paper_position_columns(connection)
            self._migrate_checkpoint_observation_columns(connection)
            self._exclude_precontract_day_paper_positions(connection)

    @staticmethod
    def _migrate_checkpoint_observation_columns(connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(checkpoint_observations)")}
        if "checkpoint_target_at" not in columns:
            connection.execute("ALTER TABLE checkpoint_observations ADD COLUMN checkpoint_target_at TEXT")
        if "checkpoint_delay_seconds" not in columns:
            connection.execute("ALTER TABLE checkpoint_observations ADD COLUMN checkpoint_delay_seconds REAL")
        if "eligible_for_calibration" not in columns:
            connection.execute("ALTER TABLE checkpoint_observations ADD COLUMN eligible_for_calibration INTEGER NOT NULL DEFAULT 1")
        rows = connection.execute(
            """SELECT id, checkpoint_date, checkpoint_name, evaluated_at
            FROM checkpoint_observations WHERE checkpoint_target_at IS NULL OR checkpoint_delay_seconds IS NULL"""
        ).fetchall()
        for row in rows:
            target_at = checkpoint_target_at(str(row[1]), str(row[2]))
            evaluated_at = datetime.fromisoformat(str(row[3]))
            delay_seconds = max(0.0, (evaluated_at - target_at).total_seconds())
            connection.execute(
                """UPDATE checkpoint_observations
                SET checkpoint_target_at = ?, checkpoint_delay_seconds = ?, eligible_for_calibration = ?
                WHERE id = ?""",
                (target_at.isoformat(), delay_seconds, int(delay_seconds <= DEFAULT_MAXIMUM_DELAY_SECONDS), int(row[0])),
            )

    @staticmethod
    def _migrate_market_candidate_columns(connection: sqlite3.Connection) -> None:
        """Keep the Phase 1 journal compatible with the earlier Yes/No-only schema."""

        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(market_candidates)")}
        required_columns = {
            "outcome_a_label": "TEXT NOT NULL DEFAULT ''",
            "outcome_b_label": "TEXT NOT NULL DEFAULT ''",
            "outcome_a_token_id": "TEXT NOT NULL DEFAULT ''",
            "outcome_b_token_id": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in required_columns.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE market_candidates ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_paper_position_columns(connection: sqlite3.Connection) -> None:
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_positions)")}
        required_columns = {
            "included_in_calibration": "INTEGER NOT NULL DEFAULT 1",
            "exclusion_reason": "TEXT",
        }
        for column, definition in required_columns.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE paper_positions ADD COLUMN {column} {definition}")

    @staticmethod
    def _exclude_precontract_day_paper_positions(connection: sqlite3.Connection) -> None:
        """Preserve, but exclude, entries opened before the contract's NY trading date."""

        rows = connection.execute(
            """SELECT position.position_id, position.opened_at, candidate.end_date
            FROM paper_positions AS position
            JOIN market_candidates AS candidate ON candidate.market_id = position.market_id
            WHERE position.included_in_calibration = 1"""
        ).fetchall()
        new_york = ZoneInfo("America/New_York")
        for position_id, opened_at, end_date in rows:
            try:
                opened_day = datetime.fromisoformat(str(opened_at)).astimezone(new_york).date()
                contract_day = datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).astimezone(new_york).date()
            except ValueError:
                continue
            if opened_day < contract_day:
                connection.execute(
                    """UPDATE paper_positions SET included_in_calibration = 0,
                    exclusion_reason = 'PRECONTRACT_TRADE_DATE' WHERE position_id = ?""",
                    (position_id,),
                )

    def record_decision(
        self,
        *,
        market_id: str,
        outcome: str,
        fair_yes_probability: float,
        executable_ask: float,
        edge: float,
        should_record_paper_trade: bool,
        payload: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> str:
        if outcome not in {"YES", "NO"}:
            raise ValueError("outcome must be YES or NO")
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest_source = f"{timestamp.isoformat()}|{market_id}|{outcome}|{payload_json}"
        decision_id = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

        with _database_connection(self.path) as connection:
            connection.execute(
                """
                INSERT INTO shadow_decisions (
                    id, created_at, market_id, outcome, fair_yes_probability,
                    executable_ask, edge, should_record_paper_trade, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    timestamp.isoformat(),
                    market_id,
                    outcome,
                    fair_yes_probability,
                    executable_ask,
                    edge,
                    int(should_record_paper_trade),
                    payload_json,
                ),
            )
        return decision_id

    def upsert_market_candidate(self, candidate: object) -> None:
        """Persist raw terms for human review; accepts the discovery dataclass lazily."""

        raw_payload = getattr(candidate, "raw_payload")
        values = (
            getattr(candidate, "market_id"),
            datetime.now(UTC).isoformat(),
            getattr(candidate, "question"),
            getattr(candidate, "slug"),
            getattr(candidate, "end_date"),
            getattr(candidate, "resolution_source"),
            getattr(candidate, "outcome_a_token_id"),
            getattr(candidate, "outcome_b_token_id"),
            getattr(candidate, "review_status"),
            json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str),
        )
        with _database_connection(self.path) as connection:
            connection.execute(
                """
                INSERT INTO market_candidates (
                    market_id, discovered_at, question, slug, end_date, resolution_source,
                    yes_token_id, no_token_id, outcome_a_label, outcome_b_label,
                    outcome_a_token_id, outcome_b_token_id, review_status, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    discovered_at=excluded.discovered_at,
                    question=excluded.question,
                    slug=excluded.slug,
                    end_date=excluded.end_date,
                    resolution_source=excluded.resolution_source,
                    yes_token_id=excluded.yes_token_id,
                    no_token_id=excluded.no_token_id,
                    outcome_a_label=excluded.outcome_a_label,
                    outcome_b_label=excluded.outcome_b_label,
                    outcome_a_token_id=excluded.outcome_a_token_id,
                    outcome_b_token_id=excluded.outcome_b_token_id,
                    review_status=excluded.review_status,
                    raw_payload_json=excluded.raw_payload_json
                """,
                (
                    values[0], values[1], values[2], values[3], values[4], values[5],
                    values[6], values[7], getattr(candidate, "outcome_a_label"),
                    getattr(candidate, "outcome_b_label"), getattr(candidate, "outcome_a_token_id"),
                    getattr(candidate, "outcome_b_token_id"), values[8], values[9],
                ),
            )

    def record_order_book_snapshot(self, market_id: str, snapshot: object) -> None:
        raw_payload = getattr(snapshot, "raw_payload")
        with _database_connection(self.path) as connection:
            connection.execute(
                """
                INSERT INTO order_book_snapshots (
                    observed_at, market_id, token_id, best_bid, best_ask, midpoint, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    getattr(snapshot, "observed_at").isoformat(),
                    market_id,
                    getattr(snapshot, "token_id"),
                    getattr(snapshot, "best_bid"),
                    getattr(snapshot, "best_ask"),
                    getattr(snapshot, "midpoint"),
                    json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def record_spot_observation(self, payload: Mapping[str, object]) -> None:
        """Persist at most one received source quote per source/symbol/second."""

        try:
            observed_at = datetime.fromisoformat(str(payload["observed_at"]))
            source = str(payload["source"]).upper()
            symbol = str(payload["symbol"]).upper()
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("spot observation is invalid") from error
        if observed_at.tzinfo is None or not source or not symbol or price <= 0:
            raise ValueError("spot observation is invalid")
        published_at = payload.get("published_at")
        confidence = payload.get("confidence")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO spot_observations (
                    observed_at, observed_second, source, symbol, price, published_at, confidence, feed_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(), observed_at.replace(microsecond=0).isoformat(), source, symbol, price,
                    str(published_at) if published_at else None,
                    float(confidence) if confidence is not None else None,
                    str(payload["feed_id"]) if payload.get("feed_id") else None,
                ),
            )

    def record_spot_source_comparison(self, payload: Mapping[str, object]) -> None:
        """Persist bounded Pyth-versus-primary source divergence diagnostics."""

        try:
            observed_at = datetime.fromisoformat(str(payload["observed_at"]))
            symbol = str(payload["symbol"]).upper()
            primary_source = str(payload["primary_source"]).upper()
            primary_price = float(payload["primary_price"])
            pyth_price = float(payload["pyth_price"])
            difference_bps = float(payload["difference_bps"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("spot source comparison is invalid") from error
        if observed_at.tzinfo is None or not symbol or not primary_source or min(primary_price, pyth_price) <= 0:
            raise ValueError("spot source comparison is invalid")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO spot_source_comparisons (
                    observed_at, observed_second, symbol, primary_source, primary_price, primary_published_at,
                    pyth_price, pyth_published_at, pyth_confidence, pyth_feed_id, difference_bps
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(), observed_at.replace(microsecond=0).isoformat(), symbol, primary_source,
                    primary_price, payload.get("primary_published_at"), pyth_price, payload.get("pyth_published_at"),
                    float(payload["pyth_confidence"]) if payload.get("pyth_confidence") is not None else None,
                    payload.get("pyth_feed_id"), difference_bps,
                ),
            )

    def record_execution_observation(
        self, *, observed_at: datetime, signal_id: str | None, observation_kind: str, market_id: str,
        symbol: str, outcome: str, token_id: str, spot: float | None, price_to_beat: float | None,
        fair_probability: float | None, best_bid: float | None, best_ask: float | None,
        fee_rate: float | None, book_payload: Mapping[str, object], evaluation_payload: Mapping[str, object],
    ) -> None:
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("execution observation outcome must be UP or DOWN")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO execution_observations (
                    observed_at, signal_id, observation_kind, market_id, symbol, outcome, token_id,
                    spot, price_to_beat, fair_probability, best_bid, best_ask, fee_rate,
                    book_payload_json, evaluation_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(), signal_id, observation_kind, market_id, symbol, outcome, token_id,
                    spot, price_to_beat, fair_probability, best_bid, best_ask, fee_rate,
                    json.dumps(book_payload, sort_keys=True, separators=(",", ":"), default=str),
                    json.dumps(evaluation_payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def get_market_outcome_tokens(self, market_id: str) -> tuple[StoredOutcomeToken, StoredOutcomeToken]:
        """Return both outcome tokens for a discovered market."""

        with _database_connection(self.path) as connection:
            row = connection.execute(
                """
                SELECT outcome_a_label, outcome_a_token_id, outcome_b_label, outcome_b_token_id
                FROM market_candidates WHERE market_id = ?
                """,
                (market_id,),
            ).fetchone()
        if row is None or not all(row):
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        return (
            StoredOutcomeToken(label=row[0], token_id=row[1]),
            StoredOutcomeToken(label=row[2], token_id=row[3]),
        )

    def list_market_candidates(self, symbol: str | None = None) -> tuple[StoredMarketCandidate, ...]:
        """Return concise local candidate metadata without exposing CLOB token IDs."""

        normalized_symbol = symbol.strip().upper() if symbol else ""
        query = """
            SELECT market_id, question, slug, end_date, outcome_a_label, outcome_b_label, review_status
            FROM market_candidates
        """
        parameters: tuple[object, ...] = ()
        if normalized_symbol:
            query += " WHERE UPPER(question) LIKE ?"
            parameters = (f"%{normalized_symbol}%",)
        query += " ORDER BY end_date ASC, market_id ASC"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(StoredMarketCandidate(*row) for row in rows)

    def get_market_candidate(self, market_id: str) -> StoredMarketCandidate:
        with _database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT market_id, question, slug, end_date, outcome_a_label, outcome_b_label, review_status
                FROM market_candidates WHERE market_id = ?""",
                (market_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        return StoredMarketCandidate(*row)

    def get_market_candidate_raw_payload(self, market_id: str) -> Mapping[str, object]:
        """Return the persisted Gamma payload so contract terms can be revalidated."""

        with _database_connection(self.path) as connection:
            row = connection.execute(
                "SELECT raw_payload_json FROM market_candidates WHERE market_id = ?", (market_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError(f"market {market_id} has an invalid persisted raw payload")
        return payload

    def get_latest_outcome_asks(self, market_id: str) -> tuple[float, float]:
        outcomes = self.get_market_outcome_tokens(market_id)
        asks: list[float] = []
        with _database_connection(self.path) as connection:
            for outcome in outcomes:
                row = connection.execute(
                    """SELECT best_ask FROM order_book_snapshots
                    WHERE market_id = ? AND token_id = ? ORDER BY id DESC LIMIT 1""",
                    (market_id, outcome.token_id),
                ).fetchone()
                if row is None or row[0] is None:
                    raise KeyError(f"market {market_id} has no stored ask for {outcome.label}")
                asks.append(float(row[0]))
        return asks[0], asks[1]

    def record_alpaca_indicative_option_quote(self, quote: object) -> None:
        with _database_connection(self.path) as connection:
            connection.execute(
                """
                INSERT INTO alpaca_indicative_option_quotes (
                    observed_at, option_symbol, bid_price, ask_price, feed, quality_label
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    getattr(quote, "observed_at").isoformat(),
                    getattr(quote, "symbol"),
                    getattr(quote, "bid_price"),
                    getattr(quote, "ask_price"),
                    getattr(quote, "feed"),
                    getattr(quote, "quality_label"),
                ),
            )

    def record_realtime_evaluation(self, payload: Mapping[str, object]) -> None:
        """Persist every fresh or rejected real-time shadow evaluation for calibration."""

        required = {"evaluated_at", "market_id", "symbol", "signal_status", "skip_reasons"}
        missing = required.difference(payload)
        if missing:
            raise ValueError(f"realtime evaluation is missing: {', '.join(sorted(missing))}")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO realtime_evaluations (
                    evaluated_at, market_id, symbol, spot, up_ask, down_ask,
                    fair_up_probability, signal_status, skip_reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload["evaluated_at"]), str(payload["market_id"]), str(payload["symbol"]),
                    payload.get("spot"), payload.get("up_ask"), payload.get("down_ask"),
                    payload.get("fair_up_probability"), str(payload["signal_status"]),
                    json.dumps(payload["skip_reasons"], sort_keys=True),
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def record_checkpoint_observation(
        self, *, checkpoint_date: str, checkpoint_name: str, payload: Mapping[str, object],
        maximum_delay_seconds: float = DEFAULT_MAXIMUM_DELAY_SECONDS,
    ) -> bool:
        """Store the first valid observation after a fixed daily research checkpoint."""

        required = {"evaluated_at", "market_id", "symbol", "fair_up_probability", "model_version"}
        missing = required.difference(payload)
        if missing:
            raise ValueError(f"checkpoint observation is missing: {', '.join(sorted(missing))}")
        evaluated_at = datetime.fromisoformat(str(payload["evaluated_at"]))
        target_at = checkpoint_target_at(checkpoint_date, checkpoint_name)
        delay_seconds = max(0.0, (evaluated_at - target_at).total_seconds())
        eligible = delay_seconds <= maximum_delay_seconds
        with _database_connection(self.path) as connection:
            return connection.execute(
                """INSERT OR IGNORE INTO checkpoint_observations (
                    market_id, symbol, checkpoint_date, checkpoint_name, evaluated_at,
                    fair_up_probability, up_ask, down_ask, model_version, option_iv, payload_json,
                    checkpoint_target_at, checkpoint_delay_seconds, eligible_for_calibration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload["market_id"]), str(payload["symbol"]), checkpoint_date, checkpoint_name,
                    str(payload["evaluated_at"]), float(payload["fair_up_probability"]), payload.get("up_ask"),
                    payload.get("down_ask"), str(payload["model_version"]), payload.get("option_iv"),
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                    target_at.isoformat(), delay_seconds, int(eligible),
                ),
            ).rowcount == 1

    def record_portfolio_decision(
        self, *, batch_id: str, market_id: str, symbol: str, outcome: str, risk_group: str,
        edge: float, selected: bool, reason: str, payload: Mapping[str, object], created_at: datetime | None = None,
    ) -> None:
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("portfolio decision outcome must be UP or DOWN")
        timestamp = created_at or datetime.now(UTC)
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO portfolio_decisions (
                    created_at, batch_id, market_id, symbol, outcome, risk_group, edge, status, reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp.isoformat(), batch_id, market_id, symbol.upper(), outcome, risk_group, edge,
                    "SELECTED" if selected else "REJECTED", reason,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def list_portfolio_decisions(self, limit: int = 100) -> tuple[Mapping[str, object], ...]:
        if limit < 1:
            raise ValueError("portfolio decision limit must be positive")
        with _database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT created_at, batch_id, market_id, symbol, outcome, risk_group, edge, status, reason, payload_json
                FROM portfolio_decisions ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return tuple({
            "created_at": str(row[0]), "batch_id": str(row[1]), "market_id": str(row[2]), "symbol": str(row[3]),
            "outcome": str(row[4]), "risk_group": str(row[5]), "edge": float(row[6]), "status": str(row[7]),
            "reason": str(row[8]), "payload": json.loads(str(row[9])),
        } for row in rows)

    def list_checkpoint_observations(self, *, eligible_only: bool = True) -> tuple[CheckpointObservation, ...]:
        query = """SELECT checkpoint.market_id, checkpoint.symbol, checkpoint.checkpoint_date,
            checkpoint.checkpoint_name, checkpoint.evaluated_at, checkpoint.fair_up_probability,
            checkpoint.up_ask, checkpoint.down_ask, checkpoint.model_version, checkpoint.option_iv,
            settlement.winning_outcome, checkpoint.checkpoint_target_at, checkpoint.checkpoint_delay_seconds,
            checkpoint.eligible_for_calibration
          FROM checkpoint_observations AS checkpoint
          JOIN market_settlements AS settlement ON settlement.market_id = checkpoint.market_id
          WHERE (? = 0 OR checkpoint.eligible_for_calibration = 1)
          ORDER BY checkpoint.checkpoint_date, checkpoint.checkpoint_name, checkpoint.market_id"""
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, (int(eligible_only),)).fetchall()
        return tuple(CheckpointObservation(
            market_id=str(row[0]), symbol=str(row[1]), checkpoint_date=str(row[2]), checkpoint_name=str(row[3]),
            evaluated_at=datetime.fromisoformat(str(row[4])), fair_up_probability=float(row[5]),
            up_ask=float(row[6]) if row[6] is not None else None,
            down_ask=float(row[7]) if row[7] is not None else None, model_version=str(row[8]),
            option_iv=float(row[9]) if row[9] is not None else None, winning_outcome=str(row[10]),
            checkpoint_target_at=datetime.fromisoformat(str(row[11])), checkpoint_delay_seconds=float(row[12]),
            eligible_for_calibration=bool(row[13]),
        ) for row in rows)

    def list_buffer_sweep_observations(self) -> tuple[BufferSweepObservation, ...]:
        """Return immutable, on-time checkpoints with their original executable costs."""

        query = """SELECT checkpoint.market_id, checkpoint.symbol, checkpoint.checkpoint_date,
            checkpoint.checkpoint_name, checkpoint.evaluated_at, checkpoint.fair_up_probability,
            checkpoint.up_ask, checkpoint.down_ask, checkpoint.payload_json, settlement.winning_outcome
          FROM checkpoint_observations AS checkpoint
          JOIN market_settlements AS settlement ON settlement.market_id = checkpoint.market_id
          WHERE checkpoint.eligible_for_calibration = 1
          ORDER BY checkpoint.checkpoint_date, checkpoint.evaluated_at, checkpoint.market_id"""
        with _database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(str(row[8]))
            comparison_models = payload.get("comparison_models")
            observations.append(BufferSweepObservation(
                market_id=str(row[0]), symbol=str(row[1]), checkpoint_date=str(row[2]), checkpoint_name=str(row[3]),
                evaluated_at=datetime.fromisoformat(str(row[4])), fair_up_probability=float(row[5]),
                up_ask=float(row[6]) if row[6] is not None else None,
                down_ask=float(row[7]) if row[7] is not None else None,
                up_taker_fee=_payload_execution_fee(payload, "up"),
                down_taker_fee=_payload_execution_fee(payload, "down"), winning_outcome=str(row[9]),
                spot=_optional_float(payload.get("spot")),
                price_to_beat=_optional_float(payload.get("price_to_beat")),
                up_bid=_optional_float(payload.get("up_bid")), down_bid=_optional_float(payload.get("down_bid")),
                annualized_volatility=_optional_float(payload.get("annualized_realized_volatility")),
                cross_source_difference=_optional_float(payload.get("cross_source_difference")),
                comparison_models=tuple(
                    dict(item) for item in comparison_models if isinstance(item, Mapping)
                ) if isinstance(comparison_models, list) else (),
                payload=payload,
            ))
        return tuple(observations)

    def list_execution_observations(self) -> tuple[ExecutionObservation, ...]:
        query = """SELECT observed_at, signal_id, observation_kind, market_id, symbol, outcome, token_id,
            spot, price_to_beat, fair_probability, best_bid, best_ask, fee_rate,
            book_payload_json, evaluation_payload_json
          FROM execution_observations ORDER BY observed_at, id"""
        with _database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        return tuple(ExecutionObservation(
            observed_at=datetime.fromisoformat(str(row[0])), signal_id=str(row[1]) if row[1] else None,
            observation_kind=str(row[2]), market_id=str(row[3]), symbol=str(row[4]), outcome=str(row[5]),
            token_id=str(row[6]), spot=_optional_float(row[7]), price_to_beat=_optional_float(row[8]),
            fair_probability=_optional_float(row[9]), best_bid=_optional_float(row[10]),
            best_ask=_optional_float(row[11]), fee_rate=_optional_float(row[12]),
            book_payload=json.loads(str(row[13])), evaluation_payload=json.loads(str(row[14])),
        ) for row in rows)

    def list_spot_observations(
        self, *, source: str | None = None, sample_every_seconds: int = 1,
    ) -> tuple[StoredSpotObservation, ...]:
        if sample_every_seconds < 1:
            raise ValueError("sample_every_seconds must be positive")
        query = "SELECT observed_at, source, symbol, price, published_at FROM spot_observations"
        conditions = []
        parameters: list[object] = []
        if source:
            conditions.append("source = ?")
            parameters.append(source.upper())
        if sample_every_seconds > 1:
            conditions.append("unixepoch(observed_at) % ? = 0")
            parameters.append(sample_every_seconds)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY observed_at, id"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(StoredSpotObservation(
            observed_at=datetime.fromisoformat(str(row[0])), source=str(row[1]), symbol=str(row[2]),
            price=float(row[3]), published_at=datetime.fromisoformat(str(row[4])) if row[4] else None,
        ) for row in rows)

    def list_spot_source_comparisons(self, *, sample_every_seconds: int = 1) -> tuple[SpotSourceComparison, ...]:
        if sample_every_seconds < 1:
            raise ValueError("sample_every_seconds must be positive")
        query = """SELECT observed_at, symbol, primary_source, primary_price, pyth_price,
            pyth_confidence, difference_bps, primary_published_at, pyth_published_at FROM spot_source_comparisons"""
        parameters: tuple[object, ...] = ()
        if sample_every_seconds > 1:
            query += " WHERE unixepoch(observed_at) % ? = 0"
            parameters = (sample_every_seconds,)
        query += " ORDER BY observed_at, id"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(SpotSourceComparison(
            observed_at=datetime.fromisoformat(str(row[0])), symbol=str(row[1]), primary_source=str(row[2]),
            primary_price=float(row[3]), pyth_price=float(row[4]), pyth_confidence=_optional_float(row[5]),
            difference_bps=float(row[6]),
            primary_published_at=datetime.fromisoformat(str(row[7])) if row[7] else None,
            pyth_published_at=datetime.fromisoformat(str(row[8])) if row[8] else None,
        ) for row in rows)

    def record_contract_review(
        self, market_id: str, *, accepted: bool, reason: str, contract: Mapping[str, object] | None = None
    ) -> None:
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO market_contract_reviews (market_id, reviewed_at, status, reason, contract_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET reviewed_at=excluded.reviewed_at, status=excluded.status,
                    reason=excluded.reason, contract_json=excluded.contract_json""",
                (
                    market_id, datetime.now(UTC).isoformat(), "ACCEPTED" if accepted else "REJECTED", reason,
                    json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str) if contract else None,
                ),
            )

    def get_market_settlement_outcome(self, market_id: str) -> str:
        """Return the previously reconciled official market outcome."""

        with _database_connection(self.path) as connection:
            row = connection.execute(
                "SELECT winning_outcome FROM market_settlements WHERE market_id = ?", (market_id,)
            ).fetchone()
        if row is None:
            raise KeyError(market_id)
        return str(row[0])

    def record_market_settlement(self, market_id: str, winning_outcome: str, payload: Mapping[str, object]) -> None:
        if winning_outcome not in {"UP", "DOWN"}:
            raise ValueError("winning_outcome must be UP or DOWN")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO market_settlements (market_id, settled_at, winning_outcome, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET settled_at=excluded.settled_at,
                    winning_outcome=excluded.winning_outcome, payload_json=excluded.payload_json""",
                (market_id, datetime.now(UTC).isoformat(), winning_outcome,
                 json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)),
            )

    def pending_evaluation_market_ids(self, limit: int = 100) -> tuple[str, ...]:
        with _database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT DISTINCT market_id FROM realtime_evaluations
                WHERE fair_up_probability IS NOT NULL
                  AND market_id NOT IN (SELECT market_id FROM market_settlements)
                ORDER BY market_id LIMIT ?""", (limit,)
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_replay_observations(self) -> tuple[ReplayObservation, ...]:
        """Return one latest valid model observation per officially settled market."""

        query = """WITH latest AS (
            SELECT market_id, MAX(evaluated_at) AS evaluated_at FROM realtime_evaluations
            WHERE fair_up_probability IS NOT NULL GROUP BY market_id
        ) SELECT evaluation.market_id, evaluation.symbol, evaluation.evaluated_at,
            evaluation.fair_up_probability, evaluation.up_ask, evaluation.down_ask, settlement.winning_outcome
          FROM latest
          JOIN realtime_evaluations AS evaluation
            ON evaluation.market_id = latest.market_id AND evaluation.evaluated_at = latest.evaluated_at
          JOIN market_settlements AS settlement ON settlement.market_id = evaluation.market_id
          ORDER BY evaluation.evaluated_at ASC"""
        with _database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        return tuple(ReplayObservation(
            market_id=str(row[0]), symbol=str(row[1]), evaluated_at=datetime.fromisoformat(str(row[2])),
            fair_up_probability=float(row[3]), up_ask=float(row[4]) if row[4] is not None else None,
            down_ask=float(row[5]) if row[5] is not None else None, winning_outcome=str(row[6]),
        ) for row in rows)

    def list_first_signal_calibration_observations(self) -> tuple[FirstSignalCalibrationObservation, ...]:
        """Return exactly one pre-settlement model signal per officially settled market.

        These are selected-side probabilities, rather than raw Fair-Up probabilities,
        so calibration measures the probability the policy actually acted on.
        """

        query = """WITH ranked AS (
            SELECT evaluated_at, market_id, symbol, fair_up_probability, up_ask, down_ask, payload_json,
                ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY evaluated_at) AS row_number
            FROM realtime_evaluations
            WHERE fair_up_probability IS NOT NULL
              AND json_extract(payload_json, '$.model_outcome') IN ('UP', 'DOWN')
        ) SELECT ranked.evaluated_at, ranked.market_id, ranked.symbol, ranked.fair_up_probability,
            ranked.up_ask, ranked.down_ask, ranked.payload_json, settlement.winning_outcome
          FROM ranked
          JOIN market_settlements AS settlement USING (market_id)
          WHERE ranked.row_number = 1
          ORDER BY ranked.evaluated_at, ranked.market_id"""
        with _database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(str(row[6]))
            outcome = str(payload["model_outcome"])
            fair_up = float(row[3])
            entry_ask = float(row[4] if outcome == "UP" else row[5])
            entry_fee_value = payload.get("up_taker_fee") if outcome == "UP" else payload.get("down_taker_fee")
            spot = payload.get("spot")
            threshold = payload.get("prior_close")
            threshold_distance_bps = None
            if spot is not None and threshold is not None and float(threshold) > 0:
                threshold_distance_bps = (float(spot) / float(threshold) - 1.0) * 10_000
            option_iv_status = str(payload.get("option_iv_status") or "IV_UNAVAILABLE")
            observations.append(FirstSignalCalibrationObservation(
                market_id=str(row[1]), symbol=str(row[2]), evaluated_at=datetime.fromisoformat(str(row[0])),
                model_outcome=outcome,
                selected_fair_probability=fair_up if outcome == "UP" else 1.0 - fair_up,
                entry_ask=entry_ask, entry_fee=float(entry_fee_value) if entry_fee_value is not None else None,
                winning_outcome=str(row[7]), model_version=str(payload.get("model_version") or "unknown"),
                option_iv_status=option_iv_status,
                iv_regime="IV_VALID" if option_iv_status == "IV_VALID" else "REALIZED_VOL_FALLBACK",
                spot_provider=str(payload.get("spot_provider") or "unknown"),
                threshold_distance_bps=threshold_distance_bps,
                volatility_estimator=str(payload.get("volatility_estimator") or "CLOSE_TO_CLOSE"),
            ))
        return tuple(observations)

    def first_signal_performance(self) -> Mapping[str, object]:
        """Summarize one first model signal per officially settled market."""

        query = """WITH ranked AS (
            SELECT market_id, json_extract(payload_json, '$.model_outcome') AS prediction,
                ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY evaluated_at) AS row_number
            FROM realtime_evaluations
            WHERE json_extract(payload_json, '$.model_outcome') IN ('UP', 'DOWN')
        ) SELECT COUNT(*), SUM(ranked.prediction = settlement.winning_outcome)
          FROM ranked
          JOIN market_settlements AS settlement USING (market_id)
          WHERE ranked.row_number = 1"""
        with _database_connection(self.path) as connection:
            count, wins = connection.execute(query).fetchone()
        settled_markets = int(count or 0)
        correct = int(wins or 0)
        return {
            "settled_markets": settled_markets,
            "wins": correct,
            "losses": settled_markets - correct,
            "win_rate": correct / settled_markets if settled_markets else None,
        }

    def dashboard_rows(
        self, limit: int = 18, *, now: datetime | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Return stable symbol rows with immutable same-day checkpoint decisions."""
        if limit < 1:
            raise ValueError("dashboard limit must be positive")
        ny_date = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        with _database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT evaluation.payload_json FROM realtime_evaluations AS evaluation
                JOIN market_candidates AS candidate ON candidate.market_id = evaluation.market_id
                WHERE evaluation.id IN (SELECT MAX(id) FROM realtime_evaluations GROUP BY market_id)
                  AND date(candidate.end_date) = ?
                ORDER BY evaluation.evaluated_at DESC""", (ny_date,)
            ).fetchall()
            checkpoint_rows = connection.execute(
                """SELECT market_id, checkpoint_name, payload_json FROM checkpoint_observations
                WHERE checkpoint_date = ? AND eligible_for_calibration = 1
                  AND checkpoint_name IN ('1200_EDT', '1400_EDT', '1530_EDT')
                ORDER BY evaluated_at, id""", (ny_date,)
            ).fetchall()

        # Old journals can contain overlapping regular and after-hours contracts.
        # Keep one stable row per symbol and prefer the regular-session contract.
        by_symbol: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = json.loads(str(row[0]))
            symbol = str(payload.get("symbol") or "").upper()
            if not symbol:
                continue
            current = by_symbol.get(symbol)
            candidate_rank = (payload.get("market_session") == "REGULAR", str(payload.get("evaluated_at") or ""))
            current_rank = (
                current.get("market_session") == "REGULAR", str(current.get("evaluated_at") or "")
            ) if current else (False, "")
            if current is None or candidate_rank > current_rank:
                by_symbol[symbol] = payload

        checkpoints_by_market: dict[str, dict[str, Mapping[str, object]]] = {}
        for market_id, checkpoint_name, payload_json in checkpoint_rows:
            checkpoints_by_market.setdefault(str(market_id), {})[str(checkpoint_name)] = json.loads(str(payload_json))
        selected = []
        for symbol in sorted(by_symbol)[:limit]:
            payload = dict(by_symbol[symbol])
            payload["checkpoints"] = checkpoints_by_market.get(str(payload.get("market_id")), {})
            selected.append(payload)
        return tuple(selected)

    def open_paper_position(
        self,
        *,
        market_id: str,
        symbol: str,
        outcome: str,
        entry_ask: float,
        fair_probability: float,
        model_version: str,
        payload: Mapping[str, object],
        contracts: float = 1.0,
        fee_rate: float,
        opened_at: datetime | None = None,
    ) -> tuple[PaperPosition, bool]:
        """Create one hold-to-settlement paper entry, idempotently per market/outcome."""

        if outcome not in {"UP", "DOWN"}:
            raise ValueError("paper position outcome must be UP or DOWN")
        if not 0 <= entry_ask <= 1 or not 0 <= fair_probability <= 1:
            raise ValueError("entry_ask and fair_probability must be between 0 and 1")
        if contracts <= 0 or fee_rate < 0 or not model_version:
            raise ValueError("invalid paper position inputs")
        timestamp = opened_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        position_id = hashlib.sha256(f"{market_id}|{outcome}".encode("utf-8")).hexdigest()
        entry_fee = estimate_taker_fee_usdc(shares=contracts, price=entry_ask, fee_rate=fee_rate)
        entry_slippage = 0.0
        with _database_connection(self.path) as connection:
            open_row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                    settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                FROM paper_positions WHERE market_id = ? AND status = 'OPEN'""",
                (market_id,),
            ).fetchone()
            if open_row is not None:
                return _paper_position_from_row(open_row), False
            inserted = connection.execute(
                """INSERT OR IGNORE INTO paper_positions (
                    position_id, opened_at, market_id, symbol, outcome, status, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version, entry_payload_json
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position_id, timestamp.isoformat(), market_id, symbol.upper(), outcome, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            ).rowcount == 1
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                    settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("paper position insert did not return a row")
        return _paper_position_from_row(row), inserted

    def sync_maker_shadow_quote(
        self,
        *,
        market_id: str,
        symbol: str,
        outcome: str,
        limit_price: float | None,
        fair_probability: float | None,
        theoretical_edge: float | None,
        best_bid: float | None,
        best_ask: float | None,
        payload: Mapping[str, object],
        no_quote_reason: str = "NO_MAKER_EDGE",
        minimum_reprice_price_change: float = 0.0,
        minimum_quote_lifetime_seconds: float = 0.0,
        observed_at: datetime | None = None,
    ) -> tuple[MakerShadowQuote | None, str | None]:
        """Create, reprice, or cancel one passive quote without assuming a fill."""

        if outcome not in {"UP", "DOWN"}:
            raise ValueError("maker quote outcome must be UP or DOWN")
        if minimum_reprice_price_change < 0 or minimum_quote_lifetime_seconds < 0:
            raise ValueError("maker reprice thresholds must be non-negative")
        timestamp = observed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        has_proposal = None not in (limit_price, fair_probability, theoretical_edge, best_bid, best_ask)
        if has_proposal:
            if not (0 < float(limit_price) < 1 and 0 <= float(fair_probability) <= 1 and 0 <= float(best_bid) <= float(best_ask) <= 1):
                raise ValueError("invalid maker quote proposal")
        with _database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                    limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                    last_touched_at, cancelled_at, cancel_reason
                FROM maker_shadow_quotes WHERE market_id = ? AND outcome = ? AND status = 'ACTIVE'""",
                (market_id, outcome),
            ).fetchone()
            active = _maker_shadow_quote_from_row(row) if row is not None else None
            if not has_proposal:
                if active is None:
                    return None, None
                connection.execute(
                    """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?,
                    cancel_reason = ?, last_observed_at = ? WHERE quote_id = ?""",
                    (timestamp.isoformat(), no_quote_reason, timestamp.isoformat(), active.quote_id),
                )
                return None, "CANCELLED"
            if active is not None and active.limit_price == float(limit_price):
                connection.execute(
                    """UPDATE maker_shadow_quotes SET last_observed_at = ?, fair_probability = ?,
                    theoretical_edge = ?, best_bid = ?, best_ask = ?, payload_json = ? WHERE quote_id = ?""",
                    (
                        timestamp.isoformat(), fair_probability, theoretical_edge, best_bid, best_ask,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), active.quote_id,
                    ),
                )
                return replace(
                    active, last_observed_at=timestamp, fair_probability=float(fair_probability),
                    theoretical_edge=float(theoretical_edge), best_bid=float(best_bid), best_ask=float(best_ask),
                ), None
            if active is not None:
                price_change = abs(active.limit_price - float(limit_price))
                quote_age_seconds = max(0.0, (timestamp - active.created_at).total_seconds())
                should_hold = (
                    price_change < minimum_reprice_price_change
                    or quote_age_seconds < minimum_quote_lifetime_seconds
                )
                if should_hold:
                    connection.execute(
                        """UPDATE maker_shadow_quotes SET last_observed_at = ?, fair_probability = ?,
                        theoretical_edge = ?, best_bid = ?, best_ask = ?, payload_json = ? WHERE quote_id = ?""",
                        (
                            timestamp.isoformat(), fair_probability, theoretical_edge, best_bid, best_ask,
                            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), active.quote_id,
                        ),
                    )
                    return replace(
                        active, last_observed_at=timestamp, fair_probability=float(fair_probability),
                        theoretical_edge=float(theoretical_edge), best_bid=float(best_bid), best_ask=float(best_ask),
                    ), None
            action = "OPENED" if active is None else "REPRICED"
            if active is not None:
                connection.execute(
                    """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?,
                    cancel_reason = 'REPRICE_LIMIT_CHANGED', last_observed_at = ? WHERE quote_id = ?""",
                    (timestamp.isoformat(), timestamp.isoformat(), active.quote_id),
                )
            quote_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO maker_shadow_quotes (
                    quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                    limit_price, fair_probability, theoretical_edge, best_bid, best_ask, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)""",
                (
                    quote_id, timestamp.isoformat(), timestamp.isoformat(), market_id, symbol.upper(), outcome,
                    limit_price, fair_probability, theoretical_edge, best_bid, best_ask,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )
            row = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                    limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                    last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes WHERE quote_id = ?""",
                (quote_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("maker quote insert did not return a row")
        return _maker_shadow_quote_from_row(row), action

    def record_maker_shadow_touch(
        self, *, market_id: str, outcome: str, current_ask: float, observed_at: datetime | None = None
    ) -> MakerShadowQuote | None:
        """Record an ask touching an active quote; a touch is explicitly not a fill."""

        if outcome not in {"UP", "DOWN"} or not 0 <= current_ask <= 1:
            raise ValueError("invalid maker touch inputs")
        timestamp = observed_at or datetime.now(UTC)
        with _database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT quote_id, limit_price FROM maker_shadow_quotes
                WHERE market_id = ? AND outcome = ? AND status = 'ACTIVE'""", (market_id, outcome)
            ).fetchone()
            if row is None or current_ask > float(row[1]):
                return None
            connection.execute(
                """UPDATE maker_shadow_quotes SET touch_count = touch_count + 1,
                    last_touched_at = ?, last_observed_at = ? WHERE quote_id = ?""",
                (timestamp.isoformat(), timestamp.isoformat(), str(row[0])),
            )
            updated = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                    limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                    last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes WHERE quote_id = ?""",
                (str(row[0]),),
            ).fetchone()
        return _maker_shadow_quote_from_row(updated) if updated is not None else None

    def cancel_maker_shadow_quotes(self, market_id: str, reason: str, cancelled_at: datetime | None = None) -> int:
        timestamp = cancelled_at or datetime.now(UTC)
        with _database_connection(self.path) as connection:
            return connection.execute(
                """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?, cancel_reason = ?,
                last_observed_at = ? WHERE market_id = ? AND status = 'ACTIVE'""",
                (timestamp.isoformat(), reason, timestamp.isoformat(), market_id),
            ).rowcount

    def list_maker_shadow_quotes(self, status: str | None = None) -> tuple[MakerShadowQuote, ...]:
        if status is not None and status not in {"ACTIVE", "CANCELLED"}:
            raise ValueError("maker quote status must be ACTIVE or CANCELLED")
        query = """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
            limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
            last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes"""
        parameters: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY created_at DESC"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_maker_shadow_quote_from_row(row) for row in rows)

    def list_paper_positions(self, status: str | None = None) -> tuple[PaperPosition, ...]:
        if status is not None and status not in {"OPEN", "SETTLED"}:
            raise ValueError("status must be OPEN or SETTLED")
        query = """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
            entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
            settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
            FROM paper_positions"""
        parameters: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY opened_at ASC"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_paper_position_from_row(row) for row in rows)

    def settle_paper_position(
        self,
        position_id: str,
        *,
        settlement_outcome: str,
        settlement_payload: Mapping[str, object],
        settled_at: datetime | None = None,
    ) -> PaperPosition:
        if settlement_outcome not in {"UP", "DOWN"}:
            raise ValueError("settlement_outcome must be UP or DOWN")
        timestamp = settled_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        with _database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                    settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown paper position {position_id}")
            position = _paper_position_from_row(row)
            if position.status == "SETTLED":
                return position
            payout = position.contracts if position.outcome == settlement_outcome else 0.0
            realized_pnl = payout - (position.entry_ask * position.contracts + position.entry_fee + position.entry_slippage)
            connection.execute(
                """UPDATE paper_positions SET status = 'SETTLED', settled_at = ?, settlement_outcome = ?,
                    payout = ?, realized_pnl = ?, settlement_payload_json = ? WHERE position_id = ?""",
                (
                    timestamp.isoformat(), settlement_outcome, payout, realized_pnl,
                    json.dumps(settlement_payload, sort_keys=True, separators=(",", ":"), default=str), position_id,
                ),
            )
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                    entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                    settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("paper position settlement did not return a row")
        return _paper_position_from_row(row)


def _payload_execution_fee(payload: Mapping[str, object], outcome_prefix: str) -> float | None:
    """Use the fee frozen with the checkpoint, never a recalculated current fee."""

    value = payload.get(f"{outcome_prefix}_taker_fee")
    if value is None:
        return None
    try:
        fee = float(value)
    except (TypeError, ValueError):
        return None
    return fee if fee >= 0 else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_position_from_row(row: tuple[object, ...]) -> PaperPosition:
    return PaperPosition(
        position_id=str(row[0]), opened_at=datetime.fromisoformat(str(row[1])), market_id=str(row[2]),
        symbol=str(row[3]), outcome=str(row[4]), status=str(row[5]), contracts=float(row[6]),
        entry_ask=float(row[7]), entry_fee=float(row[8]), entry_slippage=float(row[9]),
        fair_probability=float(row[10]), model_version=str(row[11]),
        settled_at=datetime.fromisoformat(str(row[12])) if row[12] else None,
        settlement_outcome=str(row[13]) if row[13] else None,
        payout=float(row[14]) if row[14] is not None else None,
        realized_pnl=float(row[15]) if row[15] is not None else None,
        included_in_calibration=bool(row[16]), exclusion_reason=str(row[17]) if row[17] else None,
    )


def _maker_shadow_quote_from_row(row: tuple[object, ...]) -> MakerShadowQuote:
    return MakerShadowQuote(
        quote_id=str(row[0]), created_at=datetime.fromisoformat(str(row[1])),
        last_observed_at=datetime.fromisoformat(str(row[2])), market_id=str(row[3]), symbol=str(row[4]),
        outcome=str(row[5]), status=str(row[6]), limit_price=float(row[7]), fair_probability=float(row[8]),
        theoretical_edge=float(row[9]), best_bid=float(row[10]), best_ask=float(row[11]), touch_count=int(row[12]),
        last_touched_at=datetime.fromisoformat(str(row[13])) if row[13] else None,
        cancelled_at=datetime.fromisoformat(str(row[14])) if row[14] else None,
        cancel_reason=str(row[15]) if row[15] else None,
    )
