from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

BASE_DIR = Path(__file__).resolve().parent
DIAGNOSTIC_DIR = BASE_DIR / "data" / "diagnostics"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Import impossible : {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    diagnostic_db = (
        DIAGNOSTIC_DIR
        / f"paper-smoke-{stamp}-{os.getpid()}.db"
    )

    # The diagnostic database is deliberately kept on disk. On Windows,
    # deleting a SQLite file immediately after imported modules have used it
    # can raise WinError 32 even though the trading assertions passed.
    reset = load_module(
        f"solpulse_v122_smoke_reset_{os.getpid()}",
        BASE_DIR / "reset_simulation.py",
    )
    reset.DB_PATH = diagnostic_db
    reset.main()

    strategy = load_module(
        f"solpulse_v122_smoke_strategy_{os.getpid()}",
        BASE_DIR / "qualification_pipeline.py",
    )
    strategy.DB_PATH = diagnostic_db

    config = json.loads(
        (BASE_DIR / "config.json").read_text(encoding="utf-8")
    )
    config["paper_pilot_delay_seconds"] = 0

    now = datetime.now(timezone.utc)
    detected = (now - timedelta(seconds=30)).isoformat()
    mint = "11111111111111111111111111111111"
    mayhem_mint = "22222222222222222222222222222222"

    with closing(
        sqlite3.connect(
            diagnostic_db,
            timeout=10,
        )
    ) as connection:
        for index, (token_mint, symbol, mayhem) in enumerate(
            ((mint, "SMOKE", 0), (mayhem_mint, "MAYHEM", 1))
        ):
            connection.execute(
                """
                INSERT INTO new_launches (
                    detected_at, last_updated_at, slot,
                    signature, event_index, mint,
                    name, symbol, uri, bonding_curve,
                    creator, lifecycle_state, progress_pct,
                    curve_price_sol, complete,
                    account_exists, rpc_status,
                    is_in_watchlist, source,
                    is_mayhem_mode, mayhem_checked_at,
                    mayhem_source, mayhem_conflict,
                    create_event_version, event_checked_at,
                    event_token_total_supply_raw,
                    event_virtual_token_reserves_raw,
                    event_virtual_quote_reserves_raw,
                    event_real_token_reserves_raw,
                    curve_confirmed, market_mode
                )
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, '', ?,
                        'creator', 'BONDING_EVENT', 0,
                        0.00000003, 0, 0, 'EVENT_ONLY',
                        0, 'SmokeTest', ?, ?, 'CREATE_EVENT', 0,
                        'CREATE_V2', ?, '1000000000000000',
                        '1073000000000000', '30000000000',
                        '793100000000000', 0, 'BONDING')
                """,
                (
                    detected,
                    now.isoformat(),
                    index + 1,
                    f"smoke-{index}",
                    token_mint,
                    symbol,
                    symbol,
                    f"curve-{index}",
                    mayhem,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        connection.commit()

    sources = strategy.load_sources()
    curves = {
        str(row["bonding_curve"]): strategy.event_curve_from_row(row)
        for row in sources
    }

    with strategy.connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        strategy.synchronize_candidates(
            connection,
            sources,
            curves,
            config,
        )
        portfolio = strategy.latest_portfolio(connection)
        cash = strategy.open_ready_positions(
            connection,
            curves,
            config,
            float(portfolio["cash_sol"]),
        )
        strategy.save_portfolio_snapshot(
            connection,
            cash,
            0.0,
        )
        connection.commit()

    with closing(
        sqlite3.connect(
            diagnostic_db,
            timeout=10,
        )
    ) as connection:
        connection.row_factory = sqlite3.Row
        position = connection.execute(
            "SELECT * FROM positions WHERE status='OPEN'"
        ).fetchone()
        mayhem_state = connection.execute(
            """
            SELECT state
            FROM qualification_candidates
            WHERE mint=?
            """,
            (mayhem_mint,),
        ).fetchone()

    if position is None:
        raise RuntimeError("Aucune position Paper Pilot créée.")
    if position["token_mint"] != mint:
        raise RuntimeError("Le mauvais token a été acheté.")
    if position["strategy"] != "paper_pilot_v12":
        raise RuntimeError("Stratégie Paper Pilot absente.")
    if abs(float(position["entry_sol"]) - 0.01) > 1e-12:
        raise RuntimeError("Taille Paper Pilot incorrecte.")
    if mayhem_state is None or mayhem_state[0] != "REJECTED":
        raise RuntimeError("Le token Mayhem n'a pas été rejeté.")

    print("PASS — CreateEvent → PAPER_PILOT_READY → achat paper 0,01 SOL.")
    print("PASS — token Mayhem rejeté.")
    print(f"Base de diagnostic conservée : {diagnostic_db}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FAIL — {error}")
        raise SystemExit(1)
