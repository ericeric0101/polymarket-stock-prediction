"""SQLite connection and transaction policy shared by local journals."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time


DATABASE_BUSY_TIMEOUT_MS = 5_000
DATABASE_COMMIT_RETRY_SECONDS = (0.05, 0.15, 0.45)


def is_database_locked(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "database is locked" in message or "database is busy" in message


@contextmanager
def database_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open one transactional SQLite connection with bounded lock retries."""

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
                if not is_database_locked(error) or attempt == len(DATABASE_COMMIT_RETRY_SECONDS):
                    raise
    finally:
        connection.close()
