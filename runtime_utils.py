
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")



class ClosingSQLiteConnection(sqlite3.Connection):
    """SQLite connection that really closes after a ``with`` block.

    The standard sqlite3 context manager commits or rolls back but does not
    close the file handle. On Windows this can leave trading.db locked and
    break temporary tests, imports, backups, or cleanup operations.
    """

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool:
        try:
            result = super().__exit__(
                exc_type,
                exc_value,
                traceback,
            )
            return bool(result)
        finally:
            self.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_sqlite(
    db_path: str | Path,
    *,
    timeout_seconds: float = 30.0,
    retries: int = 6,
) -> sqlite3.Connection:
    """Open SQLite with WAL and bounded retries for transient locks."""
    path = Path(db_path)
    last_error: Exception | None = None

    for attempt in range(max(1, retries)):
        try:
            connection = sqlite3.connect(
                path,
                timeout=timeout_seconds,
                factory=ClosingSQLiteConnection,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}"
            )
            current_mode = str(
                connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
            ).lower()
            if current_mode != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.OperationalError as error:
            last_error = error
            message = str(error).lower()
            transient = (
                "locked" in message
                or "busy" in message
                or "temporarily" in message
            )
            if not transient or attempt + 1 >= retries:
                raise
            time.sleep(min(0.25 * (2**attempt), 4.0))

    raise RuntimeError(
        f"Impossible d'ouvrir SQLite: {last_error}"
    )


def run_with_sqlite_retry(
    operation: Callable[[], T],
    *,
    retries: int = 6,
) -> T:
    last_error: Exception | None = None

    for attempt in range(max(1, retries)):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            last_error = error
            message = str(error).lower()
            transient = "locked" in message or "busy" in message
            if not transient or attempt + 1 >= retries:
                raise
            time.sleep(min(0.2 * (2**attempt), 3.0))

    raise RuntimeError(f"Échec SQLite: {last_error}")


def write_state(
    connection: sqlite3.Connection,
    key: str,
    value: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, str(value), now_iso()),
    )
