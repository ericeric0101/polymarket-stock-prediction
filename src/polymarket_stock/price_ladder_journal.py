"""SQLite persistence isolated to price-ladder research tables."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Mapping

from .journal import _database_connection
from .price_ladder import PriceLadderContract


LADDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_ladder_contracts (
    market_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strike REAL NOT NULL CHECK (strike > 0),
    market_date TEXT NOT NULL,
    resolves_at TEXT NOT NULL,
    pyth_feed TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    question TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status IN ('ACCEPTED', 'REJECTED')),
    review_reason TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_ladder_contracts_symbol_date
    ON price_ladder_contracts (symbol, market_date, strike);
CREATE TABLE IF NOT EXISTS price_ladder_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    observed_second TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market_date TEXT NOT NULL,
    checkpoint_name TEXT,
    strike REAL NOT NULL CHECK (strike > 0),
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    yes_bid_depth REAL NOT NULL DEFAULT 0,
    yes_ask_depth REAL NOT NULL DEFAULT 0,
    no_bid_depth REAL NOT NULL DEFAULT 0,
    no_ask_depth REAL NOT NULL DEFAULT 0,
    yes_book_json TEXT NOT NULL,
    no_book_json TEXT NOT NULL,
    UNIQUE (market_id, observed_second)
);
CREATE INDEX IF NOT EXISTS idx_price_ladder_snapshots_symbol_date_checkpoint
    ON price_ladder_snapshots (symbol, market_date, checkpoint_name, observed_at);
CREATE TABLE IF NOT EXISTS price_ladder_settlements (
    market_id TEXT PRIMARY KEY,
    settled_at TEXT NOT NULL,
    winning_outcome TEXT NOT NULL CHECK (winning_outcome IN ('YES', 'NO')),
    raw_payload_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredLadderSnapshot:
    observed_at: datetime
    market_id: str
    symbol: str
    market_date: str
    checkpoint_name: str | None
    strike: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_bid_depth: float
    yes_ask_depth: float
    no_bid_depth: float
    no_ask_depth: float
    yes_book: Mapping[str, object]
    no_book: Mapping[str, object]


class PriceLadderJournal:
    """Owns only ladder research tables; it never reads or writes paper positions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _database_connection(self.path) as connection:
            connection.executescript(LADDER_SCHEMA)

    def upsert_contract(
        self, contract: PriceLadderContract, *, accepted: bool = True, reason: str = "PYTH_CLOSE_ABOVE_TEMPLATE",
    ) -> None:
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO price_ladder_contracts (
                    market_id, event_id, event_slug, symbol, strike, market_date, resolves_at, pyth_feed,
                    yes_token_id, no_token_id, question, rules_hash, review_status, review_reason, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET event_id=excluded.event_id, event_slug=excluded.event_slug,
                    symbol=excluded.symbol, strike=excluded.strike, market_date=excluded.market_date,
                    resolves_at=excluded.resolves_at, pyth_feed=excluded.pyth_feed,
                    yes_token_id=excluded.yes_token_id, no_token_id=excluded.no_token_id,
                    question=excluded.question, rules_hash=excluded.rules_hash,
                    review_status=excluded.review_status, review_reason=excluded.review_reason,
                    raw_payload_json=excluded.raw_payload_json""",
                (
                    contract.market_id, contract.event_id, contract.event_slug, contract.symbol, contract.strike,
                    contract.market_date, contract.resolves_at.isoformat(), contract.pyth_feed,
                    contract.yes_token_id, contract.no_token_id, contract.question, contract.rules_hash,
                    "ACCEPTED" if accepted else "REJECTED", reason,
                    json.dumps(contract.raw_payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def list_contracts(
        self, *, symbols: tuple[str, ...] = (), market_date: str | None = None, accepted_only: bool = True,
    ) -> tuple[PriceLadderContract, ...]:
        query = """SELECT market_id, event_id, event_slug, symbol, strike, market_date, resolves_at,
            pyth_feed, yes_token_id, no_token_id, question, rules_hash, raw_payload_json
            FROM price_ladder_contracts"""
        conditions = []
        parameters: list[object] = []
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            conditions.append(f"symbol IN ({placeholders})")
            parameters.extend(symbol.upper() for symbol in symbols)
        if market_date:
            conditions.append("market_date = ?")
            parameters.append(market_date)
        if accepted_only:
            conditions.append("review_status = 'ACCEPTED'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY market_date, symbol, strike"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(PriceLadderContract(
            market_id=str(row[0]), event_id=str(row[1]), event_slug=str(row[2]), symbol=str(row[3]),
            strike=float(row[4]), market_date=str(row[5]), resolves_at=datetime.fromisoformat(str(row[6])),
            pyth_feed=str(row[7]), yes_token_id=str(row[8]), no_token_id=str(row[9]), question=str(row[10]),
            rules_hash=str(row[11]), raw_payload=json.loads(str(row[12])),
        ) for row in rows)

    def record_snapshot(
        self, contract: PriceLadderContract, *, observed_at: datetime, checkpoint_name: str | None,
        yes_bid: float | None, yes_ask: float | None, no_bid: float | None, no_ask: float | None,
        yes_bid_depth: float, yes_ask_depth: float, no_bid_depth: float, no_ask_depth: float,
        yes_book: Mapping[str, object], no_book: Mapping[str, object],
    ) -> bool:
        if observed_at.tzinfo is None:
            raise ValueError("ladder observed_at must be timezone-aware")
        with _database_connection(self.path) as connection:
            return connection.execute(
                """INSERT OR IGNORE INTO price_ladder_snapshots (
                    observed_at, observed_second, market_id, symbol, market_date, checkpoint_name, strike,
                    yes_bid, yes_ask, no_bid, no_ask, yes_bid_depth, yes_ask_depth, no_bid_depth, no_ask_depth,
                    yes_book_json, no_book_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(), observed_at.replace(microsecond=0).isoformat(), contract.market_id,
                    contract.symbol, contract.market_date, checkpoint_name, contract.strike,
                    yes_bid, yes_ask, no_bid, no_ask, yes_bid_depth, yes_ask_depth, no_bid_depth, no_ask_depth,
                    json.dumps(yes_book, sort_keys=True, separators=(",", ":"), default=str),
                    json.dumps(no_book, sort_keys=True, separators=(",", ":"), default=str),
                ),
            ).rowcount == 1

    def list_snapshots(
        self, *, market_date: str | None = None, checkpoint_only: bool = False,
    ) -> tuple[StoredLadderSnapshot, ...]:
        query = """SELECT observed_at, market_id, symbol, market_date, checkpoint_name, strike,
            yes_bid, yes_ask, no_bid, no_ask, yes_bid_depth, yes_ask_depth, no_bid_depth, no_ask_depth,
            yes_book_json, no_book_json FROM price_ladder_snapshots"""
        conditions = []
        parameters: list[object] = []
        if market_date:
            conditions.append("market_date = ?")
            parameters.append(market_date)
        if checkpoint_only:
            conditions.append("checkpoint_name IS NOT NULL")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY observed_at, symbol, strike"
        with _database_connection(self.path) as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(StoredLadderSnapshot(
            observed_at=datetime.fromisoformat(str(row[0])), market_id=str(row[1]), symbol=str(row[2]),
            market_date=str(row[3]), checkpoint_name=str(row[4]) if row[4] else None, strike=float(row[5]),
            yes_bid=_optional_float(row[6]), yes_ask=_optional_float(row[7]), no_bid=_optional_float(row[8]),
            no_ask=_optional_float(row[9]), yes_bid_depth=float(row[10]), yes_ask_depth=float(row[11]),
            no_bid_depth=float(row[12]), no_ask_depth=float(row[13]),
            yes_book=json.loads(str(row[14])), no_book=json.loads(str(row[15])),
        ) for row in rows)

    def latest_snapshot_rows(self, market_date: str) -> tuple[StoredLadderSnapshot, ...]:
        with _database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT observed_at, market_id, symbol, market_date, checkpoint_name, strike,
                    yes_bid, yes_ask, no_bid, no_ask, yes_bid_depth, yes_ask_depth, no_bid_depth, no_ask_depth,
                    yes_book_json, no_book_json FROM price_ladder_snapshots AS snapshot
                WHERE market_date = ? AND id IN (
                    SELECT MAX(id) FROM price_ladder_snapshots WHERE market_date = ? GROUP BY market_id
                ) ORDER BY symbol, strike""", (market_date, market_date)
            ).fetchall()
        return tuple(StoredLadderSnapshot(
            observed_at=datetime.fromisoformat(str(row[0])), market_id=str(row[1]), symbol=str(row[2]),
            market_date=str(row[3]), checkpoint_name=str(row[4]) if row[4] else None, strike=float(row[5]),
            yes_bid=_optional_float(row[6]), yes_ask=_optional_float(row[7]), no_bid=_optional_float(row[8]),
            no_ask=_optional_float(row[9]), yes_bid_depth=float(row[10]), yes_ask_depth=float(row[11]),
            no_bid_depth=float(row[12]), no_ask_depth=float(row[13]), yes_book=json.loads(str(row[14])),
            no_book=json.loads(str(row[15])),
        ) for row in rows)

    def record_settlement(self, market_id: str, winning_outcome: str, payload: Mapping[str, object], *, settled_at: datetime) -> None:
        outcome = winning_outcome.upper()
        if outcome not in {"YES", "NO"} or settled_at.tzinfo is None:
            raise ValueError("invalid ladder settlement")
        with _database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO price_ladder_settlements (market_id, settled_at, winning_outcome, raw_payload_json)
                VALUES (?, ?, ?, ?) ON CONFLICT(market_id) DO UPDATE SET settled_at=excluded.settled_at,
                    winning_outcome=excluded.winning_outcome, raw_payload_json=excluded.raw_payload_json""",
                (market_id, settled_at.isoformat(), outcome,
                 json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)),
            )


def _optional_float(value: object) -> float | None:
    return float(value) if value is not None else None
