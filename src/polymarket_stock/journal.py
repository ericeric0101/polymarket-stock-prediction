"""Append-only SQLite journal for reproducible shadow decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping


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


@contextmanager
def _database_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class ShadowJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _database_connection(self.path) as connection:
            connection.executescript(SCHEMA)
            self._migrate_market_candidate_columns(connection)

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
