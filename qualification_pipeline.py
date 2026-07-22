
from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from runtime_utils import connect_sqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
CONFIG_PATH = BASE_DIR / "config.json"
LOCK_PATH = BASE_DIR / "data" / "qualification_pipeline.lock"

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONDING_CURVE_DISCRIMINATOR = bytes(
    [23, 183, 248, 55, 96, 216, 172, 96]
)

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
STRICT_STRATEGY = "hybrid_strategy_v11"
LEGACY_ACQUISITION_STRATEGY = "acquisition_validation_v11_4"
ACQUISITION_STRATEGY = "full_acquisition_v12"
PAPER_PILOT_STRATEGY = "paper_pilot_v12"
STRATEGY = PAPER_PILOT_STRATEGY

running = True


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def age_seconds(value: str | None) -> float:
    timestamp = parse_datetime(value)
    if timestamp is None:
        return float("inf")
    return max(
        0.0,
        (now_utc() - timestamp).total_seconds(),
    )


def connect_db() -> sqlite3.Connection:
    return connect_sqlite(DB_PATH, timeout_seconds=30)


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
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, str(value), now_iso()),
    )


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < 30:
            print("Le Strategy Engine fonctionne déjà.")
            raise SystemExit(1)
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def rpc_call(
    client: httpx.Client,
    method: str,
    params: list[Any],
) -> Any:
    response = client.post(
        "",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        },
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"{method}: {payload['error']}")
    return payload.get("result")


def fetch_curve_accounts(
    client: httpx.Client,
    curves: list[str],
) -> dict[str, dict[str, Any] | None]:
    result_map: dict[str, dict[str, Any] | None] = {}

    for start in range(0, len(curves), 100):
        chunk = curves[start : start + 100]
        if not chunk:
            continue
        result = rpc_call(
            client,
            "getMultipleAccounts",
            [
                chunk,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                },
            ],
        )
        accounts = (result or {}).get("value") or []
        for curve, account in zip(chunk, accounts):
            result_map[curve] = account

    return result_map


def decode_u64(data: bytes, offset: int) -> int:
    if offset + 8 > len(data):
        raise ValueError("Compte bonding curve trop court.")
    return struct.unpack_from("<Q", data, offset)[0]


def decode_curve(
    account: dict[str, Any] | None,
) -> dict[str, Any]:
    empty = {
        "valid": False,
        "complete": False,
        "virtual_token_raw": 0,
        "virtual_quote_raw": 0,
        "real_token_raw": 0,
        "real_quote_raw": 0,
        "price_sol": 0.0,
        "real_quote_sol": 0.0,
        "status": "NO_ACCOUNT",
    }

    if not account:
        return empty

    try:
        raw = base64.b64decode(account["data"][0])
    except Exception:
        return {**empty, "status": "BAD_BASE64"}

    if len(raw) < 49:
        return {**empty, "status": "ACCOUNT_TOO_SHORT"}

    owner_valid = account.get("owner") == PUMP_PROGRAM_ID
    discriminator_valid = (
        raw[:8] == BONDING_CURVE_DISCRIMINATOR
    )

    try:
        virtual_token = decode_u64(raw, 8)
        virtual_quote = decode_u64(raw, 16)
        real_token = decode_u64(raw, 24)
        real_quote = decode_u64(raw, 32)
        complete = bool(raw[48])

        price_sol = 0.0
        if virtual_token > 0:
            price_sol = (
                virtual_quote / LAMPORTS_PER_SOL
            ) / (
                virtual_token / (10**TOKEN_DECIMALS)
            )

        return {
            "valid": bool(
                owner_valid and discriminator_valid
            ),
            "complete": complete,
            "virtual_token_raw": virtual_token,
            "virtual_quote_raw": virtual_quote,
            "real_token_raw": real_token,
            "real_quote_raw": real_quote,
            "price_sol": price_sol,
            "real_quote_sol": (
                real_quote / LAMPORTS_PER_SOL
            ),
            "status": (
                "OK"
                if owner_valid and discriminator_valid
                else "UNVERIFIED_ACCOUNT"
            ),
        }
    except (ValueError, struct.error):
        return {**empty, "status": "DECODE_ERROR"}



def event_curve_from_row(
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Build a paper-only curve quote from Pump CreateEvent reserves.

    This keeps the paper pilot moving when a public RPC is delayed. The
    on-chain BondingCurve remains preferred and replaces this fallback as
    soon as it becomes available.
    """
    virtual_token = to_int(
        row["event_virtual_token_reserves_raw"]
        if "event_virtual_token_reserves_raw" in row.keys()
        else 0
    )
    virtual_quote = to_int(
        row["event_virtual_quote_reserves_raw"]
        if "event_virtual_quote_reserves_raw" in row.keys()
        else 0
    )
    real_token = to_int(
        row["event_real_token_reserves_raw"]
        if "event_real_token_reserves_raw" in row.keys()
        else 0
    )
    valid = (
        virtual_token > 0
        and virtual_quote > 0
        and real_token > 0
        and not bool(row["complete"])
    )
    price_sol = 0.0
    if virtual_token > 0:
        price_sol = (
            virtual_quote / LAMPORTS_PER_SOL
        ) / (
            virtual_token / (10**TOKEN_DECIMALS)
        )
    return {
        "valid": valid,
        "complete": bool(row["complete"]),
        "virtual_token_raw": virtual_token,
        "virtual_quote_raw": virtual_quote,
        "real_token_raw": real_token,
        "real_quote_raw": 0,
        "price_sol": price_sol,
        "real_quote_sol": 0.0,
        "status": "EVENT_FALLBACK" if valid else "NO_EVENT_RESERVES",
        "source": "CREATE_EVENT",
    }


def constant_product_buy_quote(
    curve: dict[str, Any],
    amount_sol: float,
    fee_bps: int,
) -> tuple[float, float, float]:
    virtual_token = int(curve["virtual_token_raw"])
    virtual_quote = int(curve["virtual_quote_raw"])
    real_token = int(curve["real_token_raw"])
    gross_quote_raw = int(amount_sol * LAMPORTS_PER_SOL)
    net_quote_raw = int(
        gross_quote_raw * (10_000 - fee_bps) / 10_000
    )

    if (
        virtual_token <= 0
        or virtual_quote <= 0
        or real_token <= 0
        or net_quote_raw <= 0
    ):
        return 0.0, 0.0, 0.0

    invariant = virtual_token * virtual_quote
    new_virtual_quote = virtual_quote + net_quote_raw
    new_virtual_token = (
        invariant + new_virtual_quote - 1
    ) // new_virtual_quote
    token_out_raw = min(
        max(0, virtual_token - new_virtual_token),
        real_token,
    )
    tokens_out = token_out_raw / (10**TOKEN_DECIMALS)

    spot_price = (
        virtual_quote / LAMPORTS_PER_SOL
    ) / (
        virtual_token / (10**TOKEN_DECIMALS)
    )
    effective_price = (
        amount_sol / tokens_out
        if tokens_out > 0
        else 0.0
    )
    impact_pct = (
        (effective_price / spot_price - 1) * 100
        if spot_price > 0 and effective_price > 0
        else 0.0
    )
    return (
        tokens_out,
        effective_price,
        max(0.0, impact_pct),
    )


def constant_product_sell_quote(
    curve: dict[str, Any],
    tokens: float,
    fee_bps: int,
) -> tuple[float, float]:
    virtual_token = int(curve["virtual_token_raw"])
    virtual_quote = int(curve["virtual_quote_raw"])
    token_in_raw = int(tokens * (10**TOKEN_DECIMALS))

    if (
        virtual_token <= 0
        or virtual_quote <= 0
        or token_in_raw <= 0
    ):
        return 0.0, 0.0

    invariant = virtual_token * virtual_quote
    new_virtual_token = virtual_token + token_in_raw
    new_virtual_quote = invariant // new_virtual_token
    gross_quote_raw = max(
        0,
        virtual_quote - new_virtual_quote,
    )
    net_quote_raw = int(
        gross_quote_raw * (10_000 - fee_bps) / 10_000
    )
    output_sol = net_quote_raw / LAMPORTS_PER_SOL

    spot_value = tokens * (
        virtual_quote / LAMPORTS_PER_SOL
    ) / (
        virtual_token / (10**TOKEN_DECIMALS)
    )
    impact_pct = (
        max(0.0, (1 - output_sol / spot_value) * 100)
        if spot_value > 0
        else 0.0
    )
    return output_sol, impact_pct


def dex_impact_pct(
    amount_sol: float,
    liquidity_usd: float,
    sol_price_usd: float,
) -> float:
    if liquidity_usd <= 0 or sol_price_usd <= 0:
        return 0.0
    position_usd = amount_sol * sol_price_usd
    return max(
        0.0,
        position_usd / liquidity_usd * 100.0,
    )


def dex_buy_quote(
    price_sol: float,
    amount_sol: float,
    liquidity_usd: float,
    sol_price_usd: float,
    fee_bps: int,
    slippage_bps: int,
) -> tuple[float, float, float]:
    if price_sol <= 0 or amount_sol <= 0:
        return 0.0, 0.0, 0.0

    impact = dex_impact_pct(
        amount_sol,
        liquidity_usd,
        sol_price_usd,
    )
    total_cost_pct = (
        fee_bps / 100.0
        + slippage_bps / 100.0
        + impact
    )
    net_sol = amount_sol * (
        1 - total_cost_pct / 100.0
    )
    tokens = (
        net_sol / price_sol
        if net_sol > 0
        else 0.0
    )
    effective_price = (
        amount_sol / tokens
        if tokens > 0
        else 0.0
    )
    return tokens, effective_price, impact


def dex_sell_quote(
    price_sol: float,
    tokens: float,
    liquidity_usd: float,
    sol_price_usd: float,
    fee_bps: int,
    slippage_bps: int,
) -> tuple[float, float]:
    gross = price_sol * tokens
    if gross <= 0:
        return 0.0, 0.0

    impact = dex_impact_pct(
        gross,
        liquidity_usd,
        sol_price_usd,
    )
    total_cost_pct = (
        fee_bps / 100.0
        + slippage_bps / 100.0
        + impact
    )
    return (
        gross * (1 - total_cost_pct / 100.0),
        impact,
    )


def latest_portfolio(
    connection: sqlite3.Connection,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM portfolio_snapshots
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Portefeuille non initialisé.")
    return row


def record_transition(
    connection: sqlite3.Connection,
    mint: str,
    symbol: str | None,
    previous_state: str | None,
    new_state: str,
    score: float,
    reason: str,
) -> None:
    if previous_state == new_state:
        return
    connection.execute(
        """
        INSERT INTO qualification_events (
            timestamp, mint, symbol,
            previous_state, new_state,
            qualification_score, reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            mint,
            symbol,
            previous_state,
            new_state,
            score,
            reason,
        ),
    )


def load_sources() -> list[sqlite3.Row]:
    with connect_db() as connection:
        return connection.execute(
            """
            SELECT
                launches.*,
                safety.assessed_at AS safety_assessed_at,
                safety.safety_score,
                safety.decision AS safety_decision,
                safety.hard_reject,
                safety.analysis_status,
                safety.holder_analysis_status,
                safety.top1_pct,
                safety.mint_authority_revoked,
                safety.freeze_authority_revoked,
                safety.creator_launch_count,

                candidates.id AS candidate_id,
                candidates.first_qualified_at,
                candidates.last_sample_at,
                candidates.ready_at,
                candidates.state AS candidate_state,
                candidates.qualification_score,
                candidates.observation_samples,
                candidates.stable_samples,
                candidates.initial_progress_pct,
                candidates.current_progress_pct,
                candidates.progress_delta_pct,
                candidates.initial_price_sol,
                candidates.current_price_sol,
                candidates.price_change_pct,
                candidates.min_safety_score,
                candidates.max_safety_score,
                candidates.position_id,
                candidates.entry_mode AS candidate_entry_mode,
                candidates.market_mode
                    AS candidate_market_mode
            FROM new_launches launches
            LEFT JOIN safety_assessments safety
                ON safety.mint=launches.mint
            LEFT JOIN qualification_candidates candidates
                ON candidates.mint=launches.mint
            ORDER BY datetime(launches.detected_at) DESC
            LIMIT 1200
            """
        ).fetchall()


def load_open_positions(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            positions.*,
            launches.bonding_curve,
            launches.lifecycle_state AS launch_lifecycle,
            launches.market_mode AS launch_market_mode,
            launches.market_last_updated_at,
            launches.market_price_sol,
            launches.market_price_usd,
            launches.market_liquidity_usd,
            launches.pair_address AS launch_pair_address,
            launches.pair_url,
            launches.complete AS curve_complete,
            launches.is_mayhem_mode,
            launches.mayhem_conflict,
            launches.event_virtual_token_reserves_raw,
            launches.event_virtual_quote_reserves_raw,
            launches.event_real_token_reserves_raw
        FROM positions
        LEFT JOIN new_launches launches
            ON launches.mint=positions.token_mint
        WHERE positions.status='OPEN'
          AND positions.strategy IN (?, ?, ?, ?)
        ORDER BY positions.id
        """,
        (
            STRICT_STRATEGY,
            LEGACY_ACQUISITION_STRATEGY,
            ACQUISITION_STRATEGY,
            PAPER_PILOT_STRATEGY,
        ),
    ).fetchall()


def bonding_qualification_score(
    safety_score: float,
    progress_delta: float,
    stable_samples: int,
    price_change_pct: float,
) -> float:
    score = safety_score
    score += min(max(progress_delta, 0.0) * 1.8, 14)
    score += min(stable_samples, 10) * 0.9
    if price_change_pct > 15:
        score -= price_change_pct - 15
    elif price_change_pct < -3:
        score -= abs(price_change_pct + 3) * 1.5
    return max(0.0, min(100.0, score))


def migrated_qualification_score(
    safety_score: float,
    stable_samples: int,
    liquidity_usd: float,
    volume_5m_usd: float,
    buys_5m: int,
    sells_5m: int,
    price_change_pct: float,
) -> float:
    score = safety_score
    score += min(stable_samples, 10) * 0.8

    if liquidity_usd >= 50_000:
        score += 8
    elif liquidity_usd >= 20_000:
        score += 5
    elif liquidity_usd >= 10_000:
        score += 2

    if volume_5m_usd >= 10_000:
        score += 8
    elif volume_5m_usd >= 3_000:
        score += 5
    elif volume_5m_usd >= 1_000:
        score += 2

    if buys_5m + sells_5m >= 30:
        score += 5
    elif buys_5m + sells_5m >= 15:
        score += 2

    if sells_5m >= 2:
        score += 3

    if price_change_pct > 30:
        score -= (price_change_pct - 30) * 0.8
    elif price_change_pct < -10:
        score -= abs(price_change_pct + 10) * 1.2

    return max(0.0, min(100.0, score))


def candidate_market_mode(
    row: sqlite3.Row,
) -> str:
    return (
        "MIGRATED_DEX"
        if str(row["market_mode"] or "") == "MIGRATED_DEX"
        and bool(row["pair_address"])
        else "BONDING"
    )


def reset_observation_required(
    row: sqlite3.Row,
    mode: str,
) -> bool:
    previous_mode = str(
        row["candidate_market_mode"] or ""
    )
    return bool(previous_mode and previous_mode != mode)


def upsert_candidate(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    state: str,
    safety_score: float,
    qualification_score: float,
    first_qualified_at: str | None,
    ready_at: str | None,
    samples: int,
    stable_samples: int,
    initial_progress: float,
    current_progress: float,
    progress_delta: float,
    initial_price: float,
    current_price: float,
    price_change: float,
    reason: str,
    market_mode: str,
) -> None:
    timestamp = now_iso()
    values = {
        "launch_id": row["id"],
        "mint": row["mint"],
        "symbol": row["symbol"],
        "token_name": row["name"],
        "creator": row["creator"],
        "bonding_curve": row["bonding_curve"],
        "created_at": row["detected_at"],
        "first_qualified_at": first_qualified_at,
        "last_sample_at": timestamp,
        "ready_at": ready_at,
        "state": state,
        "safety_score": safety_score,
        "qualification_score": qualification_score,
        "observation_samples": samples,
        "stable_samples": stable_samples,
        "initial_progress_pct": initial_progress,
        "current_progress_pct": current_progress,
        "progress_delta_pct": progress_delta,
        "initial_price_sol": initial_price,
        "current_price_sol": current_price,
        "price_change_pct": price_change,
        "min_safety_score": safety_score,
        "max_safety_score": safety_score,
        "creator_launch_count": to_int(
            row["creator_launch_count"]
        ),
        "analysis_status": row["analysis_status"],
        "decision": row["safety_decision"],
        "reason": reason,
        "is_mayhem_mode": row["is_mayhem_mode"],
        "entry_mode": (
            "PAPER_PILOT"
            if state == "PAPER_PILOT_READY"
            else "FULL_ACQUISITION"
            if state == "ACQUISITION_READY"
            else str(row["candidate_entry_mode"] or "STRICT")
        ),
        "market_mode": market_mode,
        "pair_address": row["pair_address"],
        "liquidity_usd": row["market_liquidity_usd"],
        "volume_5m_usd": row["market_volume_5m_usd"],
        "buys_5m": row["market_buys_5m"],
        "sells_5m": row["market_sells_5m"],
        "market_price_sol": row["market_price_sol"],
        "market_data_at": row["market_last_updated_at"],
        "updated_at": timestamp,
    }

    if row["candidate_id"]:
        connection.execute(
            """
            UPDATE qualification_candidates
            SET launch_id=:launch_id,
                symbol=:symbol,
                token_name=:token_name,
                creator=:creator,
                bonding_curve=:bonding_curve,
                first_qualified_at=:first_qualified_at,
                last_sample_at=:last_sample_at,
                ready_at=:ready_at,
                state=:state,
                safety_score=:safety_score,
                qualification_score=:qualification_score,
                observation_samples=:observation_samples,
                stable_samples=:stable_samples,
                initial_progress_pct=:initial_progress_pct,
                current_progress_pct=:current_progress_pct,
                progress_delta_pct=:progress_delta_pct,
                initial_price_sol=:initial_price_sol,
                current_price_sol=:current_price_sol,
                price_change_pct=:price_change_pct,
                min_safety_score=MIN(
                    COALESCE(min_safety_score, :safety_score),
                    :safety_score
                ),
                max_safety_score=MAX(
                    COALESCE(max_safety_score, :safety_score),
                    :safety_score
                ),
                creator_launch_count=:creator_launch_count,
                analysis_status=:analysis_status,
                decision=:decision,
                reason=:reason,
                is_mayhem_mode=:is_mayhem_mode,
                entry_mode=:entry_mode,
                market_mode=:market_mode,
                pair_address=:pair_address,
                liquidity_usd=:liquidity_usd,
                volume_5m_usd=:volume_5m_usd,
                buys_5m=:buys_5m,
                sells_5m=:sells_5m,
                market_price_sol=:market_price_sol,
                market_data_at=:market_data_at,
                updated_at=:updated_at
            WHERE mint=:mint
            """,
            values,
        )
    else:
        connection.execute(
            """
            INSERT INTO qualification_candidates (
                launch_id, mint, symbol, token_name,
                creator, bonding_curve, created_at,
                first_qualified_at, last_sample_at,
                ready_at, state, safety_score,
                qualification_score,
                observation_samples, stable_samples,
                initial_progress_pct,
                current_progress_pct,
                progress_delta_pct,
                initial_price_sol, current_price_sol,
                price_change_pct, min_safety_score,
                max_safety_score, creator_launch_count,
                analysis_status, decision, reason,
                position_id, is_mayhem_mode,
                entry_mode, market_mode, pair_address,
                liquidity_usd, volume_5m_usd,
                buys_5m, sells_5m,
                market_price_sol, market_data_at,
                updated_at
            )
            VALUES (
                :launch_id, :mint, :symbol, :token_name,
                :creator, :bonding_curve, :created_at,
                :first_qualified_at, :last_sample_at,
                :ready_at, :state, :safety_score,
                :qualification_score,
                :observation_samples, :stable_samples,
                :initial_progress_pct,
                :current_progress_pct,
                :progress_delta_pct,
                :initial_price_sol, :current_price_sol,
                :price_change_pct, :min_safety_score,
                :max_safety_score, :creator_launch_count,
                :analysis_status, :decision, :reason,
                NULL, :is_mayhem_mode,
                :entry_mode, :market_mode, :pair_address,
                :liquidity_usd, :volume_5m_usd,
                :buys_5m, :sells_5m,
                :market_price_sol, :market_data_at,
                :updated_at
            )
            """,
            values,
        )


def synchronize_candidates(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    curves: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> None:
    current_time = now_utc()
    minimum_safety = float(
        config["qualification_min_safety_score"]
    )

    for row in rows:
        mint = str(row["mint"])
        symbol = str(row["symbol"] or "")
        previous_state = (
            str(row["candidate_state"])
            if row["candidate_state"]
            else None
        )
        mode = candidate_market_mode(row)
        mayhem_raw = row["is_mayhem_mode"]

        if previous_state in {"PAPER_POSITION", "CLOSED"}:
            continue

        safety_score = to_float(row["safety_score"])

        if mayhem_raw is None:
            state = "PAUSED"
            reason = (
                "Statut Mayhem non vérifié — aucune entrée autorisée"
            )
            upsert_candidate(
                connection,
                row,
                state=state,
                safety_score=safety_score,
                qualification_score=safety_score,
                first_qualified_at=None,
                ready_at=None,
                samples=0,
                stable_samples=0,
                initial_progress=0,
                current_progress=0,
                progress_delta=0,
                initial_price=0,
                current_price=0,
                price_change=0,
                reason=reason,
                market_mode=mode,
            )
            record_transition(
                connection,
                mint,
                symbol,
                previous_state,
                state,
                safety_score,
                reason,
            )
            continue

        if bool(mayhem_raw):
            state = "REJECTED"
            reason = "MAYHEM MODE — exclu définitivement"
            upsert_candidate(
                connection,
                row,
                state=state,
                safety_score=0.0,
                qualification_score=0.0,
                first_qualified_at=None,
                ready_at=None,
                samples=0,
                stable_samples=0,
                initial_progress=0,
                current_progress=0,
                progress_delta=0,
                initial_price=0,
                current_price=0,
                price_change=0,
                reason=reason,
                market_mode=mode,
            )
            record_transition(
                connection,
                mint,
                symbol,
                previous_state,
                state,
                0.0,
                reason,
            )
            continue
        safety_decision = str(
            row["safety_decision"] or "PENDING"
        )
        analysis_status = str(
            row["analysis_status"] or "PENDING"
        )
        holder_status = str(
            row["holder_analysis_status"] or "PENDING"
        )
        hard_reject = bool(row["hard_reject"])

        fully_safe = (
            safety_decision == "QUALIFIED"
            and analysis_status == "COMPLETE"
            and holder_status == "COMPLETE"
            and not hard_reject
            and safety_score >= minimum_safety
        )

        acquisition_enabled = bool(
            config.get("acquisition_mode_enabled", False)
        )
        if hard_reject or (
            safety_decision == "REJECTED"
            and not acquisition_enabled
        ):
            state = "REJECTED"
            reason = (
                "Blocage critique du Safety Engine"
                if hard_reject
                else "Safety Engine a rejeté le token"
            )
            upsert_candidate(
                connection,
                row,
                state=state,
                safety_score=safety_score,
                qualification_score=safety_score,
                first_qualified_at=None,
                ready_at=None,
                samples=0,
                stable_samples=0,
                initial_progress=0,
                current_progress=0,
                progress_delta=0,
                initial_price=0,
                current_price=0,
                price_change=0,
                reason=reason,
                market_mode=mode,
            )
            record_transition(
                connection,
                mint,
                symbol,
                previous_state,
                state,
                safety_score,
                reason,
            )
            continue

        if bool(config.get("acquisition_mode_enabled", False)):
            top1_raw = row["top1_pct"]
            top1_known = top1_raw is not None
            top1_pct = to_float(top1_raw, 999.0)
            mint_raw = row["mint_authority_revoked"]
            freeze_raw = row["freeze_authority_revoked"]
            mint_known = mint_raw is not None
            freeze_known = freeze_raw is not None
            mint_revoked = bool(mint_raw) if mint_known else False
            freeze_revoked = bool(freeze_raw) if freeze_known else False
            mayhem_conflict = bool(row["mayhem_conflict"])
            holder_limit = float(
                config.get(
                    "acquisition_mode_require_top1_max_pct",
                    config["safety_max_top1_pct"],
                )
            )
            detected_age = age_seconds(row["detected_at"])
            delay_ok = detected_age >= float(
                config.get(
                    "acquisition_mode_entry_delay_seconds",
                    3,
                )
            )

            if mode == "MIGRATED_DEX":
                market_ready = (
                    bool(row["pair_address"])
                    and to_float(row["market_price_sol"]) > 0
                    and age_seconds(row["market_last_updated_at"])
                    <= float(
                        config.get(
                            "strategy_max_market_data_age_seconds",
                            20,
                        )
                    )
                )
            else:
                curve = curves.get(
                    str(row["bonding_curve"] or ""),
                    {},
                )
                market_ready = (
                    bool(curve.get("valid"))
                    and not bool(curve.get("complete"))
                    and to_float(curve.get("price_sol")) > 0
                    and detected_age <= float(
                        config.get(
                            "paper_pilot_max_event_age_seconds",
                            300,
                        )
                    )
                )

            full_checks = {
                "Mayhem confirmé NON": mayhem_raw == 0,
                "Aucun conflit Mayhem": not mayhem_conflict,
                "Analyse Safety complète": analysis_status == "COMPLETE",
                "Analyse holders complète": holder_status == "COMPLETE",
                "Aucun hard reject": not hard_reject,
                "Holder ≤ 3,5 %": (
                    top1_known
                    and top1_pct <= holder_limit + 1e-9
                ),
                "Mint authority révoquée": mint_known and mint_revoked,
                "Freeze authority révoquée": freeze_known and freeze_revoked,
                "Prix/marché exploitable": market_ready,
                "Délai minimal écoulé": delay_ok,
            }

            known_holder_breach = (
                top1_known
                and top1_pct > holder_limit + 1e-9
            )
            known_active_mint = (
                analysis_status == "COMPLETE"
                and mint_known
                and not mint_revoked
            )
            known_active_freeze = (
                analysis_status == "COMPLETE"
                and freeze_known
                and not freeze_revoked
            )
            pilot_delay_ok = detected_age >= float(
                config.get("paper_pilot_delay_seconds", 25)
            )
            pilot_checks = {
                "Mayhem confirmé NON": mayhem_raw == 0,
                "Aucun conflit Mayhem": not mayhem_conflict,
                "Aucun hard reject connu": not hard_reject,
                "Aucun dépassement holder connu": not known_holder_breach,
                "Mint authority non signalée active": not known_active_mint,
                "Freeze authority non signalée active": not known_active_freeze,
                "Prix paper exploitable": market_ready,
                "Délai pilot écoulé": pilot_delay_ok,
            }

            if all(full_checks.values()):
                state = "ACQUISITION_READY"
                reason = (
                    "ACQUISITION COMPLÈTE — Safety, holders et autorités validés"
                )
                ready_at = str(row["ready_at"] or now_iso())
            elif (
                bool(config.get("paper_pilot_enabled", True))
                and all(pilot_checks.values())
            ):
                state = "PAPER_PILOT_READY"
                reason = (
                    "PAPER PILOT — achat test autorisé depuis les données "
                    "CreateEvent; contrôles inconnus surveillés après l’entrée"
                )
                ready_at = str(row["ready_at"] or now_iso())
            else:
                state = "PAUSED"
                missing_full = [
                    label
                    for label, passed in full_checks.items()
                    if not passed
                ]
                missing_pilot = [
                    label
                    for label, passed in pilot_checks.items()
                    if not passed
                ]
                reason = (
                    "ATTENTE — complet: "
                    + ", ".join(missing_full[:4])
                    + " | pilot: "
                    + ", ".join(missing_pilot[:4])
                )
                ready_at = None

            upsert_candidate(
                connection,
                row,
                state=state,
                safety_score=safety_score,
                qualification_score=safety_score,
                first_qualified_at=(
                    str(row["first_qualified_at"])
                    if row["first_qualified_at"]
                    else now_iso()
                ),
                ready_at=ready_at,
                samples=to_int(row["observation_samples"]),
                stable_samples=to_int(row["stable_samples"]),
                initial_progress=to_float(
                    row["initial_progress_pct"]
                ),
                current_progress=to_float(row["progress_pct"]),
                progress_delta=to_float(
                    row["progress_delta_pct"]
                ),
                initial_price=to_float(row["initial_price_sol"]),
                current_price=(
                    to_float(row["market_price_sol"])
                    if mode == "MIGRATED_DEX"
                    else to_float(
                        curves.get(
                            str(row["bonding_curve"] or ""),
                            {},
                        ).get("price_sol")
                    )
                ),
                price_change=to_float(row["price_change_pct"]),
                reason=reason,
                market_mode=mode,
            )
            record_transition(
                connection,
                mint,
                symbol,
                previous_state,
                state,
                safety_score,
                reason,
            )
            continue

        if not fully_safe:
            state = "PAUSED"
            reason = (
                "En attente d’une analyse Safety complète "
                f"(Safety={safety_decision}, "
                f"analyse={analysis_status}, "
                f"holders={holder_status})"
            )
            upsert_candidate(
                connection,
                row,
                state=state,
                safety_score=safety_score,
                qualification_score=safety_score,
                first_qualified_at=None,
                ready_at=None,
                samples=0,
                stable_samples=0,
                initial_progress=0,
                current_progress=0,
                progress_delta=0,
                initial_price=0,
                current_price=0,
                price_change=0,
                reason=reason,
                market_mode=mode,
            )
            record_transition(
                connection,
                mint,
                symbol,
                previous_state,
                state,
                safety_score,
                reason,
            )
            continue

        if reset_observation_required(row, mode):
            first_qualified = current_time
            last_sample = None
            samples = 0
            stable_samples = 0
            initial_progress = 0.0
            initial_price = 0.0
        else:
            first_qualified = (
                parse_datetime(row["first_qualified_at"])
                or current_time
            )
            last_sample = parse_datetime(
                row["last_sample_at"]
            )
            samples = to_int(
                row["observation_samples"]
            )
            stable_samples = to_int(
                row["stable_samples"]
            )
            initial_progress = to_float(
                row["initial_progress_pct"]
            )
            initial_price = to_float(
                row["initial_price_sol"]
            )

        sample_due = (
            last_sample is None
            or (current_time - last_sample).total_seconds()
            >= 5
        )

        if mode == "MIGRATED_DEX":
            price = to_float(row["market_price_sol"])
            liquidity = to_float(
                row["market_liquidity_usd"]
            )
            volume_5m = to_float(
                row["market_volume_5m_usd"]
            )
            buys = to_int(row["market_buys_5m"])
            sells = to_int(row["market_sells_5m"])

            if initial_price <= 0:
                initial_price = price
            if sample_due:
                samples += 1
                stable = (
                    price > 0
                    and liquidity
                    >= float(
                        config[
                            "hybrid_market_min_liquidity_usd"
                        ]
                    )
                    and age_seconds(
                        row["market_last_updated_at"]
                    )
                    <= float(
                        config[
                            "hybrid_market_data_stale_seconds"
                        ]
                    )
                )
                if stable:
                    stable_samples += 1

            price_change = (
                (price / initial_price - 1) * 100
                if price > 0 and initial_price > 0
                else 0.0
            )
            stable_ratio = (
                stable_samples / max(samples, 1)
            )
            pair_age = (
                max(
                    0.0,
                    current_time.timestamp()
                    - to_int(row["pair_created_at"]) / 1000,
                )
                if to_int(row["pair_created_at"]) > 0
                else float("inf")
            )
            ratio = (
                buys / sells
                if sells > 0
                else float("inf")
            )
            observation_age = (
                current_time - first_qualified
            ).total_seconds()

            score = migrated_qualification_score(
                safety_score,
                stable_samples,
                liquidity,
                volume_5m,
                buys,
                sells,
                price_change,
            )
            checks = {
                "observation": observation_age
                >= float(
                    config[
                        "hybrid_market_observation_seconds"
                    ]
                ),
                "samples": samples
                >= int(
                    config["hybrid_market_min_samples"]
                ),
                "stability": stable_ratio >= 0.80,
                "fresh_market": age_seconds(
                    row["market_last_updated_at"]
                )
                <= float(
                    config[
                        "hybrid_market_data_stale_seconds"
                    ]
                ),
                "pair_age_min": pair_age
                >= float(
                    config[
                        "hybrid_market_min_pair_age_seconds"
                    ]
                ),
                "pair_age_max": pair_age
                <= float(
                    config[
                        "hybrid_market_max_pair_age_hours"
                    ]
                )
                * 3600,
                "liquidity": liquidity
                >= float(
                    config[
                        "hybrid_market_min_liquidity_usd"
                    ]
                ),
                "volume": volume_5m
                >= float(
                    config[
                        "hybrid_market_min_volume_5m_usd"
                    ]
                ),
                "buys": buys
                >= int(
                    config["hybrid_market_min_buys_5m"]
                ),
                "sells": sells
                >= int(
                    config["hybrid_market_min_sells_5m"]
                ),
                "ratio": ratio
                <= float(
                    config[
                        "hybrid_market_max_buy_sell_ratio"
                    ]
                ),
                "price": abs(price_change)
                <= float(
                    config[
                        "hybrid_market_max_price_change_pct"
                    ]
                ),
                "qualification": score
                >= float(
                    config[
                        "hybrid_market_min_qualification_score"
                    ]
                ),
                "safety_fresh": age_seconds(
                    row["safety_assessed_at"]
                )
                <= float(
                    config[
                        "qualification_max_safety_age_seconds"
                    ]
                ),
            }
            progress = 100.0
            progress_delta = 0.0
        else:
            curve = curves.get(
                str(row["bonding_curve"] or ""),
                {},
            )
            price = to_float(curve.get("price_sol"))
            progress = to_float(row["progress_pct"], -1)
            if initial_price <= 0:
                initial_price = price
            if initial_progress <= 0:
                initial_progress = progress

            if sample_due:
                samples += 1
                previous_progress = to_float(
                    row["current_progress_pct"],
                    initial_progress,
                )
                stable = (
                    bool(curve.get("valid"))
                    and not bool(curve.get("complete"))
                    and progress >= previous_progress - 0.15
                    and safety_score >= minimum_safety
                )
                if stable:
                    stable_samples += 1

            progress_delta = progress - initial_progress
            price_change = (
                (price / initial_price - 1) * 100
                if price > 0 and initial_price > 0
                else 0.0
            )
            stable_ratio = (
                stable_samples / max(samples, 1)
            )
            observation_age = (
                current_time - first_qualified
            ).total_seconds()

            fast_track = (
                progress_delta
                >= float(
                    config[
                        "qualification_fast_progress_delta_pct"
                    ]
                )
                and 0 <= price_change
                <= float(
                    config[
                        "qualification_fast_max_price_change_pct"
                    ]
                )
                and stable_ratio >= 0.85
                and samples
                >= int(config["qualification_min_samples"])
            )
            observation_required = (
                float(
                    config[
                        "qualification_fast_observation_seconds"
                    ]
                )
                if fast_track
                else float(
                    config[
                        "qualification_observation_seconds"
                    ]
                )
            )
            score = bonding_qualification_score(
                safety_score,
                progress_delta,
                stable_samples,
                price_change,
            )
            checks = {
                "observation": observation_age
                >= observation_required,
                "samples": samples
                >= int(config["qualification_min_samples"]),
                "stability": stable_ratio
                >= float(
                    config[
                        "qualification_min_stable_ratio"
                    ]
                ),
                "curve": bool(curve.get("valid")),
                "active": not bool(curve.get("complete")),
                "progress": float(
                    config["qualification_min_progress_pct"]
                )
                <= progress
                <= float(
                    config["qualification_max_progress_pct"]
                ),
                "delta": progress_delta
                >= float(
                    config[
                        "qualification_min_progress_delta_pct"
                    ]
                ),
                "price": abs(price_change)
                <= float(
                    config[
                        "qualification_max_price_change_pct"
                    ]
                ),
                "qualification": score
                >= float(config["qualification_min_score"]),
                "safety_fresh": age_seconds(
                    row["safety_assessed_at"]
                )
                <= float(
                    config[
                        "qualification_max_safety_age_seconds"
                    ]
                ),
            }

        if all(checks.values()):
            state = "READY"
            ready_at = (
                str(row["ready_at"])
                if row["ready_at"]
                else now_iso()
            )
            reason = (
                "Candidat DEX migré prêt pour position paper"
                if mode == "MIGRATED_DEX"
                else "Candidat bonding prêt pour position paper"
            )
        else:
            state = "OBSERVATION"
            ready_at = None
            missing = [
                key
                for key, passed in checks.items()
                if not passed
            ]
            reason = (
                f"{mode} — contrôles restants : "
                + ", ".join(missing)
            )

        upsert_candidate(
            connection,
            row,
            state=state,
            safety_score=safety_score,
            qualification_score=score,
            first_qualified_at=first_qualified.isoformat(),
            ready_at=ready_at,
            samples=samples,
            stable_samples=stable_samples,
            initial_progress=initial_progress,
            current_progress=progress,
            progress_delta=progress_delta,
            initial_price=initial_price,
            current_price=price,
            price_change=price_change,
            reason=reason,
            market_mode=mode,
        )
        record_transition(
            connection,
            mint,
            symbol,
            previous_state,
            state,
            score,
            reason,
        )


def sol_price_usd(
    connection: sqlite3.Connection,
) -> float:
    row = connection.execute(
        """
        SELECT value
        FROM bot_state
        WHERE key='sol_price_usd'
        """
    ).fetchone()
    return to_float(row["value"] if row else None)


def latest_launch(
    connection: sqlite3.Connection,
    mint: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM new_launches
        WHERE mint=?
        """,
        (mint,),
    ).fetchone()


def update_risk_state(
    connection: sqlite3.Connection,
    position_id: int,
    pnl_pct: float,
    config: dict[str, Any],
) -> tuple[float, bool, float]:
    row = connection.execute(
        """
        SELECT *
        FROM position_risk_state
        WHERE position_id=?
        """,
        (position_id,),
    ).fetchone()

    previous_peak = to_float(
        row["peak_pnl_pct"] if row else pnl_pct,
        pnl_pct,
    )
    peak = max(previous_peak, pnl_pct)
    armed = bool(
        row["break_even_armed"] if row else False
    ) or peak >= float(
        config["radar_break_even_trigger_pct"]
    )
    active_stop = (
        float(config["radar_break_even_floor_pct"])
        if armed
        else float(config["radar_stop_loss_pct"])
    )

    connection.execute(
        """
        INSERT INTO position_risk_state (
            position_id, peak_pnl_pct,
            break_even_armed, active_stop_pct,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(position_id) DO UPDATE SET
            peak_pnl_pct=excluded.peak_pnl_pct,
            break_even_armed=excluded.break_even_armed,
            active_stop_pct=excluded.active_stop_pct,
            updated_at=excluded.updated_at
        """,
        (
            position_id,
            peak,
            int(armed),
            active_stop,
            now_iso(),
        ),
    )
    return peak, armed, active_stop


def close_position(
    connection: sqlite3.Connection,
    position: sqlite3.Row,
    *,
    exit_value_before_network: float,
    exit_price_sol: float,
    impact_pct: float,
    exit_reason: str,
    cash: float,
    realized_total: float,
    lifecycle: str,
    config: dict[str, Any],
) -> tuple[float, float]:
    exit_fee = float(config["radar_exit_fee_sol"])
    exit_sol = max(
        0.0,
        exit_value_before_network - exit_fee,
    )
    entry_total = (
        to_float(position["entry_sol"])
        + to_float(position["entry_fees_sol"])
    )
    pnl_sol = exit_sol - entry_total
    pnl_pct = (
        pnl_sol / entry_total * 100
        if entry_total > 0
        else 0.0
    )
    cash += exit_sol
    realized_total += pnl_sol

    connection.execute(
        """
        UPDATE positions
        SET closed_at=?,
            exit_sol=?,
            exit_price_sol=?,
            current_price_sol=?,
            current_value_sol=?,
            exit_fees_sol=?,
            realized_pnl_sol=?,
            realized_pnl_pct=?,
            exit_reason=?,
            status='CLOSED'
        WHERE id=?
        """,
        (
            now_iso(),
            exit_sol,
            exit_price_sol,
            exit_price_sol,
            exit_sol,
            exit_fee,
            pnl_sol,
            pnl_pct,
            exit_reason,
            position["id"],
        ),
    )

    connection.execute(
        """
        INSERT INTO paper_orders (
            timestamp, token_mint, token_name,
            symbol, market_mode, side,
            requested_sol, expected_output,
            simulated_output, price_impact_pct,
            extra_slippage_pct, latency_ms,
            status, failure_reason
        )
        VALUES (?, ?, ?, ?, ?, 'SELL',
                ?, ?, ?, ?, ?, 850, 'FILLED', NULL)
        """,
        (
            now_iso(),
            position["token_mint"],
            position["token_name"],
            position["symbol"],
            position["market_mode"],
            exit_value_before_network,
            exit_value_before_network,
            exit_sol,
            impact_pct,
            impact_pct,
        ),
    )

    connection.execute(
        """
        INSERT INTO signals (
            timestamp, token_mint, token_name,
            symbol, lifecycle_state,
            decision, strategy, score,
            reasons_json
        )
        VALUES (?, ?, ?, ?, ?, 'SELL', ?, ?, ?)
        """,
        (
            now_iso(),
            position["token_mint"],
            position["token_name"],
            position["symbol"],
            lifecycle,
            str(position["strategy"] or STRICT_STRATEGY),
            0,
            json.dumps(
                [
                    exit_reason,
                    f"PnL paper {pnl_pct:+.2f}%",
                ],
                ensure_ascii=False,
            ),
        ),
    )

    candidate = connection.execute(
        """
        SELECT state, qualification_score
        FROM qualification_candidates
        WHERE mint=?
        """,
        (position["token_mint"],),
    ).fetchone()
    previous_state = (
        candidate["state"]
        if candidate
        else "PAPER_POSITION"
    )
    score = to_float(
        candidate["qualification_score"]
        if candidate
        else 0
    )

    connection.execute(
        """
        UPDATE qualification_candidates
        SET state='CLOSED',
            reason=?,
            updated_at=?
        WHERE mint=?
        """,
        (
            f"Position clôturée : {exit_reason}",
            now_iso(),
            position["token_mint"],
        ),
    )
    record_transition(
        connection,
        position["token_mint"],
        position["symbol"],
        previous_state,
        "CLOSED",
        score,
        f"Position clôturée : {exit_reason}",
    )
    if str(position["strategy"] or "") == PAPER_PILOT_STRATEGY:
        write_state(connection, "paper_pilot_last_exit", now_iso())
    return cash, realized_total


def update_open_positions(
    connection: sqlite3.Connection,
    positions: list[sqlite3.Row],
    curves: dict[str, dict[str, Any]],
    config: dict[str, Any],
    cash: float,
    realized_total: float,
) -> tuple[float, float]:
    sol_usd = sol_price_usd(connection)

    for position in positions:
        launch = latest_launch(
            connection,
            str(position["token_mint"]),
        )
        if not launch:
            continue

        current_mode = str(
            position["market_mode"] or "RADAR_BONDING"
        )
        market_mode = str(
            launch["market_mode"] or "BONDING"
        )

        # A position initially opened on the curve can continue after
        # migration instead of being closed mechanically.
        if (
            current_mode == "RADAR_BONDING"
            and market_mode == "MIGRATED_DEX"
            and launch["pair_address"]
        ):
            current_mode = "MIGRATED_DEX"
            connection.execute(
                """
                UPDATE positions
                SET market_mode='MIGRATED_DEX',
                    lifecycle_at_entry=
                        COALESCE(lifecycle_at_entry, 'BONDING'),
                    pair_address=?,
                    source_url=?
                WHERE id=?
                """,
                (
                    launch["pair_address"],
                    launch["pair_url"],
                    position["id"],
                ),
            )

        tokens = to_float(position["tokens_received"])
        price_sol = 0.0
        current_value = 0.0
        impact = 0.0
        lifecycle = str(
            launch["lifecycle_state"] or "UNKNOWN"
        )
        data_fresh = False

        if current_mode == "MIGRATED_DEX":
            price_sol = to_float(
                launch["market_price_sol"]
            )
            liquidity = to_float(
                launch["market_liquidity_usd"]
            )
            data_fresh = age_seconds(
                launch["market_last_updated_at"]
            ) <= float(
                config["hybrid_market_data_stale_seconds"]
            )
            current_value, impact = dex_sell_quote(
                price_sol,
                tokens,
                liquidity,
                sol_usd,
                int(config["hybrid_market_trade_fee_bps"]),
                int(config["hybrid_market_slippage_bps"]),
            )
        else:
            curve = curves.get(
                str(launch["bonding_curve"] or ""),
                {},
            )
            if curve and curve.get("valid"):
                price_sol = to_float(
                    curve.get("price_sol")
                )
                current_value, impact = (
                    constant_product_sell_quote(
                        curve,
                        tokens,
                        int(config["bonding_curve_fee_bps"]),
                    )
                )
                data_fresh = True

        if not data_fresh or current_value <= 0:
            continue

        entry_total = (
            to_float(position["entry_sol"])
            + to_float(position["entry_fees_sol"])
        )
        pnl_pct = (
            (current_value - entry_total)
            / entry_total
            * 100
            if entry_total > 0
            else 0.0
        )
        _, armed, _ = update_risk_state(
            connection,
            int(position["id"]),
            pnl_pct,
            config,
        )

        safety = connection.execute(
            """
            SELECT
                decision, hard_reject, analysis_status,
                top1_pct, mint_authority_revoked,
                freeze_authority_revoked
            FROM safety_assessments
            WHERE mint=?
            """,
            (position["token_mint"],),
        ).fetchone()

        opened_at = parse_datetime(position["opened_at"])
        age_minutes = (
            (now_utc() - opened_at).total_seconds() / 60
            if opened_at
            else 0.0
        )

        exit_reason: str | None = None
        position_strategy = str(position["strategy"] or "")
        position_is_acquisition = position_strategy in {
            ACQUISITION_STRATEGY,
            LEGACY_ACQUISITION_STRATEGY,
            PAPER_PILOT_STRATEGY,
        }
        position_is_pilot = position_strategy == PAPER_PILOT_STRATEGY
        pilot_safety_violation = False
        if safety and position_is_pilot:
            holder_limit = float(
                config.get(
                    "acquisition_mode_require_top1_max_pct",
                    config["safety_max_top1_pct"],
                )
            )
            top1 = safety["top1_pct"]
            complete_safety = (
                str(safety["analysis_status"] or "") == "COMPLETE"
            )
            pilot_safety_violation = (
                bool(safety["hard_reject"])
                or (
                    top1 is not None
                    and to_float(top1) > holder_limit
                )
                or (
                    complete_safety
                    and safety["mint_authority_revoked"] is not None
                    and not bool(safety["mint_authority_revoked"])
                )
                or (
                    complete_safety
                    and safety["freeze_authority_revoked"] is not None
                    and not bool(safety["freeze_authority_revoked"])
                )
            )

        if bool(launch["is_mayhem_mode"]) or bool(
            launch["mayhem_conflict"]
        ):
            exit_reason = "MAYHEM_EXCLUDED"
        elif pilot_safety_violation:
            exit_reason = "PAPER_PILOT_SAFETY_EXIT"
        elif safety and (
            bool(safety["hard_reject"])
            or (
                safety["decision"] == "REJECTED"
                and not position_is_acquisition
            )
        ):
            exit_reason = "SAFETY_DOWNGRADE"
        elif armed and pnl_pct <= float(
            config["radar_break_even_floor_pct"]
        ):
            exit_reason = "BREAK_EVEN_STOP"
        elif pnl_pct <= float(
            config["radar_stop_loss_pct"]
        ):
            exit_reason = "STOP_LOSS"
        elif pnl_pct >= float(
            config["radar_take_profit_pct"]
        ):
            exit_reason = "TAKE_PROFIT"
        elif (
            position_is_pilot
            and age_minutes * 60.0 >= float(
                config.get("paper_pilot_max_holding_seconds", 120)
            )
        ):
            exit_reason = "PAPER_PILOT_TIME_EXIT"
        elif (
            position_strategy in {
                ACQUISITION_STRATEGY,
                LEGACY_ACQUISITION_STRATEGY,
            }
            and age_minutes >= float(
                config.get("acquisition_mode_max_holding_minutes", 5)
            )
        ):
            exit_reason = "ACQUISITION_TIME_EXIT"
        elif (
            not position_is_acquisition
            and age_minutes >= float(config["radar_max_holding_minutes"])
        ):
            exit_reason = "TIME_EXIT"
        elif (
            current_mode == "RADAR_BONDING"
            and bool(launch["complete"])
            and market_mode != "MIGRATED_DEX"
        ):
            complete_time = (
                parse_datetime(launch["last_updated_at"])
                or now_utc()
            )
            migration_wait = (
                now_utc() - complete_time
            ).total_seconds()
            if migration_wait >= 180:
                exit_reason = "MIGRATION_TIMEOUT"

        connection.execute(
            """
            UPDATE positions
            SET market_mode=?,
                pair_address=COALESCE(?, pair_address),
                current_price_sol=?,
                current_price_usd=?,
                current_value_sol=?
            WHERE id=?
            """,
            (
                current_mode,
                launch["pair_address"],
                price_sol,
                (
                    price_sol * sol_usd
                    if sol_usd > 0
                    else None
                ),
                current_value,
                position["id"],
            ),
        )

        if exit_reason:
            cash, realized_total = close_position(
                connection,
                position,
                exit_value_before_network=current_value,
                exit_price_sol=price_sol,
                impact_pct=impact,
                exit_reason=exit_reason,
                cash=cash,
                realized_total=realized_total,
                lifecycle=lifecycle,
                config=config,
            )

    return cash, realized_total


def create_position(
    connection: sqlite3.Connection,
    candidate: sqlite3.Row,
    *,
    market_mode: str,
    tokens: float,
    entry_price: float,
    current_price: float,
    current_value: float,
    impact: float,
    entry_fee: float,
    position_size: float,
    sol_usd: float,
    source_url: str,
    liquidity_usd: float,
    config: dict[str, Any],
) -> int:
    lifecycle = (
        "MIGRATED"
        if market_mode == "MIGRATED_DEX"
        else "BONDING"
    )
    stored_mode = (
        "MIGRATED_DEX"
        if market_mode == "MIGRATED_DEX"
        else "RADAR_BONDING"
    )
    entry_mode = str(candidate["entry_mode"] or "STRICT")
    entry_strategy = (
        PAPER_PILOT_STRATEGY
        if entry_mode == "PAPER_PILOT"
        or str(candidate["state"]) == "PAPER_PILOT_READY"
        else ACQUISITION_STRATEGY
        if entry_mode == "FULL_ACQUISITION"
        or str(candidate["state"]) == "ACQUISITION_READY"
        else STRICT_STRATEGY
    )

    cursor = connection.execute(
        """
        INSERT INTO positions (
            token_mint, token_name, symbol,
            market_mode, lifecycle_at_entry,
            bonding_curve_address,
            pair_address, source_url,
            opened_at, closed_at,
            entry_sol, exit_sol,
            tokens_received, entry_price_sol,
            entry_price_usd, exit_price_sol,
            current_price_sol, current_price_usd,
            current_value_sol,
            entry_liquidity_usd,
            entry_market_cap_usd,
            entry_bonding_progress_pct,
            entry_fees_sol, exit_fees_sol,
            realized_pnl_sol, realized_pnl_pct,
            strategy, exit_reason, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?,
                NULL, ?, ?, 0, NULL, NULL, ?, NULL, 'OPEN')
        """,
        (
            candidate["mint"],
            candidate["token_name"],
            candidate["symbol"],
            stored_mode,
            lifecycle,
            candidate["bonding_curve"],
            candidate["pair_address"],
            source_url,
            now_iso(),
            position_size,
            tokens,
            entry_price,
            (
                entry_price * sol_usd
                if sol_usd > 0
                else None
            ),
            current_price,
            (
                current_price * sol_usd
                if sol_usd > 0
                else None
            ),
            current_value,
            liquidity_usd,
            (
                candidate["current_progress_pct"]
                if market_mode == "BONDING"
                else None
            ),
            entry_fee,
            entry_strategy,
        ),
    )
    position_id = int(cursor.lastrowid)
    entry_total = position_size + entry_fee
    initial_pnl = (
        (current_value - entry_total)
        / entry_total
        * 100
        if entry_total > 0
        else 0.0
    )

    connection.execute(
        """
        INSERT INTO position_risk_state (
            position_id, peak_pnl_pct,
            break_even_armed, active_stop_pct,
            updated_at
        )
        VALUES (?, ?, 0, ?, ?)
        """,
        (
            position_id,
            initial_pnl,
            float(config["radar_stop_loss_pct"]),
            now_iso(),
        ),
    )

    connection.execute(
        """
        INSERT INTO paper_orders (
            timestamp, token_mint, token_name,
            symbol, market_mode, side,
            requested_sol, expected_output,
            simulated_output, price_impact_pct,
            extra_slippage_pct, latency_ms,
            status, failure_reason
        )
        VALUES (?, ?, ?, ?, ?, 'BUY',
                ?, ?, ?, ?, ?, 850, 'FILLED', NULL)
        """,
        (
            now_iso(),
            candidate["mint"],
            candidate["token_name"],
            candidate["symbol"],
            stored_mode,
            position_size,
            tokens,
            tokens,
            impact,
            (
                impact
                + (
                    float(
                        config[
                            "hybrid_market_slippage_bps"
                        ]
                    )
                    / 100
                    if market_mode == "MIGRATED_DEX"
                    else impact
                )
            ),
        ),
    )

    connection.execute(
        """
        INSERT INTO signals (
            timestamp, token_mint, token_name,
            symbol, lifecycle_state,
            decision, strategy, score,
            reasons_json
        )
        VALUES (?, ?, ?, ?, ?, 'BUY', ?, ?, ?)
        """,
        (
            now_iso(),
            candidate["mint"],
            candidate["token_name"],
            candidate["symbol"],
            lifecycle,
            entry_strategy,
            to_float(candidate["qualification_score"]),
            json.dumps(
                [
                    (
                        "Paper Pilot — validation technique depuis CreateEvent"
                        if entry_strategy == PAPER_PILOT_STRATEGY
                        else "Acquisition complète — garde-fous validés"
                        if entry_strategy == ACQUISITION_STRATEGY
                        else "Stratégie stricte"
                    ),
                    (
                        "Mayhem bloqué; contrôles inconnus surveillés"
                        if entry_strategy == PAPER_PILOT_STRATEGY
                        else "Garde-fous Mayhem/holders/autorités validés"
                    ),
                    f"Marché {market_mode}",
                    (
                        f"Qualification "
                        f"{to_float(candidate['qualification_score']):.1f}/100"
                    ),
                ],
                ensure_ascii=False,
            ),
        ),
    )

    connection.execute(
        """
        UPDATE qualification_candidates
        SET state='PAPER_POSITION',
            position_id=?,
            reason=?,
            updated_at=?
        WHERE mint=?
        """,
        (
            position_id,
            (
                f"Position Paper Pilot {market_mode} ouverte"
                if entry_strategy == PAPER_PILOT_STRATEGY
                else f"Position acquisition complète {market_mode} ouverte"
                if entry_strategy == ACQUISITION_STRATEGY
                else f"Position paper {market_mode} ouverte"
            ),
            now_iso(),
            candidate["mint"],
        ),
    )
    record_transition(
        connection,
        str(candidate["mint"]),
        candidate["symbol"],
        str(candidate["state"]),
        "PAPER_POSITION",
        to_float(candidate["qualification_score"]),
        (
            f"Position Paper Pilot {market_mode} ouverte"
            if entry_strategy == PAPER_PILOT_STRATEGY
            else f"Position acquisition complète {market_mode} ouverte"
            if entry_strategy == ACQUISITION_STRATEGY
            else f"Position paper {market_mode} ouverte"
        ),
    )
    if entry_strategy in {ACQUISITION_STRATEGY, PAPER_PILOT_STRATEGY}:
        prefix = (
            "paper_pilot"
            if entry_strategy == PAPER_PILOT_STRATEGY
            else "acquisition_mode"
        )
        current_count_row = connection.execute(
            f"SELECT value FROM bot_state WHERE key='{prefix}_entries_count'"
        ).fetchone()
        current_count = to_int(
            current_count_row["value"]
            if current_count_row
            else 0
        )
        write_state(
            connection,
            f"{prefix}_entries_count",
            current_count + 1,
        )
        write_state(
            connection,
            f"{prefix}_last_entry",
            now_iso(),
        )
    return position_id


def open_ready_positions(
    connection: sqlite3.Connection,
    curves: dict[str, dict[str, Any]],
    config: dict[str, Any],
    cash: float,
) -> float:
    full_position_size = float(
        config.get(
            "acquisition_mode_position_size_sol",
            config["radar_position_size_sol"],
        )
    )
    pilot_position_size = float(
        config.get("paper_pilot_position_size_sol", 0.01)
    )
    max_positions = int(
        config["radar_max_open_positions"]
    )
    max_exposure = float(
        config["radar_max_total_exposure_sol"]
    )
    reserve = float(
        config["radar_min_cash_reserve_sol"]
    )
    entry_fee = float(config["radar_entry_fee_sol"])
    sol_usd = sol_price_usd(connection)

    open_rows = connection.execute(
        """
        SELECT *
        FROM positions
        WHERE status='OPEN'
        """
    ).fetchall()
    open_count = len(open_rows)
    exposure = sum(
        to_float(row["entry_sol"])
        for row in open_rows
    )
    open_mints = {
        str(row["token_mint"])
        for row in open_rows
    }

    ready = connection.execute(
        """
        SELECT candidates.*, launches.pair_url,
               launches.market_last_updated_at,
               launches.market_liquidity_usd,
               launches.market_price_sol,
               launches.market_price_usd,
               launches.market_cap_usd,
               launches.last_updated_at,
               launches.mayhem_conflict,
               launches.event_virtual_token_reserves_raw,
               launches.event_virtual_quote_reserves_raw,
               launches.event_real_token_reserves_raw,
               safety.analysis_status AS final_safety_status,
               safety.holder_analysis_status AS final_holder_status,
               safety.hard_reject AS final_hard_reject,
               safety.top1_pct AS final_top1_pct,
               safety.mint_authority_revoked AS final_mint_revoked,
               safety.freeze_authority_revoked AS final_freeze_revoked
        FROM qualification_candidates candidates
        JOIN new_launches launches
            ON launches.mint=candidates.mint
        LEFT JOIN safety_assessments safety
            ON safety.mint=candidates.mint
        WHERE candidates.state IN (
                'PAPER_PILOT_READY',
                'ACQUISITION_READY',
                'READY'
          )
          AND launches.is_mayhem_mode=0
          AND COALESCE(launches.mayhem_conflict, 0)=0
          AND COALESCE(candidates.is_mayhem_mode, 0)=0
          AND COALESCE(safety.hard_reject, 0)=0
        ORDER BY
            CASE
                WHEN candidates.state='ACQUISITION_READY' THEN 0
                WHEN candidates.state='PAPER_PILOT_READY' THEN 1
                ELSE 2
            END,
            datetime(candidates.ready_at) ASC,
            candidates.qualification_score DESC
        LIMIT 50
        """
    ).fetchall()

    for candidate in ready:
        mint = str(candidate["mint"])
        is_pilot = str(candidate["state"]) == "PAPER_PILOT_READY"
        position_size = (
            pilot_position_size if is_pilot else full_position_size
        )
        holder_limit = float(
            config.get(
                "acquisition_mode_require_top1_max_pct",
                config["safety_max_top1_pct"],
            )
        )
        final_top1 = candidate["final_top1_pct"]
        if bool(candidate["mayhem_conflict"]):
            continue
        if bool(candidate["final_hard_reject"]):
            continue
        if final_top1 is not None and to_float(final_top1) > holder_limit:
            continue
        if not is_pilot:
            if (
                str(candidate["final_safety_status"] or "") != "COMPLETE"
                or str(candidate["final_holder_status"] or "") != "COMPLETE"
                or not bool(candidate["final_mint_revoked"])
                or not bool(candidate["final_freeze_revoked"])
            ):
                continue
        elif str(candidate["final_safety_status"] or "") == "COMPLETE":
            if (
                candidate["final_mint_revoked"] is not None
                and not bool(candidate["final_mint_revoked"])
            ) or (
                candidate["final_freeze_revoked"] is not None
                and not bool(candidate["final_freeze_revoked"])
            ):
                continue
        if candidate["is_mayhem_mode"] is None:
            connection.execute(
                """
                UPDATE qualification_candidates
                SET state='PAUSED',
                    reason='Statut Mayhem non vérifié',
                    updated_at=?
                WHERE mint=?
                """,
                (now_iso(), mint),
            )
            continue
        if bool(candidate["is_mayhem_mode"]):
            connection.execute(
                """
                UPDATE qualification_candidates
                SET state='REJECTED',
                    reason='MAYHEM MODE — exclu définitivement',
                    updated_at=?
                WHERE mint=?
                """,
                (now_iso(), mint),
            )
            continue
        if mint in open_mints:
            continue
        if open_count >= max_positions:
            break
        if exposure + position_size > max_exposure:
            break
        if cash - position_size - entry_fee < reserve:
            break

        mode = str(
            candidate["market_mode"] or "BONDING"
        )
        tokens = 0.0
        entry_price = 0.0
        current_price = 0.0
        current_value = 0.0
        impact = 0.0
        liquidity = to_float(
            candidate["market_liquidity_usd"]
        )
        source_url = (
            str(candidate["pair_url"] or "")
            if mode == "MIGRATED_DEX"
            else f"https://pump.fun/coin/{mint}"
        )

        if mode == "MIGRATED_DEX":
            if age_seconds(
                candidate["market_last_updated_at"]
            ) > float(
                config["strategy_max_market_data_age_seconds"]
            ):
                connection.execute(
                    """
                    UPDATE qualification_candidates
                    SET state='PAUSED',
                        reason=?,
                        updated_at=?
                    WHERE mint=?
                    """,
                    (
                        "ENTRÉE BLOQUÉE — données DEX périmées",
                        now_iso(),
                        mint,
                    ),
                )
                continue

            current_price = to_float(
                candidate["market_price_sol"]
            )
            tokens, entry_price, impact = dex_buy_quote(
                current_price,
                position_size,
                liquidity,
                sol_usd,
                int(config["hybrid_market_trade_fee_bps"]),
                int(config["hybrid_market_slippage_bps"]),
            )
            current_value, _ = dex_sell_quote(
                current_price,
                tokens,
                liquidity,
                sol_usd,
                int(config["hybrid_market_trade_fee_bps"]),
                int(config["hybrid_market_slippage_bps"]),
            )
            if impact > float(
                config["hybrid_market_max_price_impact_pct"]
            ):
                connection.execute(
                    """
                    UPDATE qualification_candidates
                    SET state='PAUSED',
                        reason=?,
                        updated_at=?
                    WHERE mint=?
                    """,
                    (
                        f"Impact DEX simulé trop élevé : {impact:.2f}%",
                        now_iso(),
                        mint,
                    ),
                )
                continue
        else:
            if age_seconds(
                candidate["last_updated_at"]
            ) > float(
                config["strategy_max_launch_data_age_seconds"]
            ):
                connection.execute(
                    """
                    UPDATE qualification_candidates
                    SET state='PAUSED',
                        reason=?,
                        updated_at=?
                    WHERE mint=?
                    """,
                    (
                        "ENTRÉE BLOQUÉE — données bonding périmées",
                        now_iso(),
                        mint,
                    ),
                )
                continue

            curve = curves.get(
                str(candidate["bonding_curve"] or ""),
                {},
            )
            if (
                not curve
                or not curve.get("valid")
                or curve.get("complete")
            ):
                continue
            tokens, entry_price, impact = (
                constant_product_buy_quote(
                    curve,
                    position_size,
                    int(config["bonding_curve_fee_bps"]),
                )
            )
            current_value, _ = (
                constant_product_sell_quote(
                    curve,
                    tokens,
                    int(config["bonding_curve_fee_bps"]),
                )
            )
            current_price = to_float(
                curve.get("price_sol")
            )

        if (
            tokens <= 0
            or entry_price <= 0
            or current_value <= 0
        ):
            continue

        create_position(
            connection,
            candidate,
            market_mode=mode,
            tokens=tokens,
            entry_price=entry_price,
            current_price=current_price,
            current_value=current_value,
            impact=impact,
            entry_fee=entry_fee,
            position_size=position_size,
            sol_usd=sol_usd,
            source_url=source_url,
            liquidity_usd=liquidity,
            config=config,
        )

        cash -= position_size + entry_fee
        exposure += position_size
        open_count += 1
        open_mints.add(mint)

    return cash


def save_portfolio_snapshot(
    connection: sqlite3.Connection,
    cash: float,
    realized_total: float,
) -> None:
    positions = connection.execute(
        """
        SELECT *
        FROM positions
        WHERE status='OPEN'
        """
    ).fetchall()
    open_value = sum(
        to_float(row["current_value_sol"])
        for row in positions
    )
    entry_cost = sum(
        to_float(row["entry_sol"])
        + to_float(row["entry_fees_sol"])
        for row in positions
    )
    unrealized = open_value - entry_cost
    equity = cash + open_value

    connection.execute(
        """
        INSERT INTO portfolio_snapshots (
            timestamp, cash_sol,
            open_positions_value_sol,
            equity_sol, realized_pnl_sol,
            unrealized_pnl_sol
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            cash,
            open_value,
            equity,
            realized_total,
            unrealized,
        ),
    )
    connection.execute(
        """
        DELETE FROM portfolio_snapshots
        WHERE id NOT IN (
            SELECT id
            FROM portfolio_snapshots
            ORDER BY id DESC
            LIMIT 15000
        )
        """
    )


def update_pipeline_state(
    connection: sqlite3.Connection,
    status: str,
    error_text: str = "",
) -> None:
    counts = {
        str(row["state"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM qualification_candidates
            GROUP BY state
            """
        ).fetchall()
    }
    mode_counts = {
        str(row["market_mode"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT market_mode, COUNT(*) AS count
            FROM qualification_candidates
            GROUP BY market_mode
            """
        ).fetchall()
    }

    write_state(
        connection,
        "qualification_status",
        status,
    )
    write_state(
        connection,
        "qualification_last_cycle",
        now_iso(),
    )
    write_state(
        connection,
        "qualification_last_error",
        error_text[:500],
    )
    write_state(
        connection,
        "qualification_observation_count",
        counts.get("OBSERVATION", 0),
    )
    write_state(
        connection,
        "qualification_ready_count",
        counts.get("READY", 0)
        + counts.get("ACQUISITION_READY", 0)
        + counts.get("PAPER_PILOT_READY", 0),
    )
    write_state(
        connection,
        "paper_pilot_ready_count",
        counts.get("PAPER_PILOT_READY", 0),
    )
    write_state(
        connection,
        "qualification_open_count",
        counts.get("PAPER_POSITION", 0),
    )
    write_state(
        connection,
        "qualification_bonding_count",
        mode_counts.get("BONDING", 0),
    )
    write_state(
        connection,
        "qualification_migrated_count",
        mode_counts.get("MIGRATED_DEX", 0),
    )


def run_cycle(
    rpc_client: httpx.Client,
    config: dict[str, Any],
) -> None:
    sources = load_sources()

    curve_addresses = {
        str(row["bonding_curve"])
        for row in sources
        if candidate_market_mode(row) == "BONDING"
        and row["bonding_curve"]
    }

    with connect_db() as connection:
        open_positions = load_open_positions(connection)
    curve_addresses.update(
        str(row["bonding_curve"])
        for row in open_positions
        if str(row["market_mode"] or "")
        == "RADAR_BONDING"
        and row["bonding_curve"]
    )

    accounts: dict[str, dict[str, Any] | None] = {}
    rpc_error = ""
    try:
        accounts = fetch_curve_accounts(
            rpc_client,
            sorted(curve_addresses),
        )
    except Exception as error:
        rpc_error = str(error)[:300]

    curves = {
        address: decode_curve(account)
        for address, account in accounts.items()
    }
    if config.get("paper_pilot_allow_event_curve_fallback", True):
        for row in sources:
            if candidate_market_mode(row) != "BONDING":
                continue
            address = str(row["bonding_curve"] or "")
            current = curves.get(address, {})
            if not current.get("valid"):
                fallback = event_curve_from_row(row)
                if fallback.get("valid"):
                    curves[address] = fallback

    for position in open_positions:
        address = str(position["bonding_curve"] or "")
        current = curves.get(address, {})
        if not current.get("valid"):
            fallback = event_curve_from_row(position)
            if fallback.get("valid"):
                curves[address] = fallback

    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")

        synchronize_candidates(
            connection,
            sources,
            curves,
            config,
        )

        portfolio = latest_portfolio(connection)
        cash = to_float(portfolio["cash_sol"])
        realized = to_float(
            portfolio["realized_pnl_sol"]
        )

        open_positions = load_open_positions(connection)
        cash, realized = update_open_positions(
            connection,
            open_positions,
            curves,
            config,
            cash,
            realized,
        )
        cash = open_ready_positions(
            connection,
            curves,
            config,
            cash,
        )
        save_portfolio_snapshot(
            connection,
            cash,
            realized,
        )
        update_pipeline_state(
            connection,
            "DEGRADED" if rpc_error else "RUNNING",
            rpc_error,
        )
        write_state(
            connection,
            "strategy_curve_source",
            "EVENT_FALLBACK" if rpc_error else "RPC+EVENT_FALLBACK",
        )
        connection.commit()


def main() -> None:
    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    config = load_json(CONFIG_PATH)
    if not config.get("qualification_enabled", True):
        print("Le Strategy Engine est désactivé.")
        return

    rpc_url = os.getenv(
        "SOLANA_RPC_URL",
        str(config.get("solana_rpc_url")),
    )
    interval = float(
        config.get("qualification_scan_seconds", 5)
    )

    acquire_lock()
    print("=" * 76)
    print("SOLPULSE V12.2 — STABLE PAPER PILOT ENGINE")
    print("=" * 76)
    print("Deux voies : Paper Pilot rapide 0,01 SOL et acquisition complète 0,05 SOL.")
    print("Une seule position paper; Mayhem reste interdit.")
    print("SL -20 %, TP +100 %, break-even après +50 %.")
    print("Aucune transaction réelle.")
    print()

    rpc_client = httpx.Client(
        base_url=rpc_url,
        timeout=httpx.Timeout(float(config.get("qualification_rpc_timeout_seconds", 8))),
    )

    try:
        while running:
            started = time.monotonic()
            LOCK_PATH.touch(exist_ok=True)
            try:
                run_cycle(rpc_client, config)
                with connect_db() as connection:
                    portfolio = latest_portfolio(connection)
                    open_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM positions
                            WHERE status='OPEN'
                            """
                        ).fetchone()[0]
                    )
                    ready = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM qualification_candidates
                            WHERE state IN ('READY','ACQUISITION_READY','PAPER_PILOT_READY')
                            """
                        ).fetchone()[0]
                    )
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} | "
                    f"READY {ready} | positions {open_count} | "
                    f"équité {to_float(portfolio['equity_sol']):.4f} SOL"
                )
            except KeyboardInterrupt:
                break
            except Exception as error:
                message = str(error)
                print(f"Erreur Strategy V12 : {message}")
                try:
                    with connect_db() as connection:
                        update_pipeline_state(
                            connection,
                            "ERROR",
                            message,
                        )
                        connection.commit()
                except Exception:
                    pass

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    finally:
        rpc_client.close()
        try:
            with connect_db() as connection:
                update_pipeline_state(
                    connection,
                    "STOPPED",
                )
                connection.commit()
        except Exception:
            pass
        release_lock()
        print("Hybrid Strategy Engine arrêté proprement.")


if __name__ == "__main__":
    main()
