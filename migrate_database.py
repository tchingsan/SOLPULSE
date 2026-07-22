
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS position_risk_state (
    position_id INTEGER PRIMARY KEY,
    peak_pnl_pct REAL NOT NULL DEFAULT 0,
    break_even_armed INTEGER NOT NULL DEFAULT 0,
    active_stop_pct REAL NOT NULL DEFAULT -20,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS engine_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    engine_key TEXT NOT NULL,
    engine_label TEXT NOT NULL,
    event_type TEXT NOT NULL,
    exit_code INTEGER,
    restart_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    log_file TEXT
);

CREATE INDEX IF NOT EXISTS idx_engine_incidents_time
ON engine_incidents(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_engine_incidents_engine
ON engine_incidents(engine_key, timestamp DESC);
"""


COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "new_launches": [
        ("is_mayhem_mode", "INTEGER"),
        ("mayhem_checked_at", "TEXT"),
        ("exclusion_reason", "TEXT"),
        ("mayhem_source", "TEXT"),
        ("mayhem_conflict", "INTEGER NOT NULL DEFAULT 0"),
        ("create_event_version", "TEXT"),
        ("event_checked_at", "TEXT"),
        ("event_detection_latency_ms", "REAL"),
        ("event_token_program", "TEXT"),
        ("event_token_total_supply_raw", "TEXT"),
        ("event_virtual_token_reserves_raw", "TEXT"),
        ("event_real_token_reserves_raw", "TEXT"),
        ("curve_confirmed", "INTEGER NOT NULL DEFAULT 0"),
        ("curve_confirmed_at", "TEXT"),
        ("event_is_cashback_enabled", "INTEGER"),
        ("event_quote_mint", "TEXT"),
        ("event_virtual_quote_reserves_raw", "TEXT"),
        ("market_mode", "TEXT NOT NULL DEFAULT 'BONDING'"),
        ("migrated_at", "TEXT"),
        ("market_last_updated_at", "TEXT"),
        ("pair_address", "TEXT"),
        ("dex_id", "TEXT"),
        ("pair_url", "TEXT"),
        ("pool_base_token_account", "TEXT"),
        ("pool_quote_token_account", "TEXT"),
        ("market_price_sol", "REAL"),
        ("market_price_usd", "REAL"),
        ("market_change_5m_pct", "REAL"),
        ("market_change_h1_pct", "REAL"),
        ("market_liquidity_usd", "REAL"),
        ("market_volume_5m_usd", "REAL"),
        ("market_volume_h1_usd", "REAL"),
        ("market_buys_5m", "INTEGER"),
        ("market_sells_5m", "INTEGER"),
        ("market_cap_usd", "REAL"),
        ("market_fdv_usd", "REAL"),
        ("pair_created_at", "INTEGER"),
        ("market_data_status", "TEXT"),
    ],
    "safety_assessments": [
        ("is_mayhem_mode", "INTEGER"),
        ("market_mode", "TEXT"),
        ("pair_address", "TEXT"),
        ("ignored_pool_token_account", "TEXT"),
        (
            "holder_analysis_status",
            "TEXT NOT NULL DEFAULT 'PENDING'",
        ),
        ("concentration_source", "TEXT"),
        (
            "provisional_score",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        ("rpc_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_success_at", "TEXT"),
        ("last_holder_success_at", "TEXT"),
        ("next_retry_at", "TEXT"),
        (
            "assessment_version",
            "TEXT NOT NULL DEFAULT 'V11'",
        ),
    ],
    "qualification_candidates": [
        ("is_mayhem_mode", "INTEGER"),
        ("entry_mode", "TEXT NOT NULL DEFAULT 'STRICT'"),
        ("market_mode", "TEXT NOT NULL DEFAULT 'BONDING'"),
        ("pair_address", "TEXT"),
        ("liquidity_usd", "REAL"),
        ("volume_5m_usd", "REAL"),
        ("buys_5m", "INTEGER"),
        ("sells_5m", "INTEGER"),
        ("market_price_sol", "REAL"),
        ("market_data_at", "TEXT"),
    ],
    "research_samples": [
        ("is_mayhem_mode", "INTEGER"),
        ("market_mode", "TEXT"),
        ("pair_address", "TEXT"),
        ("liquidity_usd", "REAL"),
        ("volume_5m_usd", "REAL"),
        ("buys_5m", "INTEGER"),
        ("sells_5m", "INTEGER"),
        ("market_price_sol", "REAL"),
    ],
}


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    return connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table_name,),
    ).fetchone() is not None


def column_names(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        )
    }


def add_missing_columns(
    connection: sqlite3.Connection,
) -> list[str]:
    added: list[str] = []

    for table_name, definitions in COLUMN_MIGRATIONS.items():
        if not table_exists(connection, table_name):
            continue

        existing = column_names(connection, table_name)
        for column_name, column_type in definitions:
            if column_name in existing:
                continue
            connection.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_type}"
            )
            added.append(f"{table_name}.{column_name}")
            existing.add(column_name)

    return added


def write_state(
    connection: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_state(key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value),
    )


def is_v12_2(connection: sqlite3.Connection) -> bool:
    if not table_exists(connection, "bot_state"):
        return False

    version_row = connection.execute(
        """
        SELECT value
        FROM bot_state
        WHERE key='strategy_version'
        """
    ).fetchone()
    hotfix_row = connection.execute(
        """
        SELECT value
        FROM bot_state
        WHERE key='hotfix_release'
        """
    ).fetchone()
    if not version_row or not hotfix_row:
        return False

    required_launch_columns = {
        "market_mode",
        "pair_address",
        "market_price_sol",
        "is_mayhem_mode",
        "mayhem_checked_at",
        "exclusion_reason",
        "mayhem_source",
        "mayhem_conflict",
        "create_event_version",
        "event_token_program",
        "event_virtual_token_reserves_raw",
        "event_real_token_reserves_raw",
        "curve_confirmed",
        "curve_confirmed_at",
    }
    required_safety_columns = {"is_mayhem_mode"}
    required_candidate_columns = {"is_mayhem_mode", "entry_mode"}
    required_replay_columns = {"is_mayhem_mode"}

    launch_columns = (
        column_names(connection, "new_launches")
        if table_exists(connection, "new_launches")
        else set()
    )
    safety_columns = (
        column_names(connection, "safety_assessments")
        if table_exists(connection, "safety_assessments")
        else set()
    )
    candidate_columns = (
        column_names(connection, "qualification_candidates")
        if table_exists(connection, "qualification_candidates")
        else set()
    )
    replay_columns = (
        column_names(connection, "research_samples")
        if table_exists(connection, "research_samples")
        else set()
    )

    return (
        version_row[0] == "stable_paper_pilot_v12"
        and hotfix_row[0] == "12.2"
        and required_launch_columns.issubset(launch_columns)
        and required_safety_columns.issubset(safety_columns)
        and required_candidate_columns.issubset(candidate_columns)
        and required_replay_columns.issubset(replay_columns)
    )


def main() -> None:
    if not DB_PATH.exists():
        print("Aucune base existante à migrer.")
        return

    with closing(sqlite3.connect(DB_PATH, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        if is_v12_2(connection):
            print("Base déjà compatible avec SOLPULSE V12.2.")
            return
        connection.execute("PRAGMA wal_checkpoint(FULL)")
        connection.commit()

    backup = DB_PATH.with_name(
        "trading-before-v12-"
        + datetime.now().strftime("%Y%m%d-%H%M%S")
        + ".db"
    )
    shutil.copy2(DB_PATH, backup)

    with closing(sqlite3.connect(DB_PATH, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(CREATE_SQL)
        added = add_missing_columns(connection)

        if table_exists(connection, "new_launches"):
            if table_exists(connection, "bonding_snapshots"):
                connection.execute(
                    """
                    UPDATE new_launches
                    SET is_mayhem_mode = COALESCE(
                            is_mayhem_mode,
                            (
                                SELECT snapshot.is_mayhem_mode
                                FROM bonding_snapshots snapshot
                                WHERE snapshot.token_mint = new_launches.mint
                                  AND snapshot.is_mayhem_mode IS NOT NULL
                                ORDER BY snapshot.id DESC
                                LIMIT 1
                            )
                        ),
                        mayhem_checked_at = COALESCE(
                            mayhem_checked_at,
                            (
                                SELECT snapshot.timestamp
                                FROM bonding_snapshots snapshot
                                WHERE snapshot.token_mint = new_launches.mint
                                  AND snapshot.is_mayhem_mode IS NOT NULL
                                ORDER BY snapshot.id DESC
                                LIMIT 1
                            )
                        )
                    """
                )

            connection.execute(
                """
                UPDATE new_launches
                SET exclusion_reason = CASE
                    WHEN is_mayhem_mode = 1
                    THEN 'MAYHEM_MODE_EXCLUDED'
                    ELSE exclusion_reason
                END
                """
            )

            connection.execute(
                """
                UPDATE new_launches
                SET market_mode = CASE
                    WHEN lifecycle_state IN (
                        'MIGRATED', 'DEX_ACTIVE', 'BONDED'
                    ) THEN 'MIGRATED_DEX'
                    ELSE COALESCE(market_mode, 'BONDING')
                END
                """
            )

        if table_exists(connection, "positions"):
            connection.execute(
                """
                INSERT OR IGNORE INTO position_risk_state (
                    position_id, peak_pnl_pct,
                    break_even_armed, active_stop_pct,
                    updated_at
                )
                SELECT
                    id,
                    CASE
                        WHEN entry_sol + COALESCE(entry_fees_sol, 0) > 0
                        THEN (
                            COALESCE(current_value_sol, entry_sol)
                            - entry_sol
                            - COALESCE(entry_fees_sol, 0)
                        ) / (
                            entry_sol
                            + COALESCE(entry_fees_sol, 0)
                        ) * 100.0
                        ELSE 0
                    END,
                    0,
                    -20,
                    datetime('now')
                FROM positions
                WHERE status='OPEN'
                """
            )

        if (
            table_exists(connection, "safety_assessments")
            and table_exists(connection, "new_launches")
        ):
            connection.execute(
                """
                UPDATE safety_assessments
                SET is_mayhem_mode = (
                        SELECT launches.is_mayhem_mode
                        FROM new_launches launches
                        WHERE launches.mint = safety_assessments.mint
                    ),
                    safety_score = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint = safety_assessments.mint
                        ) = 1
                        THEN 0
                        ELSE safety_score
                    END,
                    decision = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint = safety_assessments.mint
                        ) = 1
                        THEN 'REJECTED'
                        ELSE decision
                    END,
                    hard_reject = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint = safety_assessments.mint
                        ) = 1
                        THEN 1
                        ELSE hard_reject
                    END,
                    error_text = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint = safety_assessments.mint
                        ) = 1
                        THEN 'MAYHEM_MODE_EXCLUDED'
                        ELSE error_text
                    END
                WHERE mint IN (
                    SELECT mint
                    FROM new_launches
                    WHERE is_mayhem_mode = 1
                )
                """
            )

        if table_exists(
            connection,
            "qualification_candidates",
        ):
            connection.execute(
                """
                UPDATE qualification_candidates
                SET entry_mode=COALESCE(entry_mode, 'STRICT')
                """
            )

        if (
            table_exists(connection, "qualification_candidates")
            and table_exists(connection, "new_launches")
        ):
            connection.execute(
                """
                UPDATE qualification_candidates
                SET is_mayhem_mode = (
                        SELECT launches.is_mayhem_mode
                        FROM new_launches launches
                        WHERE launches.mint =
                            qualification_candidates.mint
                    ),
                    state = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint =
                                qualification_candidates.mint
                        ) = 1
                        THEN 'REJECTED'
                        ELSE state
                    END,
                    reason = CASE
                        WHEN (
                            SELECT launches.is_mayhem_mode
                            FROM new_launches launches
                            WHERE launches.mint =
                                qualification_candidates.mint
                        ) = 1
                        THEN 'MAYHEM MODE — exclu définitivement'
                        ELSE reason
                    END
                WHERE mint IN (
                    SELECT mint
                    FROM new_launches
                    WHERE is_mayhem_mode = 1
                )
                """
            )

        if table_exists(connection, "bot_state"):
            states = {
                "strategy_version": "stable_paper_pilot_v12",
                "strategy_rules": (
                    "bonding + migrated DEX; holder max 3.5% "
                    "total supply; pool ignored; 1 position; "
                    "0.05 SOL; SL -20%; TP +100%; BE after +50%"
                ),
                "diagnostics_release": "12.2",
                "hybrid_release": "11.0",
                "entry_ranking_release": "11.1",
                "entry_ranking_status": "READY",
                "mayhem_filter_release": "11.2",
                "fast_safety_release": "11.3",
                "rate_limit_recovery_release": "11.3.1",
                "acquisition_mode_release": "11.4",
                "paper_pilot_release": "12.0",
                "paper_pilot_status": "ENABLED",
                "paper_pilot_ready_count": "0",
                "paper_pilot_entries_count": "0",
                "paper_pilot_last_entry": "",
                "paper_pilot_last_exit": "",
                "startup_self_test": "PENDING",
                "startup_self_test_details": "",
                "windows_sqlite_handle_fix": "ENABLED",
                "hotfix_release": "12.2",
                "startup_smoke_test_required": "DISABLED",
                "startup_diagnostics_non_blocking": "ENABLED",
                "ui_release": "12.2",
                "acquisition_mode_status": "ENABLED",
                "acquisition_mode_entries_count": "0",
                "acquisition_mode_last_entry": "",
                "event_mayhem_immediate_count": "0",
                "event_mayhem_conflict_count": "0",
                "hybrid_market_cooldown_until": "",
                "radar_rpc_status": "WAITING",
                "radar_rpc_cooldown_seconds": "0",
                "safety_oldest_pending_age_seconds": "0",
                "safety_starved_count": "0",
                "safety_full_parallel_workers": "6",
                "mayhem_filter_status": "ENABLED",
                "mayhem_detected_count": "0",
                "mayhem_excluded_count": "0",
                "mayhem_unknown_count": "0",
                "hybrid_market_status": "STOPPED",
                "hybrid_market_last_scan": "",
                "hybrid_market_last_error": "",
                "hybrid_market_pairs_found": "0",
                "hybrid_market_migrated_count": "0",
                "safety_complete_count": "0",
                "safety_partial_count": "0",
                "safety_error_count": "0",
                "safety_provisional_count": "0",
                "safety_queue_pending": "0",
                "safety_last_success": "",
                "safety_rpc_rate_limited": "0",
                "supervisor_market_status": "STOPPED",
                "supervisor_market_pid": "",
                "supervisor_market_restarts": "0",
                "supervisor_market_last_exit": "",
                "supervisor_market_message": "",
                "supervisor_status": "STOPPED",
            }
            for key, value in states.items():
                write_state(connection, key, value)

        connection.commit()
        integrity = str(
            connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        )

    print("=" * 72)
    print("MIGRATION SOLPULSE V12.2")
    print("=" * 72)
    print(f"Base migrée : {DB_PATH}")
    print(f"Sauvegarde : {backup.name}")
    print(f"Colonnes ajoutées : {len(added)}")
    print(f"Intégrité : {integrity}")


if __name__ == "__main__":
    main()
