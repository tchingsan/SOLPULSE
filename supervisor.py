
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from db_maintenance import perform_maintenance
from runtime_utils import connect_sqlite, now_iso, write_state

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
LOG_DIR = BASE_DIR / "logs"
LOCK_PATH = BASE_DIR / "data" / "supervisor.lock"

CHECK_SECONDS = 3.0
MAX_LOG_BYTES = 5_000_000
RESTART_WINDOW_SECONDS = 600
MAX_RESTARTS_IN_WINDOW = 5
COOLDOWN_SECONDS = 300

running = True


@dataclass
class Engine:
    key: str
    label: str
    script: str
    process: subprocess.Popen[str] | None = None
    log_handle: IO[str] | None = None
    restart_times: deque[float] = field(
        default_factory=deque
    )
    restart_count: int = 0
    next_start_at: float = 0.0
    last_exit_code: int | None = None
    last_reported_status: str = ""
    last_report_at: float = 0.0


ENGINES = [
    Engine("collector", "Watchlist Collector", "prebond_paper_bot.py"),
    Engine("radar", "New Coin Radar", "new_coin_radar.py"),
    Engine(
        "market",
        "Hybrid Market Scanner",
        "hybrid_market_scanner.py",
    ),
    Engine("safety", "Safety Recovery Engine", "safety_engine.py"),
    Engine("strategy", "Paper Pilot Engine", "qualification_pipeline.py"),
    Engine("recorder", "Event Recorder", "event_recorder.py"),
]


def stop_handler(*_: object) -> None:
    global running
    running = False


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < 30:
            print("Un superviseur SOLPULSE fonctionne déjà.")
            raise SystemExit(1)
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def rotate_log(path: Path) -> None:
    if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
        return

    oldest = path.with_suffix(path.suffix + ".3")
    oldest.unlink(missing_ok=True)

    for index in (2, 1):
        source = path.with_suffix(path.suffix + f".{index}")
        target = path.with_suffix(path.suffix + f".{index + 1}")
        if source.exists():
            source.replace(target)

    path.replace(path.with_suffix(path.suffix + ".1"))



def record_incident(
    engine: Engine,
    event_type: str,
    message: str,
) -> None:
    try:
        with connect_sqlite(DB_PATH) as connection:
            connection.execute(
                """
                INSERT INTO engine_incidents (
                    timestamp, engine_key, engine_label,
                    event_type, exit_code, restart_count,
                    message, log_file
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    engine.key,
                    engine.label,
                    event_type,
                    engine.last_exit_code,
                    engine.restart_count,
                    message[:1000],
                    f"logs/{engine.key}.log",
                ),
            )
            connection.commit()
    except Exception as error:
        print(
            f"Impossible d'enregistrer l'incident "
            f"{engine.key}: {error}"
        )


def update_engine_state(
    engine: Engine,
    status: str,
    *,
    message: str = "",
) -> None:
    current_time = time.time()
    if (
        status == engine.last_reported_status
        and not message
        and current_time - engine.last_report_at < 15
    ):
        return

    try:
        with connect_sqlite(DB_PATH) as connection:
            write_state(
                connection,
                f"supervisor_{engine.key}_status",
                status,
            )
            write_state(
                connection,
                f"supervisor_{engine.key}_pid",
                (
                    engine.process.pid
                    if engine.process
                    and engine.process.poll() is None
                    else ""
                ),
            )
            write_state(
                connection,
                f"supervisor_{engine.key}_restarts",
                engine.restart_count,
            )
            write_state(
                connection,
                f"supervisor_{engine.key}_last_exit",
                (
                    engine.last_exit_code
                    if engine.last_exit_code is not None
                    else ""
                ),
            )
            if message:
                write_state(
                    connection,
                    f"supervisor_{engine.key}_message",
                    message[:500],
                )
            connection.commit()
        engine.last_reported_status = status
        engine.last_report_at = current_time
    except Exception as error:
        print(
            f"Impossible d'écrire l'état de {engine.key}: {error}"
        )


def start_engine(engine: Engine) -> None:
    script_path = BASE_DIR / engine.script
    if not script_path.exists():
        update_engine_state(
            engine,
            "MISSING",
            message=f"Script absent: {engine.script}",
        )
        engine.next_start_at = time.time() + COOLDOWN_SECONDS
        return

    lock_file = BASE_DIR / "data" / {
        "collector": "prebond_bot.lock",
        "radar": "new_coin_radar.lock",
        "market": "hybrid_market_scanner.lock",
        "safety": "safety_engine.lock",
        "strategy": "qualification_pipeline.lock",
        "recorder": "event_recorder.lock",
    }[engine.key]
    lock_file.unlink(missing_ok=True)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{engine.key}.log"
    rotate_log(log_path)
    engine.log_handle = log_path.open(
        "a",
        encoding="utf-8",
        buffering=1,
    )
    engine.log_handle.write(
        "\n"
        + "=" * 72
        + "\n"
        + f"{now_iso()} — démarrage {engine.label}\n"
        + "=" * 72
        + "\n"
    )

    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW

    engine.process = subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
        cwd=BASE_DIR,
        stdout=engine.log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    engine.restart_count += 1
    now = time.time()
    engine.restart_times.append(now)
    engine.next_start_at = 0.0
    update_engine_state(engine, "RUNNING")
    record_incident(
        engine,
        "STARTED",
        f"Processus démarré avec PID {engine.process.pid}",
    )
    print(
        f"{datetime.now().strftime('%H:%M:%S')} | "
        f"{engine.label} démarré (PID {engine.process.pid})"
    )


def stop_engine(engine: Engine) -> None:
    process = engine.process
    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=8)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    if engine.log_handle:
        try:
            engine.log_handle.close()
        except Exception:
            pass
        engine.log_handle = None

    engine.process = None
    update_engine_state(engine, "STOPPED")


def check_engine(engine: Engine) -> None:
    now = time.time()

    while (
        engine.restart_times
        and now - engine.restart_times[0]
        > RESTART_WINDOW_SECONDS
    ):
        engine.restart_times.popleft()

    if engine.process is None:
        if now >= engine.next_start_at:
            start_engine(engine)
        return

    exit_code = engine.process.poll()
    if exit_code is None:
        update_engine_state(engine, "RUNNING")
        return

    engine.last_exit_code = int(exit_code)
    if engine.log_handle:
        try:
            engine.log_handle.write(
                f"{now_iso()} — processus terminé avec code {exit_code}\n"
            )
            engine.log_handle.close()
        except Exception:
            pass
        engine.log_handle = None
    engine.process = None

    if len(engine.restart_times) >= MAX_RESTARTS_IN_WINDOW:
        engine.next_start_at = now + COOLDOWN_SECONDS
        cooldown_message = (
            f"{len(engine.restart_times)} arrêts en moins de "
            f"{RESTART_WINDOW_SECONDS // 60} minutes; "
            f"nouvel essai dans {COOLDOWN_SECONDS // 60} minutes"
        )
        update_engine_state(
            engine,
            "COOLDOWN",
            message=cooldown_message,
        )
        record_incident(
            engine,
            "COOLDOWN",
            cooldown_message,
        )
        print(
            f"{engine.label}: redémarrages trop fréquents, "
            "mise en pause."
        )
    else:
        delay = min(3 * max(1, len(engine.restart_times)), 30)
        engine.next_start_at = now + delay
        restart_message = (
            f"Code de sortie {exit_code}; "
            f"nouvel essai dans {delay} secondes"
        )
        update_engine_state(
            engine,
            "RESTARTING",
            message=restart_message,
        )
        record_incident(
            engine,
            "CRASH_RESTART",
            restart_message,
        )
        print(
            f"{engine.label} arrêté (code {exit_code}), "
            f"redémarrage dans {delay} s."
        )


def supervisor_heartbeat() -> None:
    with connect_sqlite(DB_PATH) as connection:
        write_state(connection, "supervisor_status", "RUNNING")
        write_state(
            connection,
            "supervisor_last_tick",
            now_iso(),
        )
        write_state(
            connection,
            "supervisor_engine_count",
            len(ENGINES),
        )
        connection.commit()


def main() -> None:
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    acquire_lock()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("SOLPULSE V12.2 — SUPERVISEUR STABLE")
    print("=" * 72)
    print("Surveillance de six moteurs, redémarrage automatique et logs persistants.")
    print(f"Logs : {LOG_DIR}")
    print()

    try:
        result = perform_maintenance(create_backup=True)
        print(f"SQLite : {result['integrity']}")
        print(f"Sauvegarde : {result['backup']}")
    except Exception as error:
        print(f"Maintenance de démarrage échouée : {error}")
        raise

    last_maintenance = time.time()

    try:
        for engine in ENGINES:
            start_engine(engine)
            time.sleep(0.5)

        while running:
            LOCK_PATH.touch(exist_ok=True)
            supervisor_heartbeat()

            for engine in ENGINES:
                check_engine(engine)

            if time.time() - last_maintenance >= 21_600:
                try:
                    perform_maintenance(create_backup=True)
                except Exception as error:
                    print(f"Maintenance périodique: {error}")
                last_maintenance = time.time()

            time.sleep(CHECK_SECONDS)
    finally:
        print("Arrêt des moteurs...")
        for engine in ENGINES:
            stop_engine(engine)

        try:
            with connect_sqlite(DB_PATH) as connection:
                write_state(
                    connection,
                    "supervisor_status",
                    "STOPPED",
                )
                write_state(
                    connection,
                    "supervisor_last_tick",
                    now_iso(),
                )
                connection.commit()
        except Exception:
            pass

        LOCK_PATH.unlink(missing_ok=True)
        print("Superviseur arrêté proprement.")


if __name__ == "__main__":
    main()
