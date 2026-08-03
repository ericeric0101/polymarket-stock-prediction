"""Schema initialization and backwards-compatible SQLite migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..checkpoints import DEFAULT_MAXIMUM_DELAY_SECONDS, checkpoint_target_at
from .schema import CORE_SCHEMA
from .sqlite import database_connection


def initialize_database(path: Path) -> None:
    """Create the journal schema and apply idempotent legacy migrations."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with database_connection(path) as connection:
        current_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if current_mode != "wal":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(CORE_SCHEMA)
        _migrate_market_candidate_columns(connection)
        _migrate_paper_position_columns(connection)
        _migrate_checkpoint_observation_columns(connection)
        _exclude_precontract_day_paper_positions(connection)


def _migrate_checkpoint_observation_columns(connection: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(checkpoint_observations)")}
    if "checkpoint_target_at" not in columns:
        connection.execute("ALTER TABLE checkpoint_observations ADD COLUMN checkpoint_target_at TEXT")
    if "checkpoint_delay_seconds" not in columns:
        connection.execute("ALTER TABLE checkpoint_observations ADD COLUMN checkpoint_delay_seconds REAL")
    if "eligible_for_calibration" not in columns:
        connection.execute(
            "ALTER TABLE checkpoint_observations ADD COLUMN eligible_for_calibration INTEGER NOT NULL DEFAULT 1"
        )
    rows = connection.execute(
        """SELECT id, checkpoint_date, checkpoint_name, evaluated_at
        FROM checkpoint_observations WHERE checkpoint_target_at IS NULL OR checkpoint_delay_seconds IS NULL"""
    ).fetchall()
    for row in rows:
        target_at = checkpoint_target_at(str(row["checkpoint_date"]), str(row["checkpoint_name"]))
        evaluated_at = datetime.fromisoformat(str(row["evaluated_at"]))
        delay_seconds = max(0.0, (evaluated_at - target_at).total_seconds())
        connection.execute(
            """UPDATE checkpoint_observations
            SET checkpoint_target_at = ?, checkpoint_delay_seconds = ?, eligible_for_calibration = ?
            WHERE id = ?""",
            (
                target_at.isoformat(),
                delay_seconds,
                int(delay_seconds <= DEFAULT_MAXIMUM_DELAY_SECONDS),
                int(row["id"]),
            ),
        )


def _migrate_market_candidate_columns(connection: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(market_candidates)")}
    required_columns = {
        "outcome_a_label": "TEXT NOT NULL DEFAULT ''",
        "outcome_b_label": "TEXT NOT NULL DEFAULT ''",
        "outcome_a_token_id": "TEXT NOT NULL DEFAULT ''",
        "outcome_b_token_id": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in required_columns.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE market_candidates ADD COLUMN {column} {definition}")


def _migrate_paper_position_columns(connection: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(paper_positions)")}
    required_columns = {
        "included_in_calibration": "INTEGER NOT NULL DEFAULT 1",
        "exclusion_reason": "TEXT",
    }
    for column, definition in required_columns.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE paper_positions ADD COLUMN {column} {definition}")


def _exclude_precontract_day_paper_positions(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """SELECT position.position_id, position.opened_at, candidate.end_date
        FROM paper_positions AS position
        JOIN market_candidates AS candidate ON candidate.market_id = position.market_id
        WHERE position.included_in_calibration = 1"""
    ).fetchall()
    new_york = ZoneInfo("America/New_York")
    for row in rows:
        try:
            opened_day = datetime.fromisoformat(str(row["opened_at"])).astimezone(new_york).date()
            contract_day = (
                datetime.fromisoformat(str(row["end_date"]).replace("Z", "+00:00")).astimezone(new_york).date()
            )
        except ValueError:
            continue
        if opened_day < contract_day:
            connection.execute(
                """UPDATE paper_positions SET included_in_calibration = 0,
                exclusion_reason = 'PRECONTRACT_TRADE_DATE' WHERE position_id = ?""",
                (row["position_id"],),
            )
