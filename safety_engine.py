
from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import struct
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from runtime_utils import connect_sqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
CONFIG_PATH = BASE_DIR / "config.json"
LOCK_PATH = BASE_DIR / "data" / "safety_engine.lock"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

BASE58_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)

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


def b58encode(value: bytes) -> str:
    zero_count = len(value) - len(value.lstrip(b"\x00"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * zero_count + encoded


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
            print("Le Safety Engine semble déjà fonctionner.")
            raise SystemExit(1)
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def rpc_call(
    client: httpx.Client,
    method: str,
    params: list[Any],
    config: dict[str, Any],
) -> tuple[Any, int]:
    retries = int(config.get("safety_rpc_retries", 4))
    backoff = float(
        config.get("safety_rpc_backoff_seconds", 0.7)
    )
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = client.post(
                "",
                json={
                    "jsonrpc": "2.0",
                    "id": attempt,
                    "method": method,
                    "params": params,
                },
                headers={"Content-Type": "application/json"},
            )

            if response.status_code == 429:
                raise RuntimeError("RPC_RATE_LIMIT_429")
            response.raise_for_status()
            payload = response.json()

            if payload.get("error"):
                error_text = str(payload["error"])
                if (
                    "-32005" in error_text
                    or "rate" in error_text.lower()
                    or "limit" in error_text.lower()
                ):
                    raise RuntimeError(
                        f"RPC_RATE_LIMIT: {error_text}"
                    )
                raise RuntimeError(
                    f"{method}: {payload['error']}"
                )

            return payload.get("result"), attempt

        except (
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.HTTPStatusError,
            RuntimeError,
        ) as error:
            last_error = error
            message = str(error).lower()
            retryable = (
                "rate" in message
                or "429" in message
                or "timeout" in message
                or "tempor" in message
                or "connection" in message
                or "502" in message
                or "503" in message
                or "504" in message
            )
            if not retryable or attempt >= retries:
                break
            time.sleep(backoff * attempt)

    raise RuntimeError(str(last_error or f"{method} failed"))


def fetch_multiple_accounts(
    client: httpx.Client,
    addresses: list[str],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any] | None], int]:
    if not addresses:
        return [], 0

    accounts: list[dict[str, Any] | None] = []
    attempts_total = 0

    for start in range(0, len(addresses), 100):
        chunk = addresses[start : start + 100]
        result, attempts = rpc_call(
            client,
            "getMultipleAccounts",
            [
                chunk,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                },
            ],
            config,
        )
        attempts_total += attempts
        values = (result or {}).get("value") or []
        accounts.extend(values)

    return accounts, attempts_total


def decode_coption_pubkey(
    raw: bytes,
    option_offset: int,
    key_offset: int,
) -> str | None:
    if key_offset + 32 > len(raw):
        return None
    option = struct.unpack_from("<I", raw, option_offset)[0]
    if option == 0:
        return None
    return b58encode(raw[key_offset : key_offset + 32])


def decode_mint_account(
    account: dict[str, Any] | None,
) -> dict[str, Any]:
    empty = {
        "exists": False,
        "parsed": False,
        "token_program": None,
        "decimals": None,
        "supply_raw": None,
        "mint_authority": None,
        "freeze_authority": None,
    }
    if not account:
        return empty

    try:
        raw = base64.b64decode(account["data"][0])
    except Exception:
        return {
            **empty,
            "exists": True,
            "token_program": account.get("owner"),
        }

    if len(raw) < 82:
        return {
            **empty,
            "exists": True,
            "token_program": account.get("owner"),
        }

    return {
        "exists": True,
        "parsed": True,
        "token_program": str(account.get("owner") or ""),
        "decimals": int(raw[44]),
        "supply_raw": str(
            struct.unpack_from("<Q", raw, 36)[0]
        ),
        "mint_authority": decode_coption_pubkey(
            raw,
            0,
            4,
        ),
        "freeze_authority": decode_coption_pubkey(
            raw,
            46,
            50,
        ),
    }


def decode_token_account(
    account: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not account:
        return None
    try:
        raw = base64.b64decode(account["data"][0])
    except Exception:
        return None
    if len(raw) < 72:
        return None
    return {
        "mint": b58encode(raw[0:32]),
        "owner": b58encode(raw[32:64]),
        "amount": struct.unpack_from("<Q", raw, 64)[0],
    }


def fetch_mint_infos(
    rpc_client: httpx.Client,
    mints: list[str],
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], int]:
    accounts, attempts = fetch_multiple_accounts(
        rpc_client,
        mints,
        config,
    )
    return {
        mint: decode_mint_account(account)
        for mint, account in zip(mints, accounts)
    }, attempts


def fetch_largest_accounts(
    rpc_client: httpx.Client,
    mint: str,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    result, attempts = rpc_call(
        rpc_client,
        "getTokenLargestAccounts",
        [
            mint,
            {
                "commitment": "confirmed",
            },
        ],
        config,
    )
    value = (result or {}).get("value")
    return (
        value if isinstance(value, list) else [],
        attempts,
    )


def concentration_from_accounts(
    largest: list[dict[str, Any]],
    token_accounts: dict[str, dict[str, Any] | None],
    *,
    mint: str,
    supply_raw: str | None,
    ignored_owner: str | None,
    ignored_token_account: str | None,
) -> dict[str, Any]:
    balances_by_owner: dict[str, int] = defaultdict(int)
    ignored_balance = 0
    valid_accounts = 0

    for largest_item in largest:
        address = str(largest_item.get("address") or "")
        decoded = token_accounts.get(address)
        if not decoded:
            continue
        if decoded["mint"] != mint:
            continue

        amount = to_int(
            decoded.get("amount")
            or largest_item.get("amount"),
            0,
        )
        owner = str(decoded.get("owner") or "")
        if amount <= 0 or not owner:
            continue

        valid_accounts += 1
        if (
            ignored_token_account
            and address == ignored_token_account
        ):
            ignored_balance += amount
        elif ignored_owner and owner == ignored_owner:
            ignored_balance += amount
        else:
            balances_by_owner[owner] += amount

    total_supply = max(to_int(supply_raw, 0), 1)
    ranked = sorted(
        balances_by_owner.values(),
        reverse=True,
    )

    def top_pct(number: int) -> float:
        return (
            sum(ranked[:number])
            / total_supply
            * 100.0
        )

    return {
        "top1_pct": top_pct(1),
        "top5_pct": top_pct(5),
        "top10_pct": top_pct(10),
        "top20_pct": top_pct(20),
        "distinct_top_owners": len(ranked),
        "curve_balance_pct": (
            ignored_balance / total_supply * 100.0
        ),
        "largest_accounts_count": valid_accounts,
    }


def launch_activity(
    launch: sqlite3.Row,
) -> dict[str, Any]:
    buys = to_int(launch["market_buys_5m"])
    sells = to_int(launch["market_sells_5m"])
    has_pair = bool(launch["pair_address"])

    return {
        "buys_5m": buys,
        "sells_5m": sells,
        "activity_source": (
            "DEX Screener"
            if has_pair
            else "Bonding curve sans paire DEX"
        ),
        "liquidity_usd": to_float(
            launch["market_liquidity_usd"]
        ),
        "volume_5m_usd": to_float(
            launch["market_volume_5m_usd"]
        ),
        "change_5m_pct": to_float(
            launch["market_change_5m_pct"]
        ),
    }


def count_creator_launches(
    connection: sqlite3.Connection,
    creator: str | None,
) -> int:
    if not creator:
        return 0
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM new_launches
            WHERE creator=?
            """,
            (creator,),
        ).fetchone()[0]
    )


def score_assessment(
    launch: sqlite3.Row,
    mint_info: dict[str, Any],
    concentration: dict[str, Any] | None,
    creator_launch_count: int,
    activity: dict[str, Any],
    config: dict[str, Any],
    *,
    holder_complete: bool,
    pool_exclusion_confirmed: bool,
) -> dict[str, Any]:
    score = 50.0
    reasons: list[str] = []
    warnings: list[str] = []
    hard_reject = False
    complete_analysis = True

    mayhem_raw = launch["is_mayhem_mode"]
    mayhem_known = mayhem_raw is not None
    is_mayhem = bool(mayhem_raw) if mayhem_known else False

    if is_mayhem:
        score = 0.0
        hard_reject = True
        warnings.append(
            "MAYHEM MODE détecté — exclusion définitive de la stratégie"
        )
    elif not mayhem_known:
        complete_analysis = False
        warnings.append(
            "Statut Mayhem non vérifié — entrée interdite par sécurité"
        )
    else:
        reasons.append("Mayhem Mode désactivé")

    if not mint_info.get("exists"):
        score -= 30
        warnings.append("Compte mint temporairement introuvable")
        complete_analysis = False
    elif not mint_info.get("parsed"):
        score -= 15
        warnings.append("Compte mint non décodé")
        complete_analysis = False
    else:
        if mint_info.get("mint_authority") is None:
            score += 12
            reasons.append("Mint authority révoquée")
        else:
            score -= 35
            hard_reject = True
            warnings.append("Mint authority encore active")

        if mint_info.get("freeze_authority") is None:
            score += 12
            reasons.append("Freeze authority révoquée")
        else:
            score -= 35
            hard_reject = True
            warnings.append("Freeze authority encore active")

        if mint_info.get("token_program") in {
            TOKEN_PROGRAM,
            TOKEN_2022_PROGRAM,
        }:
            score += 3
            reasons.append("Programme de token reconnu")
        else:
            score -= 8
            warnings.append("Programme de token non reconnu")

    market_mode = str(
        launch["market_mode"] or "BONDING"
    )
    is_migrated = market_mode == "MIGRATED_DEX"

    if is_migrated and not pool_exclusion_confirmed:
        complete_analysis = False
        warnings.append(
            "Token vault de la pool migrée non confirmé"
        )

    if not holder_complete or concentration is None:
        score -= 6
        complete_analysis = False
        warnings.append(
            "Analyse des holders en attente ou temporairement indisponible"
        )
    else:
        top1 = to_float(concentration.get("top1_pct"))
        top10 = to_float(concentration.get("top10_pct"))
        owners = to_int(
            concentration.get("distinct_top_owners")
        )

        max_top1 = float(config["safety_max_top1_pct"])
        if top1 > max_top1 + 1e-9:
            score -= 50
            hard_reject = True
            warnings.append(
                f"Un holder hors pool contrôle {top1:.2f}% "
                f"de la supply totale "
                f"(maximum {max_top1:.2f}%)"
            )
        elif top1 <= 1:
            score += 12
            reasons.append("Plus gros holder hors pool ≤ 1 %")
        elif top1 <= 2:
            score += 9
            reasons.append("Plus gros holder hors pool ≤ 2 %")
        else:
            score += 5
            reasons.append(
                f"Plus gros holder conforme : {top1:.2f}%"
            )

        if top10 <= 40:
            score += 8
            reasons.append("Top 10 correctement réparti")
        elif top10 <= 60:
            score += 2
            reasons.append("Top 10 acceptable")
        elif top10 <= 75:
            score -= 8
            warnings.append("Top 10 concentré")
        elif top10 > float(config["safety_max_top10_pct"]):
            score -= 25
            hard_reject = True
            warnings.append("Concentration Top 10 critique")
        else:
            score -= 15
            warnings.append("Top 10 très concentré")

        if owners >= 10:
            score += 7
            reasons.append("Au moins 10 gros propriétaires distincts")
        elif owners >= 5:
            score += 3
            reasons.append("Diversité de propriétaires moyenne")
        elif owners < 3:
            score -= 12
            warnings.append("Très peu de propriétaires importants")

    reasons.append(
        f"Créateur observé sur {creator_launch_count} lancement(s) "
        f"(informatif uniquement)"
    )

    buys = to_int(activity.get("buys_5m"))
    sells = to_int(activity.get("sells_5m"))
    liquidity = to_float(activity.get("liquidity_usd"))
    volume_5m = to_float(activity.get("volume_5m_usd"))
    change_5m = to_float(activity.get("change_5m_pct"))

    if is_migrated:
        if liquidity >= 50_000:
            score += 8
            reasons.append("Liquidité DEX ≥ 50 k$")
        elif liquidity >= 10_000:
            score += 4
            reasons.append("Liquidité DEX ≥ 10 k$")
        else:
            score -= 10
            warnings.append("Liquidité DEX faible")

        if volume_5m >= 5_000:
            score += 6
            reasons.append("Volume 5 min significatif")
        elif volume_5m >= 1_000:
            score += 3
            reasons.append("Volume 5 min présent")
        else:
            score -= 5
            warnings.append("Volume 5 min faible")

        if sells >= 2:
            score += 5
            reasons.append("Ventes réelles observées sur le DEX")
        elif buys >= 8 and sells == 0:
            score -= 8
            warnings.append("Achats DEX sans vente observée")

        if buys + sells >= 20:
            score += 4
            reasons.append("Activité DEX récente significative")

        if buys >= 8 and sells > 0 and buys / sells > 8:
            score -= 6
            warnings.append("Ratio achats/ventes extrême")

        if change_5m > 50:
            score -= 8
            warnings.append("Pump 5 min déjà très avancé")
        elif change_5m < -20:
            score -= 8
            warnings.append("Baisse 5 min importante")

        score += 3
        reasons.append("Coin migré avec paire DEX active")
    else:
        progress = to_float(launch["progress_pct"], -1)
        if 3 <= progress <= 75:
            score += 5
            reasons.append(
                "Progression de courbe dans une zone observable"
            )
        elif 0 <= progress < 1:
            score -= 4
            warnings.append("Courbe presque inactive")
        elif progress > 95:
            score -= 8
            warnings.append("Courbe proche de la migration")

        if launch["pair_address"]:
            if sells >= 2:
                score += 3
                reasons.append("Ventes DEX observées")
            if buys >= 10 and sells == 0:
                score -= 6
                warnings.append("Achats sans vente observée")

    if launch["uri"]:
        score += 2
        reasons.append("URI de métadonnées présente")
    else:
        score -= 3
        warnings.append("URI de métadonnées absente")

    if is_mayhem:
        score = 0.0
    score = max(0.0, min(100.0, score))

    if hard_reject:
        decision = "REJECTED"
    elif (
        complete_analysis
        and score >= float(config["safety_qualified_score"])
    ):
        decision = "QUALIFIED"
    elif complete_analysis and score < float(
        config["safety_observation_score"]
    ):
        decision = "REJECTED"
    else:
        decision = "OBSERVATION"

    return {
        "score": score,
        "decision": decision,
        "hard_reject": int(hard_reject),
        "reasons": reasons,
        "warnings": warnings,
        "analysis_status": (
            "COMPLETE"
            if complete_analysis
            else "PARTIAL"
        ),
        "buys_5m": buys,
        "sells_5m": sells,
        "activity_source": str(
            activity.get("activity_source") or "Indisponible"
        ),
    }


def select_provisional_targets(
    limit: int,
    partial_refresh_seconds: int,
    priority_slots: int = 20,
) -> list[sqlite3.Row]:
    threshold = (
        now_utc()
        - timedelta(seconds=partial_refresh_seconds)
    ).isoformat()
    priority_slots = max(0, min(priority_slots, limit))

    base_where = """
        FROM new_launches launches
        LEFT JOIN safety_assessments safety
            ON safety.mint = launches.mint
        WHERE (
                safety.mint IS NULL
                OR (
                    safety.analysis_status != 'COMPLETE'
                    AND datetime(safety.assessed_at) < datetime(?)
                )
                OR (
                    launches.market_last_updated_at IS NOT NULL
                    AND datetime(launches.market_last_updated_at)
                        > datetime(safety.assessed_at)
                )
        )
    """

    with connect_db() as connection:
        recent = connection.execute(
            f"""
            SELECT
                launches.*,
                safety.assessed_at AS previous_assessed_at,
                safety.analysis_status AS previous_analysis_status,
                safety.holder_analysis_status
            {base_where}
              AND datetime(launches.detected_at)
                  >= datetime('now', '-60 seconds')
            ORDER BY datetime(launches.detected_at) DESC
            LIMIT ?
            """,
            (threshold, priority_slots),
        ).fetchall()

        recent_mints = {str(row["mint"]) for row in recent}
        fair_pool = connection.execute(
            f"""
            SELECT
                launches.*,
                safety.assessed_at AS previous_assessed_at,
                safety.analysis_status AS previous_analysis_status,
                safety.holder_analysis_status
            {base_where}
            ORDER BY
                CASE WHEN safety.mint IS NULL THEN 0 ELSE 1 END,
                CASE
                    WHEN safety.mint IS NULL
                    THEN datetime(launches.detected_at)
                    ELSE datetime(safety.assessed_at)
                END ASC
            LIMIT ?
            """,
            (threshold, max(limit * 3, limit)),
        ).fetchall()

    combined = list(recent)
    for row in fair_pool:
        if str(row["mint"]) in recent_mints:
            continue
        combined.append(row)
        if len(combined) >= limit:
            break
    return combined[:limit]

def select_full_targets(
    limit: int,
    refresh_seconds: int,
    priority_slots: int = 2,
) -> list[sqlite3.Row]:
    threshold = (
        now_utc()
        - timedelta(seconds=refresh_seconds)
    ).isoformat()
    priority_slots = max(0, min(priority_slots, limit))

    base_where = """
        FROM new_launches launches
        JOIN safety_assessments safety
            ON safety.mint = launches.mint
        WHERE safety.hard_reject = 0
          AND launches.is_mayhem_mode = 0
          AND COALESCE(launches.mayhem_conflict, 0) = 0
          AND (
                safety.holder_analysis_status
                    NOT IN ('COMPLETE', 'STALE_FALLBACK')
                OR safety.last_holder_success_at IS NULL
                OR datetime(safety.last_holder_success_at)
                    < datetime(?)
          )
          AND (
                safety.next_retry_at IS NULL
                OR datetime(safety.next_retry_at)
                    <= datetime('now')
          )
    """

    with connect_db() as connection:
        recent = connection.execute(
            f"""
            SELECT launches.*, safety.*
            {base_where}
              AND datetime(launches.detected_at)
                  >= datetime('now', '-60 seconds')
            ORDER BY datetime(launches.detected_at) DESC
            LIMIT ?
            """,
            (threshold, priority_slots),
        ).fetchall()

        recent_mints = {str(row["mint"]) for row in recent}
        fair_pool = connection.execute(
            f"""
            SELECT launches.*, safety.*
            {base_where}
            ORDER BY
                CASE
                    WHEN safety.holder_analysis_status='PENDING'
                    THEN 0
                    WHEN safety.holder_analysis_status='ERROR'
                    THEN 1
                    WHEN safety.holder_analysis_status='PARTIAL'
                    THEN 2
                    ELSE 3
                END,
                CASE
                    WHEN safety.last_holder_success_at IS NULL
                    THEN datetime(launches.detected_at)
                    ELSE datetime(safety.last_holder_success_at)
                END ASC
            LIMIT ?
            """,
            (threshold, max(limit * 3, limit)),
        ).fetchall()

    combined = list(recent)
    for row in fair_pool:
        if str(row["mint"]) in recent_mints:
            continue
        combined.append(row)
        if len(combined) >= limit:
            break
    return combined[:limit]

def previous_concentration(
    mint: str,
) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM safety_assessments
            WHERE mint=?
              AND last_holder_success_at IS NOT NULL
            """,
            (mint,),
        ).fetchone()

    if not row:
        return None

    return {
        "top1_pct": row["top1_pct"],
        "top5_pct": row["top5_pct"],
        "top10_pct": row["top10_pct"],
        "top20_pct": row["top20_pct"],
        "distinct_top_owners": row[
            "distinct_top_owners"
        ],
        "curve_balance_pct": row["curve_balance_pct"],
        "largest_accounts_count": row[
            "largest_accounts_count"
        ],
    }


def save_assessment(
    launch: sqlite3.Row,
    mint_info: dict[str, Any],
    concentration: dict[str, Any] | None,
    creator_count: int,
    scored: dict[str, Any],
    *,
    holder_status: str,
    concentration_source: str,
    provisional: bool,
    rpc_attempts: int,
    error_text: str = "",
    last_holder_success_at: str | None = None,
    next_retry_at: str | None = None,
) -> None:
    concentration = concentration or {}
    timestamp = now_iso()
    last_success_at = (
        timestamp
        if scored["analysis_status"] in {"COMPLETE", "PARTIAL"}
        else None
    )

    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO safety_assessments (
                assessed_at, launch_id, mint, creator,
                symbol, token_name, lifecycle_state,
                progress_pct, safety_score, decision,
                hard_reject, reasons_json, warnings_json,
                mint_account_exists, token_program,
                decimals, supply_raw, mint_authority,
                freeze_authority, mint_authority_revoked,
                freeze_authority_revoked, top1_pct,
                top5_pct, top10_pct, top20_pct,
                distinct_top_owners, curve_balance_pct,
                largest_accounts_count,
                creator_launch_count, buys_5m,
                sells_5m, activity_source,
                analysis_status, error_text,
                is_mayhem_mode, market_mode, pair_address,
                ignored_pool_token_account,
                holder_analysis_status,
                concentration_source,
                provisional_score, rpc_attempts,
                last_success_at, last_holder_success_at,
                next_retry_at, assessment_version
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, 'V12'
            )
            ON CONFLICT(mint) DO UPDATE SET
                assessed_at=excluded.assessed_at,
                launch_id=excluded.launch_id,
                creator=excluded.creator,
                symbol=excluded.symbol,
                token_name=excluded.token_name,
                lifecycle_state=excluded.lifecycle_state,
                progress_pct=excluded.progress_pct,
                safety_score=excluded.safety_score,
                decision=excluded.decision,
                hard_reject=excluded.hard_reject,
                reasons_json=excluded.reasons_json,
                warnings_json=excluded.warnings_json,
                mint_account_exists=excluded.mint_account_exists,
                token_program=excluded.token_program,
                decimals=excluded.decimals,
                supply_raw=excluded.supply_raw,
                mint_authority=excluded.mint_authority,
                freeze_authority=excluded.freeze_authority,
                mint_authority_revoked=excluded.mint_authority_revoked,
                freeze_authority_revoked=excluded.freeze_authority_revoked,
                top1_pct=COALESCE(excluded.top1_pct, top1_pct),
                top5_pct=COALESCE(excluded.top5_pct, top5_pct),
                top10_pct=COALESCE(excluded.top10_pct, top10_pct),
                top20_pct=COALESCE(excluded.top20_pct, top20_pct),
                distinct_top_owners=COALESCE(
                    excluded.distinct_top_owners,
                    distinct_top_owners
                ),
                curve_balance_pct=COALESCE(
                    excluded.curve_balance_pct,
                    curve_balance_pct
                ),
                largest_accounts_count=COALESCE(
                    excluded.largest_accounts_count,
                    largest_accounts_count
                ),
                creator_launch_count=excluded.creator_launch_count,
                buys_5m=excluded.buys_5m,
                sells_5m=excluded.sells_5m,
                activity_source=excluded.activity_source,
                analysis_status=excluded.analysis_status,
                error_text=excluded.error_text,
                is_mayhem_mode=excluded.is_mayhem_mode,
                market_mode=excluded.market_mode,
                pair_address=excluded.pair_address,
                ignored_pool_token_account=
                    excluded.ignored_pool_token_account,
                holder_analysis_status=
                    excluded.holder_analysis_status,
                concentration_source=
                    excluded.concentration_source,
                provisional_score=excluded.provisional_score,
                rpc_attempts=excluded.rpc_attempts,
                last_success_at=COALESCE(
                    excluded.last_success_at,
                    last_success_at
                ),
                last_holder_success_at=COALESCE(
                    excluded.last_holder_success_at,
                    last_holder_success_at
                ),
                next_retry_at=excluded.next_retry_at,
                assessment_version='V12'
            """,
            (
                timestamp,
                launch["id"],
                launch["mint"],
                launch["creator"],
                launch["symbol"],
                launch["name"],
                launch["lifecycle_state"],
                launch["progress_pct"],
                scored["score"],
                scored["decision"],
                scored["hard_reject"],
                json.dumps(
                    scored["reasons"],
                    ensure_ascii=False,
                ),
                json.dumps(
                    scored["warnings"],
                    ensure_ascii=False,
                ),
                int(bool(mint_info.get("exists"))),
                mint_info.get("token_program"),
                mint_info.get("decimals"),
                mint_info.get("supply_raw"),
                mint_info.get("mint_authority"),
                mint_info.get("freeze_authority"),
                int(
                    mint_info.get("exists")
                    and mint_info.get("parsed")
                    and mint_info.get("mint_authority") is None
                ),
                int(
                    mint_info.get("exists")
                    and mint_info.get("parsed")
                    and mint_info.get("freeze_authority") is None
                ),
                concentration.get("top1_pct"),
                concentration.get("top5_pct"),
                concentration.get("top10_pct"),
                concentration.get("top20_pct"),
                concentration.get("distinct_top_owners"),
                concentration.get("curve_balance_pct"),
                concentration.get("largest_accounts_count"),
                creator_count,
                scored["buys_5m"],
                scored["sells_5m"],
                scored["activity_source"],
                scored["analysis_status"],
                (
                    "MAYHEM_MODE_EXCLUDED"
                    if bool(launch["is_mayhem_mode"])
                    else error_text[:1000]
                ),
                launch["is_mayhem_mode"],
                launch["market_mode"],
                launch["pair_address"],
                launch["pool_base_token_account"],
                holder_status,
                concentration_source,
                int(provisional),
                rpc_attempts,
                last_success_at,
                last_holder_success_at,
                next_retry_at,
            ),
        )
        connection.commit()


def run_provisional_phase(
    rpc_client: httpx.Client,
    config: dict[str, Any],
) -> int:
    targets = select_provisional_targets(
        int(config["safety_provisional_batch_size"]),
        int(config["safety_partial_refresh_seconds"]),
        priority_slots=min(
            20,
            int(config["safety_provisional_batch_size"]),
        ),
    )
    if not targets:
        return 0

    mints = [str(row["mint"]) for row in targets]
    provisional_error = ""
    attempts = 0
    try:
        mint_infos, attempts = fetch_mint_infos(
            rpc_client,
            mints,
            config,
        )
    except Exception as error:
        # Even with a fully unavailable public RPC, V11 persists a
        # visible numeric provisional score instead of leaving the
        # dashboard empty.
        provisional_error = str(error)
        mint_infos = {
            mint: {
                "exists": False,
                "parsed": False,
                "token_program": None,
                "decimals": None,
                "supply_raw": None,
                "mint_authority": None,
                "freeze_authority": None,
            }
            for mint in mints
        }

    completed = 0
    with connect_db() as connection:
        creator_counts = {
            str(row["mint"]): count_creator_launches(
                connection,
                row["creator"],
            )
            for row in targets
        }

    for launch in targets:
        mint = str(launch["mint"])
        mint_info = mint_infos.get(
            mint,
            {
                "exists": False,
                "parsed": False,
                "token_program": None,
                "decimals": None,
                "supply_raw": None,
                "mint_authority": None,
                "freeze_authority": None,
            },
        )
        previous = previous_concentration(mint)
        holder_complete = previous is not None
        is_migrated = (
            str(launch["market_mode"] or "")
            == "MIGRATED_DEX"
        )
        pool_confirmed = (
            not is_migrated
            or bool(launch["pool_base_token_account"])
        )

        scored = score_assessment(
            launch,
            mint_info,
            previous,
            creator_counts[mint],
            launch_activity(launch),
            config,
            holder_complete=holder_complete,
            pool_exclusion_confirmed=pool_confirmed,
        )
        holder_status = (
            "STALE_FALLBACK"
            if previous
            else "PENDING"
        )
        source = (
            "previous_complete"
            if previous
            else "pending_rpc"
        )

        save_assessment(
            launch,
            mint_info,
            previous,
            creator_counts[mint],
            scored,
            holder_status=holder_status,
            concentration_source=source,
            provisional=True,
            rpc_attempts=attempts,
            error_text=provisional_error,
            last_holder_success_at=None,
            next_retry_at=(
                (
                    now_utc() + timedelta(seconds=30)
                ).isoformat()
                if provisional_error
                else None
            ),
        )
        completed += 1

    return completed


def run_full_phase(
    rpc_client: httpx.Client,
    config: dict[str, Any],
) -> int:
    targets = select_full_targets(
        int(config["safety_full_batch_size"]),
        int(config["safety_refresh_seconds"]),
        priority_slots=int(
            config.get("safety_new_priority_slots", 2)
        ),
    )
    if not targets:
        return 0

    mints = [str(row["mint"]) for row in targets]

    mint_attempts = 0
    mint_batch_error = ""
    try:
        mint_infos, mint_attempts = fetch_mint_infos(
            rpc_client,
            mints,
            config,
        )
    except Exception as error:
        mint_batch_error = str(error)
        mint_infos = {
            str(row["mint"]): {
                "exists": bool(row["mint_account_exists"]),
                "parsed": bool(row["mint_account_exists"]),
                "token_program": row["token_program"],
                "decimals": row["decimals"],
                "supply_raw": row["supply_raw"],
                "mint_authority": row["mint_authority"],
                "freeze_authority": row["freeze_authority"],
            }
            for row in targets
        }

    largest_by_mint: dict[str, list[dict[str, Any]]] = {}
    attempts_by_mint: dict[str, int] = {}
    errors: dict[str, str] = {}

    worker_count = max(
        1,
        min(
            int(config.get("safety_full_parallel_workers", 6)),
            len(targets),
        ),
    )

    def fetch_one(
        launch: sqlite3.Row,
    ) -> tuple[str, list[dict[str, Any]], int, str]:
        mint = str(launch["mint"])
        try:
            largest, attempts = fetch_largest_accounts(
                rpc_client,
                mint,
                config,
            )
            return mint, largest, attempts, ""
        except Exception as error:
            return mint, [], 0, str(error)

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="safety-holder",
    ) as executor:
        futures = {
            executor.submit(fetch_one, launch): launch
            for launch in targets
        }
        for future in as_completed(futures):
            mint, largest, attempts, error_text = future.result()
            attempts_by_mint[mint] = attempts + mint_attempts
            if error_text:
                errors[mint] = error_text
            else:
                largest_by_mint[mint] = largest

    if mint_batch_error:
        for mint in mints:
            if mint not in errors:
                errors[mint] = mint_batch_error

    all_addresses: list[str] = []
    for largest in largest_by_mint.values():
        for item in largest:
            address = str(item.get("address") or "")
            if address and address not in all_addresses:
                all_addresses.append(address)

    decoded_accounts: dict[
        str,
        dict[str, Any] | None,
    ] = {}
    if all_addresses:
        try:
            accounts, owner_attempts = fetch_multiple_accounts(
                rpc_client,
                all_addresses,
                config,
            )
            decoded_accounts = {
                address: decode_token_account(account)
                for address, account in zip(
                    all_addresses,
                    accounts,
                )
            }
            for mint in attempts_by_mint:
                attempts_by_mint[mint] += owner_attempts
        except Exception as error:
            for mint in largest_by_mint:
                errors[mint] = str(error)

    with connect_db() as connection:
        creator_counts = {
            str(row["mint"]): count_creator_launches(
                connection,
                row["creator"],
            )
            for row in targets
        }

    completed = 0
    for launch in targets:
        mint = str(launch["mint"])
        mint_info = mint_infos.get(
            mint,
            {
                "exists": False,
                "parsed": False,
                "token_program": None,
                "decimals": None,
                "supply_raw": None,
                "mint_authority": None,
                "freeze_authority": None,
            },
        )

        error_text = errors.get(mint, "")
        if error_text:
            previous = previous_concentration(mint)
            if previous and bool(
                config.get(
                    "safety_preserve_last_complete",
                    True,
                )
            ):
                scored = score_assessment(
                    launch,
                    mint_info,
                    previous,
                    creator_counts[mint],
                    launch_activity(launch),
                    config,
                    holder_complete=True,
                    pool_exclusion_confirmed=(
                        str(launch["market_mode"] or "")
                        != "MIGRATED_DEX"
                        or bool(
                            launch["pool_base_token_account"]
                        )
                    ),
                )
                scored["warnings"].append(
                    "Dernière concentration valide conservée "
                    "après erreur RPC"
                )
                holder_status = "STALE_FALLBACK"
                source = "last_complete_fallback"
            else:
                scored = score_assessment(
                    launch,
                    mint_info,
                    None,
                    creator_counts[mint],
                    launch_activity(launch),
                    config,
                    holder_complete=False,
                    pool_exclusion_confirmed=False,
                )
                holder_status = "ERROR"
                source = "rpc_error"

            current_attempts = attempts_by_mint.get(mint, 0)
            retry_delay = min(
                60,
                5 + max(0, current_attempts - 1) * 5,
            )
            retry_at = (
                now_utc() + timedelta(seconds=retry_delay)
            ).isoformat()
            save_assessment(
                launch,
                mint_info,
                previous,
                creator_counts[mint],
                scored,
                holder_status=holder_status,
                concentration_source=source,
                provisional=True,
                rpc_attempts=attempts_by_mint.get(mint, 0),
                error_text=error_text,
                next_retry_at=retry_at,
            )
            continue

        largest = largest_by_mint.get(mint, [])
        is_migrated = (
            str(launch["market_mode"] or "")
            == "MIGRATED_DEX"
        )
        ignored_owner = (
            None
            if is_migrated
            else str(launch["bonding_curve"] or "")
        )
        ignored_token_account = (
            str(launch["pool_base_token_account"] or "")
            if is_migrated
            else None
        )
        pool_confirmed = (
            not is_migrated
            or bool(ignored_token_account)
        )

        concentration = concentration_from_accounts(
            largest,
            decoded_accounts,
            mint=mint,
            supply_raw=mint_info.get("supply_raw"),
            ignored_owner=ignored_owner,
            ignored_token_account=ignored_token_account,
        )
        holder_complete = (
            concentration["largest_accounts_count"] > 0
            and concentration["distinct_top_owners"] > 0
            and pool_confirmed
        )

        scored = score_assessment(
            launch,
            mint_info,
            concentration,
            creator_counts[mint],
            launch_activity(launch),
            config,
            holder_complete=holder_complete,
            pool_exclusion_confirmed=pool_confirmed,
        )

        save_assessment(
            launch,
            mint_info,
            concentration,
            creator_counts[mint],
            scored,
            holder_status=(
                "COMPLETE"
                if holder_complete
                else "PARTIAL"
            ),
            concentration_source=(
                "solana_largest_accounts_raw"
            ),
            provisional=not holder_complete,
            rpc_attempts=attempts_by_mint.get(mint, 0),
            error_text=(
                ""
                if holder_complete
                else "Données holders incomplètes"
            ),
            last_holder_success_at=(
                now_iso()
                if holder_complete
                else None
            ),
        )
        completed += int(holder_complete)

        print(
            f"{datetime.now().strftime('%H:%M:%S')} | "
            f"{launch['symbol']} | "
            f"{scored['score']:.1f}/100 | "
            f"{scored['decision']} | "
            f"{'FULL' if holder_complete else 'PARTIAL'}"
        )

    return completed


def update_engine_state(
    status: str,
    error_text: str = "",
) -> None:
    with connect_db() as connection:
        counts = {
            str(row["analysis_status"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT analysis_status, COUNT(*) AS count
                FROM safety_assessments
                GROUP BY analysis_status
                """
            ).fetchall()
        }
        provisional_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM safety_assessments
                WHERE provisional_score=1
                """
            ).fetchone()[0]
        )
        qualified_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM safety_assessments
                WHERE decision='QUALIFIED'
                """
            ).fetchone()[0]
        )
        mayhem_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM safety_assessments
                WHERE is_mayhem_mode=1
                """
            ).fetchone()[0]
        )
        pending_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM new_launches launches
                LEFT JOIN safety_assessments safety
                    ON safety.mint=launches.mint
                WHERE launches.is_mayhem_mode = 0
                  AND (
                        safety.mint IS NULL
                        OR safety.holder_analysis_status
                            NOT IN ('COMPLETE', 'STALE_FALLBACK')
                  )
                """
            ).fetchone()[0]
        )
        oldest_pending_row = connection.execute(
            """
            SELECT MIN(launches.detected_at)
            FROM new_launches launches
            LEFT JOIN safety_assessments safety
                ON safety.mint=launches.mint
            WHERE launches.is_mayhem_mode = 0
              AND (
                    safety.mint IS NULL
                    OR safety.holder_analysis_status
                        NOT IN ('COMPLETE', 'STALE_FALLBACK')
              )
            """
        ).fetchone()
        oldest_pending_time = parse_datetime(
            oldest_pending_row[0]
            if oldest_pending_row
            else None
        )
        oldest_pending_age = (
            int(
                max(
                    0.0,
                    (
                        now_utc() - oldest_pending_time
                    ).total_seconds(),
                )
            )
            if oldest_pending_time
            else 0
        )
        starved_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM new_launches launches
                LEFT JOIN safety_assessments safety
                    ON safety.mint=launches.mint
                WHERE launches.is_mayhem_mode = 0
                  AND (
                        safety.mint IS NULL
                        OR safety.holder_analysis_status
                            NOT IN ('COMPLETE', 'STALE_FALLBACK')
                  )
                  AND datetime(launches.detected_at)
                      < datetime('now', '-45 seconds')
                """
            ).fetchone()[0]
        )

        write_state(connection, "safety_status", status)
        write_state(connection, "safety_last_scan", now_iso())
        write_state(
            connection,
            "safety_last_error",
            error_text[:500],
        )
        write_state(
            connection,
            "safety_assessed_count",
            sum(counts.values()),
        )
        write_state(
            connection,
            "safety_qualified_count",
            qualified_count,
        )
        write_state(
            connection,
            "mayhem_excluded_count",
            mayhem_count,
        )
        write_state(
            connection,
            "safety_complete_count",
            counts.get("COMPLETE", 0),
        )
        write_state(
            connection,
            "safety_partial_count",
            counts.get("PARTIAL", 0),
        )
        write_state(
            connection,
            "safety_error_count",
            counts.get("ERROR", 0),
        )
        write_state(
            connection,
            "safety_provisional_count",
            provisional_count,
        )
        write_state(
            connection,
            "safety_queue_pending",
            pending_count,
        )
        write_state(
            connection,
            "safety_oldest_pending_age_seconds",
            oldest_pending_age,
        )
        write_state(
            connection,
            "safety_starved_count",
            starved_count,
        )
        worker_config = load_json(CONFIG_PATH)
        write_state(
            connection,
            "safety_full_parallel_workers",
            int(
                worker_config.get(
                    "safety_full_parallel_workers",
                    2,
                )
            ),
        )
        if status == "RUNNING" and not error_text:
            write_state(
                connection,
                "safety_last_success",
                now_iso(),
            )
        if "rate" in error_text.lower() or "429" in error_text:
            row = connection.execute(
                """
                SELECT value
                FROM bot_state
                WHERE key='safety_rpc_rate_limited'
                """
            ).fetchone()
            write_state(
                connection,
                "safety_rpc_rate_limited",
                to_int(row["value"] if row else 0) + 1,
            )
        connection.commit()


def run_cycle(
    rpc_client: httpx.Client,
    config: dict[str, Any],
) -> tuple[int, int]:
    provisional = run_provisional_phase(
        rpc_client,
        config,
    )
    full = run_full_phase(
        rpc_client,
        config,
    )
    update_engine_state("RUNNING")
    return provisional, full


def main() -> None:
    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    config = load_json(CONFIG_PATH)
    if not config.get("safety_engine_enabled", True):
        print("Le Safety Engine est désactivé.")
        return

    rpc_url = os.getenv(
        "SOLANA_RPC_URL",
        str(config.get("solana_rpc_url")),
    )
    interval = float(
        config.get("safety_scan_seconds", 5)
    )

    acquire_lock()
    print("=" * 76)
    print("SOLPULSE V12.2 — PRIORITY SAFETY")
    print("=" * 76)
    print("Deux voies : nouveaux coins immédiats et anciens coins protégés contre la famine.")
    print("Décodage RPC brut pour SPL Token et Token-2022.")
    print("Les dernières analyses valides survivent aux erreurs RPC.")
    print("Bonding et DEX sont analysés; tout coin Mayhem est rejeté.")
    print()

    rpc_client = httpx.Client(
        base_url=rpc_url,
        timeout=httpx.Timeout(
            float(config.get("safety_rpc_timeout_seconds", 8))
        ),
        limits=httpx.Limits(
            max_connections=max(
                8,
                int(
                    config.get(
                        "safety_full_parallel_workers",
                        6,
                    )
                )
                + 2,
            ),
            max_keepalive_connections=8,
        ),
    )

    try:
        update_engine_state("RUNNING")
        while running:
            started = time.monotonic()
            LOCK_PATH.touch(exist_ok=True)

            try:
                provisional, full = run_cycle(
                    rpc_client,
                    config,
                )
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} | "
                    f"{provisional} score(s) rapide(s) | "
                    f"{full} analyse(s) holder complète(s)"
                )
            except KeyboardInterrupt:
                break
            except Exception as error:
                message = str(error)
                print(f"Erreur Safety V12 : {message}")
                update_engine_state("ERROR", message)

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    finally:
        rpc_client.close()
        try:
            update_engine_state("STOPPED")
        except Exception:
            pass
        release_lock()
        print("Safety Recovery Engine arrêté proprement.")


if __name__ == "__main__":
    main()
