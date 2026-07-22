
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
CONFIG_PATH = BASE_DIR / "config.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT NOT NULL,
    token_name TEXT,
    symbol TEXT,
    market_mode TEXT NOT NULL,
    lifecycle_at_entry TEXT,
    bonding_curve_address TEXT,
    pair_address TEXT,
    source_url TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry_sol REAL NOT NULL,
    exit_sol REAL,
    tokens_received REAL NOT NULL,
    entry_price_sol REAL NOT NULL,
    entry_price_usd REAL,
    exit_price_sol REAL,
    current_price_sol REAL,
    current_price_usd REAL,
    current_value_sol REAL,
    entry_liquidity_usd REAL,
    entry_market_cap_usd REAL,
    entry_bonding_progress_pct REAL,
    entry_fees_sol REAL DEFAULT 0,
    exit_fees_sol REAL DEFAULT 0,
    realized_pnl_sol REAL,
    realized_pnl_pct REAL,
    strategy TEXT NOT NULL,
    exit_reason TEXT,
    status TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS position_risk_state (
    position_id INTEGER PRIMARY KEY,
    peak_pnl_pct REAL NOT NULL DEFAULT 0,
    break_even_armed INTEGER NOT NULL DEFAULT 0,
    active_stop_pct REAL NOT NULL DEFAULT -20,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_position_risk_break_even
ON position_risk_state(break_even_armed, peak_pnl_pct DESC);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cash_sol REAL NOT NULL,
    open_positions_value_sol REAL NOT NULL,
    equity_sol REAL NOT NULL,
    realized_pnl_sol REAL NOT NULL,
    unrealized_pnl_sol REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_name TEXT,
    symbol TEXT,
    lifecycle_state TEXT,
    decision TEXT NOT NULL,
    strategy TEXT NOT NULL,
    score REAL,
    reasons_json TEXT
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_name TEXT,
    symbol TEXT,
    market_mode TEXT,
    side TEXT NOT NULL,
    requested_sol REAL,
    expected_output REAL,
    simulated_output REAL,
    price_impact_pct REAL,
    extra_slippage_pct REAL,
    latency_ms INTEGER,
    status TEXT NOT NULL,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_name TEXT,
    symbol TEXT NOT NULL,
    price_sol REAL,
    price_usd REAL,
    change_1m_pct REAL,
    change_5m_pct REAL,
    change_h1_pct REAL,
    liquidity_usd REAL,
    volume_5m_usd REAL,
    volume_h1_usd REAL,
    buys_5m INTEGER,
    sells_5m INTEGER,
    market_cap_usd REAL,
    fdv_usd REAL,
    pair_created_at INTEGER,
    pair_address TEXT,
    dex_id TEXT,
    source_url TEXT,
    score REAL,
    data_status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_mint_time
ON market_snapshots(token_mint, timestamp);

CREATE TABLE IF NOT EXISTS bonding_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_name TEXT,
    symbol TEXT,
    bonding_curve_address TEXT NOT NULL,
    account_exists INTEGER NOT NULL,
    owner_valid INTEGER NOT NULL,
    discriminator_valid INTEGER NOT NULL,
    virtual_token_reserves_raw INTEGER,
    virtual_quote_reserves_raw INTEGER,
    real_token_reserves_raw INTEGER,
    real_quote_reserves_raw INTEGER,
    token_total_supply_raw INTEGER,
    complete INTEGER,
    creator TEXT,
    is_mayhem_mode INTEGER,
    initial_real_token_reserves_raw INTEGER,
    progress_pct REAL,
    curve_price_sol REAL,
    curve_price_usd REAL,
    real_quote_reserves_sol REAL,
    lifecycle_state TEXT NOT NULL,
    rpc_status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bonding_mint_time
ON bonding_snapshots(token_mint, timestamp);


CREATE TABLE IF NOT EXISTS new_launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    last_updated_at TEXT,
    slot INTEGER,
    signature TEXT NOT NULL,
    event_index INTEGER NOT NULL,
    mint TEXT NOT NULL UNIQUE,
    name TEXT,
    symbol TEXT,
    uri TEXT,
    bonding_curve TEXT NOT NULL,
    creator TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'DETECTED',
    progress_pct REAL,
    curve_price_sol REAL,
    curve_price_usd REAL,
    real_quote_reserves_sol REAL,
    complete INTEGER,
    account_exists INTEGER,
    rpc_status TEXT,
    is_in_watchlist INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'Pump CreateEvent',

    is_mayhem_mode INTEGER,
    mayhem_checked_at TEXT,
    exclusion_reason TEXT,
    mayhem_source TEXT,
    mayhem_conflict INTEGER NOT NULL DEFAULT 0,

    create_event_version TEXT,
    event_checked_at TEXT,
    event_detection_latency_ms REAL,
    event_token_program TEXT,
    event_token_total_supply_raw TEXT,
    event_virtual_token_reserves_raw TEXT,
    event_real_token_reserves_raw TEXT,
    curve_confirmed INTEGER NOT NULL DEFAULT 0,
    curve_confirmed_at TEXT,
    event_is_cashback_enabled INTEGER,
    event_quote_mint TEXT,
    event_virtual_quote_reserves_raw TEXT,

    market_mode TEXT NOT NULL DEFAULT 'BONDING',
    migrated_at TEXT,
    market_last_updated_at TEXT,
    pair_address TEXT,
    dex_id TEXT,
    pair_url TEXT,
    pool_base_token_account TEXT,
    pool_quote_token_account TEXT,
    market_price_sol REAL,
    market_price_usd REAL,
    market_change_5m_pct REAL,
    market_change_h1_pct REAL,
    market_liquidity_usd REAL,
    market_volume_5m_usd REAL,
    market_volume_h1_usd REAL,
    market_buys_5m INTEGER,
    market_sells_5m INTEGER,
    market_cap_usd REAL,
    market_fdv_usd REAL,
    pair_created_at INTEGER,
    market_data_status TEXT,

    UNIQUE(signature, event_index)
);

CREATE INDEX IF NOT EXISTS idx_new_launches_detected
ON new_launches(detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_new_launches_creator
ON new_launches(creator);


CREATE TABLE IF NOT EXISTS safety_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessed_at TEXT NOT NULL,
    launch_id INTEGER,
    mint TEXT NOT NULL UNIQUE,
    creator TEXT,
    symbol TEXT,
    token_name TEXT,
    lifecycle_state TEXT,
    progress_pct REAL,
    safety_score REAL NOT NULL,
    decision TEXT NOT NULL,
    hard_reject INTEGER NOT NULL DEFAULT 0,
    reasons_json TEXT,
    warnings_json TEXT,
    mint_account_exists INTEGER,
    token_program TEXT,
    decimals INTEGER,
    supply_raw TEXT,
    mint_authority TEXT,
    freeze_authority TEXT,
    mint_authority_revoked INTEGER,
    freeze_authority_revoked INTEGER,
    top1_pct REAL,
    top5_pct REAL,
    top10_pct REAL,
    top20_pct REAL,
    distinct_top_owners INTEGER,
    curve_balance_pct REAL,
    largest_accounts_count INTEGER,
    creator_launch_count INTEGER,
    buys_5m INTEGER,
    sells_5m INTEGER,
    activity_source TEXT,
    analysis_status TEXT NOT NULL,
    error_text TEXT,

    is_mayhem_mode INTEGER,
    market_mode TEXT,
    pair_address TEXT,
    ignored_pool_token_account TEXT,
    holder_analysis_status TEXT NOT NULL DEFAULT 'PENDING',
    concentration_source TEXT,
    provisional_score INTEGER NOT NULL DEFAULT 0,
    rpc_attempts INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_holder_success_at TEXT,
    next_retry_at TEXT,
    assessment_version TEXT NOT NULL DEFAULT 'V11',

    FOREIGN KEY(launch_id) REFERENCES new_launches(id)
);

CREATE INDEX IF NOT EXISTS idx_safety_decision_score
ON safety_assessments(decision, safety_score DESC);

CREATE INDEX IF NOT EXISTS idx_safety_assessed_at
ON safety_assessments(assessed_at DESC);


CREATE TABLE IF NOT EXISTS qualification_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    launch_id INTEGER,
    mint TEXT NOT NULL UNIQUE,
    symbol TEXT,
    token_name TEXT,
    creator TEXT,
    bonding_curve TEXT,
    created_at TEXT NOT NULL,
    first_qualified_at TEXT,
    last_sample_at TEXT,
    ready_at TEXT,
    state TEXT NOT NULL,
    safety_score REAL,
    qualification_score REAL,
    observation_samples INTEGER NOT NULL DEFAULT 0,
    stable_samples INTEGER NOT NULL DEFAULT 0,
    initial_progress_pct REAL,
    current_progress_pct REAL,
    progress_delta_pct REAL,
    initial_price_sol REAL,
    current_price_sol REAL,
    price_change_pct REAL,
    min_safety_score REAL,
    max_safety_score REAL,
    creator_launch_count INTEGER,
    analysis_status TEXT,
    decision TEXT,
    reason TEXT,
    position_id INTEGER,

    is_mayhem_mode INTEGER,
    entry_mode TEXT NOT NULL DEFAULT 'STRICT',
    market_mode TEXT NOT NULL DEFAULT 'BONDING',
    pair_address TEXT,
    liquidity_usd REAL,
    volume_5m_usd REAL,
    buys_5m INTEGER,
    sells_5m INTEGER,
    market_price_sol REAL,
    market_data_at TEXT,

    updated_at TEXT NOT NULL,
    FOREIGN KEY(launch_id) REFERENCES new_launches(id),
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE INDEX IF NOT EXISTS idx_qualification_state_score
ON qualification_candidates(state, qualification_score DESC);

CREATE TABLE IF NOT EXISTS qualification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    previous_state TEXT,
    new_state TEXT NOT NULL,
    qualification_score REAL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_qualification_events_time
ON qualification_events(timestamp DESC);


CREATE TABLE IF NOT EXISTS research_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    token_name TEXT,
    detected_at TEXT,
    lifecycle_state TEXT,
    progress_pct REAL,
    curve_price_sol REAL,
    curve_price_usd REAL,
    real_quote_reserves_sol REAL,
    safety_score REAL,
    safety_decision TEXT,
    safety_hard_reject INTEGER,
    top1_pct REAL,
    top10_pct REAL,
    creator_launch_count INTEGER,
    qualification_state TEXT,
    qualification_score REAL,
    observation_samples INTEGER,
    stable_samples INTEGER,
    progress_delta_pct REAL,
    price_change_pct REAL,
    position_id INTEGER,
    position_status TEXT,
    position_value_sol REAL,
    realized_pnl_sol REAL,
    is_mayhem_mode INTEGER,
    market_mode TEXT,
    pair_address TEXT,
    liquidity_usd REAL,
    volume_5m_usd REAL,
    buys_5m INTEGER,
    sells_5m INTEGER,
    market_price_sol REAL
);

CREATE INDEX IF NOT EXISTS idx_research_samples_mint_time
ON research_samples(mint, timestamp);

CREATE INDEX IF NOT EXISTS idx_research_samples_time
ON research_samples(timestamp DESC);

CREATE TABLE IF NOT EXISTS research_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    event_type TEXT NOT NULL,
    previous_value TEXT,
    new_value TEXT,
    details_json TEXT,
    source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_events_mint_time
ON research_events(mint, timestamp DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    trade_count INTEGER NOT NULL,
    win_rate REAL NOT NULL,
    pnl_sol REAL NOT NULL,
    return_pct REAL NOT NULL,
    max_drawdown_sol REAL NOT NULL,
    ending_equity_sol REAL NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    entry_at TEXT NOT NULL,
    exit_at TEXT NOT NULL,
    entry_price_sol REAL NOT NULL,
    exit_price_sol REAL NOT NULL,
    position_size_sol REAL NOT NULL,
    tokens_received REAL NOT NULL,
    entry_safety_score REAL,
    entry_qualification_score REAL,
    entry_progress_pct REAL,
    exit_progress_pct REAL,
    pnl_sol REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    holding_seconds REAL NOT NULL,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run
ON backtest_trades(run_id);


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

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (
        DB_PATH,
        Path(str(DB_PATH) + "-wal"),
        Path(str(DB_PATH) + "-shm"),
    ):
        candidate.unlink(missing_ok=True)

    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    now = now_iso()

    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(SCHEMA)

        initial_capital = float(config.get("initial_capital_sol", 1.0))
        connection.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, cash_sol, open_positions_value_sol,
                equity_sol, realized_pnl_sol, unrealized_pnl_sol
            )
            VALUES (?, ?, 0.0, ?, 0.0, 0.0)
            """,
            (now, initial_capital, initial_capital),
        )

        for token in watchlist["tokens"]:
            connection.execute(
                """
                INSERT INTO market_snapshots (
                    timestamp, token_mint, token_name, symbol,
                    price_sol, price_usd, change_1m_pct, change_5m_pct,
                    change_h1_pct, liquidity_usd, volume_5m_usd,
                    volume_h1_usd, buys_5m, sells_5m,
                    market_cap_usd, fdv_usd, pair_created_at,
                    pair_address, dex_id, source_url, score, data_status
                )
                VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, 0,
                        0, 0, 0, 0, 0, NULL, NULL, NULL,
                        NULL, NULL, NULL, 0, 'WAITING')
                """,
                (now, token["address"], token["label"], token["label"]),
            )

        states = {
            "status": "STOPPED",
            "dex_status": "WAITING",
            "rpc_status": "WAITING",
            "data_source": "Pump WebSocket + Solana RPC + DEX Screener hybride",
            "last_tick": "",
            "last_error": "",
            "sol_price_usd": "",
            "paper_trading": "ENABLED",
            "pump_program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "radar_status": "STOPPED",
            "radar_last_event": "",
            "radar_last_error": "",
            "radar_launches_detected": "0",
            "safety_status": "STOPPED",
            "safety_last_scan": "",
            "safety_last_error": "",
            "safety_assessed_count": "0",
            "safety_qualified_count": "0",
            "safety_complete_count": "0",
            "safety_partial_count": "0",
            "safety_error_count": "0",
            "safety_provisional_count": "0",
            "safety_queue_pending": "0",
            "safety_last_success": "",
            "safety_rpc_rate_limited": "0",
            "qualification_status": "STOPPED",
            "qualification_last_cycle": "",
            "qualification_last_error": "",
            "qualification_observation_count": "0",
            "qualification_ready_count": "0",
            "qualification_open_count": "0",
            "hybrid_market_status": "STOPPED",
            "hybrid_market_last_scan": "",
            "hybrid_market_last_error": "",
            "hybrid_market_pairs_found": "0",
            "hybrid_market_migrated_count": "0",
            "recorder_status": "STOPPED",
            "recorder_last_sample": "",
            "recorder_last_error": "",
            "recorder_sample_count": "0",
            "recorder_event_count": "0",
            "strategy_version": "stable_paper_pilot_v12",
            "strategy_rules": "paper pilot 0.01 SOL after 25s from event data; full acquisition 0.05 SOL after complete safety; Mayhem always blocked; one position",
            "position_risk_status": "READY",
            "holder_denominator": "TOTAL_SUPPLY",
            "pool_holder_rule": "IGNORED",
            "supervisor_status": "STOPPED",
            "supervisor_last_tick": "",
            "supervisor_engine_count": "6",
            "db_integrity": "UNKNOWN",
            "db_last_checkpoint": "",
            "db_last_backup": "",
            "db_last_backup_file": "",
            "diagnostics_release": "12.2",
            "hybrid_release": "11.0",
            "entry_ranking_release": "11.1",
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
            "startup_smoke_test_required": "DISABLED",
            "startup_diagnostics_non_blocking": "ENABLED",
            "hotfix_release": "12.2",
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
            "safety_full_parallel_workers": "2",
            "mayhem_filter_status": "ENABLED",
            "mayhem_detected_count": "0",
            "mayhem_excluded_count": "0",
            "mayhem_unknown_count": "0",
            "entry_ranking_status": "READY",
        }

        for engine_key in (
            "collector",
            "radar",
            "safety",
            "strategy",
            "recorder",
            "market",
        ):
            states[f"supervisor_{engine_key}_status"] = "STOPPED"
            states[f"supervisor_{engine_key}_pid"] = ""
            states[f"supervisor_{engine_key}_restarts"] = "0"
            states[f"supervisor_{engine_key}_last_exit"] = ""
            states[f"supervisor_{engine_key}_message"] = ""
        for key, value in states.items():
            connection.execute(
                """
                INSERT INTO bot_state(key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")

    # sqlite3's native context manager commits/rolls back but does not close.
    # Explicit closure prevents WinError 32 during tests, imports and cleanup.
    connection.close()

    print("=" * 64)
    print("SOLPULSE STABLE PAPER PILOT V12.2")
    print("=" * 64)
    print(f"Capital paper : {initial_capital:.4f} SOL")
    print(f"Contrats surveillés : {len(watchlist['tokens'])}")
    print("Base réinitialisée.")


if __name__ == "__main__":
    main()
