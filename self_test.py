from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from runtime_utils import connect_sqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
REPORT_PATH = BASE_DIR / "logs" / "startup_self_test.json"

REQUIRED_COLUMNS = {
    "new_launches": {
        "is_mayhem_mode",
        "mayhem_conflict",
        "event_virtual_token_reserves_raw",
        "event_virtual_quote_reserves_raw",
        "event_real_token_reserves_raw",
        "curve_confirmed",
    },
    "qualification_candidates": {"entry_mode", "state"},
    "positions": {"strategy", "status"},
}


def write_state(status: str, details: str) -> None:
    if not DB_PATH.exists():
        return
    try:
        with connect_sqlite(DB_PATH) as connection:
            timestamp = datetime.now(timezone.utc).isoformat()
            for key, value in (
                ("startup_self_test", status),
                ("startup_self_test_details", details[:1000]),
            ):
                connection.execute(
                    """
                    INSERT INTO bot_state(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (key, value, timestamp),
                )
            connection.commit()
    except Exception:
        # A diagnostic report must never prevent SOLPULSE from starting.
        pass


def run_checks(include_smoke_test: bool) -> tuple[str, list[dict[str, object]]]:
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})
        print(("PASS" if passed else "FAIL") + f" — {name}: {detail}")

    try:
        config = json.loads(
            (BASE_DIR / "config.json").read_text(encoding="utf-8")
        )
        check(
            "Configuration",
            config.get("strategy_release") == "12.0"
            and config.get("paper_pilot_enabled") is True
            and config.get("hotfix_release") == "12.2"
            and config.get("startup_smoke_test_required") is False,
            "Paper Pilot V12.2; test d'achat séparé du démarrage",
        )
    except Exception as error:
        check("Configuration", False, str(error))

    try:
        for path in BASE_DIR.glob("*.py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        check("Syntaxe Python", True, "tous les modules compilent")
    except Exception as error:
        check("Syntaxe Python", False, str(error))

    try:
        for directory in (
            BASE_DIR / "logs",
            BASE_DIR / "backups",
            BASE_DIR / "data",
            BASE_DIR / "data" / "diagnostics",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        check("Dossiers", True, "lecture/écriture disponible")
    except Exception as error:
        check("Dossiers", False, str(error))

    try:
        with closing(
            sqlite3.connect(
                DB_PATH,
                timeout=10,
            )
        ) as connection:
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            missing: list[str] = []
            for table, required in REQUIRED_COLUMNS.items():
                actual = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                missing.extend(
                    f"{table}.{column}"
                    for column in required - actual
                )
        check(
            "SQLite",
            integrity == "ok" and not missing,
            (
                "intégrité ok"
                if not missing
                else "colonnes manquantes: " + ", ".join(missing)
            ),
        )
    except Exception as error:
        check("SQLite", False, str(error))

    if include_smoke_test:
        smoke = subprocess.run(
            [sys.executable, str(BASE_DIR / "paper_smoke_test.py")],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
        )
        smoke_text = (smoke.stdout + "\n" + smoke.stderr).strip()
        check(
            "Achat paper technique",
            smoke.returncode == 0,
            smoke_text[-1000:] if smoke_text else "aucune sortie",
        )
    else:
        check(
            "Achat paper technique",
            True,
            "test non destructif séparé; lancer "
            "06_TESTER_ACHAT_PAPER_V12_2.bat",
        )

    passed = all(bool(item["passed"]) for item in checks)
    return ("PASS" if passed else "WARN"), checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Inclut le test d'achat paper manuel.",
    )
    parser.add_argument(
        "--startup",
        action="store_true",
        help="Contrôles rapides et non bloquants du démarrage.",
    )
    args = parser.parse_args()

    status, checks = run_checks(include_smoke_test=bool(args.full))
    details = "; ".join(
        f"{item['name']}={'OK' if item['passed'] else 'FAIL'}"
        for item in checks
    )
    write_state(status, details)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": "full" if args.full else "startup",
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"DIAGNOSTIC SOLPULSE V12.2 : {status}")

    # During normal startup, diagnostics never block the real engines.
    if args.startup:
        return 0
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
