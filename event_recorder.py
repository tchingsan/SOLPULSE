
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_utils import connect_sqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
CONFIG_PATH = BASE_DIR / "config.json"
LOCK_PATH = BASE_DIR / "data" / "event_recorder.lock"

running = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def connect_db() -> sqlite3.Connection:
    return connect_sqlite(DB_PATH, timeout_seconds=30)


def write_state(
    connection: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < 30:
            print("L'Event Recorder semble déjà fonctionner.")
            sys.exit(1)
        LOCK_PATH.unlink(missing_ok=True)

    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def latest_source_rows(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            launches.id AS launch_id,
            launches.detected_at,
            launches.mint,
            launches.symbol,
            launches.name AS token_name,
            launches.lifecycle_state,
            launches.progress_pct,
            CASE
                WHEN launches.market_mode='MIGRATED_DEX'
                THEN launches.market_price_sol
                ELSE launches.curve_price_sol
            END AS curve_price_sol,
            CASE
                WHEN launches.market_mode='MIGRATED_DEX'
                THEN launches.market_price_usd
                ELSE launches.curve_price_usd
            END AS curve_price_usd,
            launches.real_quote_reserves_sol,
            launches.is_mayhem_mode,
            launches.market_mode,
            launches.pair_address,
            launches.market_liquidity_usd AS liquidity_usd,
            launches.market_volume_5m_usd AS volume_5m_usd,
            launches.market_buys_5m AS buys_5m,
            launches.market_sells_5m AS sells_5m,
            launches.market_price_sol,

            safety.safety_score,
            safety.decision AS safety_decision,
            safety.hard_reject AS safety_hard_reject,
            safety.top1_pct,
            safety.top10_pct,
            safety.creator_launch_count,

            qualification.state AS qualification_state,
            qualification.qualification_score,
            qualification.observation_samples,
            qualification.stable_samples,
            qualification.progress_delta_pct,
            qualification.price_change_pct,

            position.id AS position_id,
            position.status AS position_status,
            position.current_value_sol AS position_value_sol,
            position.realized_pnl_sol

        FROM new_launches launches

        LEFT JOIN safety_assessments safety
            ON safety.mint = launches.mint

        LEFT JOIN qualification_candidates qualification
            ON qualification.mint = launches.mint

        LEFT JOIN positions position
            ON position.id = (
                SELECT MAX(position_inner.id)
                FROM positions position_inner
                WHERE position_inner.token_mint = launches.mint
            )

        ORDER BY datetime(launches.detected_at) DESC
        LIMIT 2000
        """
    ).fetchall()


def previous_sample(
    connection: sqlite3.Connection,
    mint: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM research_samples
        WHERE mint = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (mint,),
    ).fetchone()


def insert_event(
    connection: sqlite3.Connection,
    mint: str,
    symbol: str | None,
    event_type: str,
    previous_value: Any,
    new_value: Any,
    details: dict[str, Any],
    source: str,
) -> None:
    connection.execute(
        """
        INSERT INTO research_events (
            timestamp, mint, symbol,
            event_type, previous_value, new_value,
            details_json, source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            mint,
            symbol,
            event_type,
            None if previous_value is None else str(previous_value),
            None if new_value is None else str(new_value),
            json.dumps(details, ensure_ascii=False),
            source,
        ),
    )


def normalized(value: Any) -> Any:
    return None if value is None else value


def progress_milestone(value: Any) -> int | None:
    try:
        number = float(value)
        if number < 0:
            return None
        return int(number // 10) * 10
    except (TypeError, ValueError):
        return None


def record_change_events(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    previous: sqlite3.Row | None,
) -> None:
    mint = str(row["mint"])
    symbol = row["symbol"]

    if previous is None:
        insert_event(
            connection,
            mint,
            symbol,
            "LAUNCH_RECORDED",
            None,
            row["lifecycle_state"],
            {
                "detected_at": row["detected_at"],
                "token_name": row["token_name"],
            },
            "Event Recorder",
        )
        return

    fields = [
        (
            "lifecycle_state",
            "LIFECYCLE_CHANGED",
            "New Coin Radar",
        ),
        (
            "is_mayhem_mode",
            "MAYHEM_STATUS_CHANGED",
            "New Coin Radar",
        ),
        (
            "market_mode",
            "MARKET_MODE_CHANGED",
            "Hybrid Market Scanner",
        ),
        (
            "safety_decision",
            "SAFETY_DECISION_CHANGED",
            "Safety Recovery Engine",
        ),
        (
            "qualification_state",
            "QUALIFICATION_STATE_CHANGED",
            "Qualification Pipeline",
        ),
        (
            "position_status",
            "POSITION_STATUS_CHANGED",
            "Qualification Pipeline",
        ),
    ]

    for field, event_type, source in fields:
        old = normalized(previous[field])
        new = normalized(row[field])

        if old != new and (old is not None or new is not None):
            insert_event(
                connection,
                mint,
                symbol,
                event_type,
                old,
                new,
                {
                    "safety_score": row["safety_score"],
                    "qualification_score": row[
                        "qualification_score"
                    ],
                    "progress_pct": row["progress_pct"],
                    "position_id": row["position_id"],
                },
                source,
            )

    old_milestone = progress_milestone(
        previous["progress_pct"]
    )
    new_milestone = progress_milestone(
        row["progress_pct"]
    )
    if (
        old_milestone is not None
        and new_milestone is not None
        and new_milestone > old_milestone
    ):
        insert_event(
            connection,
            mint,
            symbol,
            "PROGRESS_MILESTONE",
            old_milestone,
            new_milestone,
            {
                "progress_pct": row["progress_pct"],
                "curve_price_sol": row["curve_price_sol"],
            },
            "Bonding Curve",
        )

    try:
        old_safety = float(previous["safety_score"])
        new_safety = float(row["safety_score"])
        if abs(new_safety - old_safety) >= 5:
            insert_event(
                connection,
                mint,
                symbol,
                "SAFETY_SCORE_MOVED",
                f"{old_safety:.1f}",
                f"{new_safety:.1f}",
                {
                    "delta": new_safety - old_safety,
                    "decision": row["safety_decision"],
                },
                "Safety Engine",
            )
    except (TypeError, ValueError):
        pass


def insert_sample(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    connection.execute(
        """
        INSERT INTO research_samples (
            timestamp, mint, symbol, token_name,
            detected_at, lifecycle_state,
            progress_pct, curve_price_sol, curve_price_usd,
            real_quote_reserves_sol,
            safety_score, safety_decision,
            safety_hard_reject, top1_pct, top10_pct,
            creator_launch_count,
            qualification_state, qualification_score,
            observation_samples, stable_samples,
            progress_delta_pct, price_change_pct,
            position_id, position_status,
            position_value_sol, realized_pnl_sol,
            is_mayhem_mode, market_mode, pair_address,
            liquidity_usd, volume_5m_usd,
            buys_5m, sells_5m, market_price_sol
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            row["mint"],
            row["symbol"],
            row["token_name"],
            row["detected_at"],
            row["lifecycle_state"],
            row["progress_pct"],
            row["curve_price_sol"],
            row["curve_price_usd"],
            row["real_quote_reserves_sol"],
            row["safety_score"],
            row["safety_decision"],
            row["safety_hard_reject"],
            row["top1_pct"],
            row["top10_pct"],
            row["creator_launch_count"],
            row["qualification_state"],
            row["qualification_score"],
            row["observation_samples"],
            row["stable_samples"],
            row["progress_delta_pct"],
            row["price_change_pct"],
            row["position_id"],
            row["position_status"],
            row["position_value_sol"],
            row["realized_pnl_sol"],
            row["is_mayhem_mode"],
            row["market_mode"],
            row["pair_address"],
            row["liquidity_usd"],
            row["volume_5m_usd"],
            row["buys_5m"],
            row["sells_5m"],
            row["market_price_sol"],
        ),
    )


def prune_samples(
    connection: sqlite3.Connection,
    retention: int,
) -> None:
    connection.execute(
        """
        DELETE FROM research_samples
        WHERE id NOT IN (
            SELECT id
            FROM research_samples
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (retention,),
    )


def record_cycle(
    retention: int,
) -> tuple[int, int]:
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = latest_source_rows(connection)
        events_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM research_events"
            ).fetchone()[0]
        )

        for row in rows:
            previous = previous_sample(
                connection,
                str(row["mint"]),
            )
            record_change_events(
                connection,
                row,
                previous,
            )
            insert_sample(connection, row)

        prune_samples(connection, retention)

        sample_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM research_samples"
            ).fetchone()[0]
        )
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM research_events"
            ).fetchone()[0]
        )

        write_state(connection, "recorder_status", "RUNNING")
        write_state(
            connection,
            "recorder_last_sample",
            now_iso(),
        )
        write_state(
            connection,
            "recorder_last_error",
            "",
        )
        write_state(
            connection,
            "recorder_sample_count",
            str(sample_count),
        )
        write_state(
            connection,
            "recorder_event_count",
            str(event_count),
        )
        connection.commit()

    return len(rows), event_count - events_before


def update_error_state(
    status: str,
    message: str,
) -> None:
    try:
        with connect_db() as connection:
            write_state(connection, "recorder_status", status)
            write_state(
                connection,
                "recorder_last_error",
                message[:500],
            )
            connection.commit()
    except Exception:
        pass


def main() -> None:
    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    config = load_json(CONFIG_PATH)
    if not config.get("recorder_enabled", True):
        print("L'Event Recorder est désactivé.")
        return

    interval = float(
        config.get("recorder_interval_seconds", 5)
    )
    retention = int(
        config.get("recorder_retention_samples", 150000)
    )

    acquire_lock()

    print("=" * 72)
    print("SOLPULSE V12.2 — PAPER PILOT EVENT RECORDER")
    print("=" * 72)
    print(f"Échantillonnage : toutes les {interval:g} secondes")
    print(f"Rétention : {retention:,} échantillons")
    print("Enregistrement local pour replay et backtest.")
    print()

    try:
        while running:
            started = time.monotonic()
            LOCK_PATH.touch(exist_ok=True)

            try:
                recorded, new_events = record_cycle(
                    retention,
                )
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} | "
                    f"{recorded} token(s) | "
                    f"{new_events} nouvel événement"
                )
            except KeyboardInterrupt:
                break
            except Exception as error:
                message = str(error)
                print(f"Erreur Event Recorder : {message}")
                update_error_state("ERROR", message)

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    finally:
        update_error_state("STOPPED", "")
        release_lock()
        print("Event Recorder arrêté proprement.")


if __name__ == "__main__":
    main()
