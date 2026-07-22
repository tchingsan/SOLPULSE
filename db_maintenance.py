
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from runtime_utils import connect_sqlite, now_iso, write_state

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
BACKUP_DIR = BASE_DIR / "backups"


def backup_database(*, force: bool = False) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    current_time = datetime.now(timezone.utc)
    date_tag = current_time.strftime("%Y-%m-%d")
    if force:
        file_tag = current_time.strftime("%Y-%m-%d-%H%M%S")
    else:
        file_tag = date_tag
    target = BACKUP_DIR / f"trading-{file_tag}.db"

    if (
        not force
        and target.exists()
        and target.stat().st_size > 0
    ):
        return target

    with connect_sqlite(DB_PATH) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            destination.commit()

    return target


def prune_backups(keep: int = 14) -> None:
    backups = sorted(
        BACKUP_DIR.glob("trading-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in backups[max(1, keep):]:
        path.unlink(missing_ok=True)


def perform_maintenance(
    *,
    create_backup: bool = True,
    force_backup: bool = False,
) -> dict[str, str]:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            "La base data/trading.db est absente."
        )

    with connect_sqlite(DB_PATH) as connection:
        integrity = str(
            connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        )
        if integrity != "ok":
            write_state(
                connection,
                "db_integrity",
                integrity,
            )
            connection.commit()
            raise RuntimeError(
                f"Échec du contrôle SQLite: {integrity}"
            )

        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        write_state(connection, "db_integrity", "OK")
        write_state(
            connection,
            "db_last_checkpoint",
            now_iso(),
        )
        connection.commit()

    backup_path = ""
    if create_backup:
        backup_path = str(backup_database(force=force_backup))
        prune_backups()

        with connect_sqlite(DB_PATH) as connection:
            write_state(
                connection,
                "db_last_backup",
                now_iso(),
            )
            write_state(
                connection,
                "db_last_backup_file",
                Path(backup_path).name,
            )
            connection.commit()

    return {
        "integrity": "OK",
        "backup": backup_path,
    }


if __name__ == "__main__":
    result = perform_maintenance(
        create_backup=True,
        force_backup=True,
    )
    print("=" * 68)
    print("SOLPULSE V12 — MAINTENANCE SQLITE")
    print("=" * 68)
    print(f"Intégrité : {result['integrity']}")
    print(f"Sauvegarde : {result['backup'] or 'déjà existante'}")
