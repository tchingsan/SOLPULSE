
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_utils import connect_sqlite
from urllib.parse import urlparse, urlunparse

import httpx
import websockets

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
CONFIG_PATH = BASE_DIR / "config.json"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
LOCK_PATH = BASE_DIR / "data" / "new_coin_radar.lock"

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GLOBAL_ACCOUNT = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

CREATE_EVENT_DISCRIMINATOR = bytes(
    [27, 114, 169, 77, 222, 235, 99, 118]
)
BONDING_CURVE_DISCRIMINATOR = bytes(
    [23, 183, 248, 55, 96, 216, 172, 96]
)
GLOBAL_DISCRIMINATOR = hashlib.sha256(
    b"account:Global"
).digest()[:8]

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
FALLBACK_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000

BASE58_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)

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
        age = datetime.now().timestamp() - LOCK_PATH.stat().st_mtime
        if age < 30:
            print("Le New Coin Radar semble déjà fonctionner.")
            sys.exit(1)
        LOCK_PATH.unlink(missing_ok=True)

    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def b58encode(value: bytes) -> str:
    zero_count = len(value) - len(value.lstrip(b"\x00"))
    number = int.from_bytes(value, "big")
    encoded = ""

    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded

    return "1" * zero_count + encoded


def read_borsh_string(
    payload: bytes,
    offset: int,
) -> tuple[str, int]:
    if offset + 4 > len(payload):
        raise ValueError("Longueur Borsh absente.")

    length = struct.unpack_from("<I", payload, offset)[0]
    offset += 4

    if length > 16_384 or offset + length > len(payload):
        raise ValueError("Chaîne Borsh invalide.")

    raw = payload[offset : offset + length]
    return raw.decode("utf-8", errors="replace"), offset + length


def decode_create_event(
    payload: bytes,
) -> dict[str, Any] | None:
    """Decode the current Pump CreateEvent directly from logs.

    Current events expose the Mayhem flag at creation time. Legacy create
    events predate Mayhem, so they are safely classified as non-Mayhem.
    Appended quote fields are decoded when present and otherwise ignored.
    """
    if len(payload) < 8 or payload[:8] != CREATE_EVENT_DISCRIMINATOR:
        return None

    offset = 8
    name, offset = read_borsh_string(payload, offset)
    symbol, offset = read_borsh_string(payload, offset)
    uri, offset = read_borsh_string(payload, offset)

    # Legacy/current common layout:
    # mint, bonding_curve, user, creator,
    # timestamp, virtual token/quote reserves,
    # real token reserves, token total supply.
    common_size = 32 * 4 + 8 * 5
    if offset + common_size > len(payload):
        raise ValueError("CreateEvent incomplet.")

    mint = b58encode(payload[offset : offset + 32])
    offset += 32
    bonding_curve = b58encode(payload[offset : offset + 32])
    offset += 32
    user = b58encode(payload[offset : offset + 32])
    offset += 32
    creator = b58encode(payload[offset : offset + 32])
    offset += 32

    timestamp = struct.unpack_from("<q", payload, offset)[0]
    offset += 8
    virtual_token_reserves = struct.unpack_from(
        "<Q",
        payload,
        offset,
    )[0]
    offset += 8
    virtual_quote_reserves = struct.unpack_from(
        "<Q",
        payload,
        offset,
    )[0]
    offset += 8
    real_token_reserves = struct.unpack_from(
        "<Q",
        payload,
        offset,
    )[0]
    offset += 8
    token_total_supply = struct.unpack_from(
        "<Q",
        payload,
        offset,
    )[0]
    offset += 8

    token_program = ""
    is_mayhem_mode = 0
    is_cashback_enabled = 0
    create_event_version = "LEGACY"
    quote_mint = ""
    appended_virtual_quote_reserves: int | None = None

    # create_v2 adds token_program + the two creation flags.
    if offset + 34 <= len(payload):
        token_program = b58encode(payload[offset : offset + 32])
        offset += 32
        is_mayhem_mode = int(bool(payload[offset]))
        offset += 1
        is_cashback_enabled = int(bool(payload[offset]))
        offset += 1
        create_event_version = "CREATE_V2"

    # New quote-pool fields are append-only.
    if offset + 40 <= len(payload):
        quote_mint = b58encode(payload[offset : offset + 32])
        offset += 32
        appended_virtual_quote_reserves = struct.unpack_from(
            "<Q",
            payload,
            offset,
        )[0]
        create_event_version = "CREATE_V2_QUOTE"

    effective_virtual_quote = (
        appended_virtual_quote_reserves
        if appended_virtual_quote_reserves is not None
        else virtual_quote_reserves
    )

    # This is the creation event: progress starts at zero.
    # The current BondingCurve account replaces this value on confirmation.
    progress_pct = 0.0

    curve_price_sol = None
    if virtual_token_reserves > 0:
        curve_price_sol = (
            effective_virtual_quote / LAMPORTS_PER_SOL
        ) / (
            virtual_token_reserves / (10**TOKEN_DECIMALS)
        )

    return {
        "name": name[:256],
        "symbol": symbol[:64],
        "uri": uri[:2048],
        "mint": mint,
        "bonding_curve": bonding_curve,
        "user": user,
        "creator": creator,
        "timestamp": timestamp,
        "virtual_token_reserves": virtual_token_reserves,
        "virtual_quote_reserves": effective_virtual_quote,
        "real_token_reserves": real_token_reserves,
        "token_total_supply": token_total_supply,
        "token_program": token_program,
        "is_mayhem_mode": is_mayhem_mode,
        "is_cashback_enabled": is_cashback_enabled,
        "quote_mint": quote_mint,
        "create_event_version": create_event_version,
        "progress_pct": progress_pct,
        "curve_price_sol": curve_price_sol,
        # CreateEvent exposes virtual quote reserves, not the current
        # real quote reserve. Avoid presenting the virtual value as real.
        "real_quote_reserves_sol": None,
    }

def extract_create_events(
    logs: list[str] | None,
) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []

    for log in logs or []:
        if not log.startswith("Program data: "):
            continue

        encoded = log.removeprefix("Program data: ").strip()
        try:
            payload = base64.b64decode(encoded, validate=True)
            event = decode_create_event(payload)
        except (ValueError, struct.error):
            event = None

        if event:
            events.append(event)

    return events


def safe_detected_at(block_time: int | None) -> str:
    if block_time:
        try:
            return datetime.fromtimestamp(
                block_time,
                tz=timezone.utc,
            ).isoformat()
        except (ValueError, OSError):
            pass
    return now_iso()


def watchlist_mints() -> set[str]:
    try:
        watchlist = load_json(WATCHLIST_PATH)
        return {
            str(token["address"])
            for token in watchlist.get("tokens", [])
            if token.get("address")
        }
    except Exception:
        return set()


def save_launches(
    signature: str,
    slot: int | None,
    logs: list[str] | None,
    block_time: int | None = None,
) -> int:
    events = extract_create_events(logs)
    if not events:
        return 0

    added = 0
    known_watchlist = watchlist_mints()

    with connect_db() as connection:
        for event_index, event in enumerate(events):
            existed = connection.execute(
                "SELECT 1 FROM new_launches WHERE mint = ?",
                (event["mint"],),
            ).fetchone()

            connection.execute(
                """
                INSERT INTO new_launches (
                    detected_at, last_updated_at,
                    slot, signature, event_index,
                    mint, name, symbol, uri,
                    bonding_curve, creator,
                    lifecycle_state, progress_pct,
                    curve_price_sol, curve_price_usd,
                    real_quote_reserves_sol,
                    complete, account_exists, rpc_status,
                    is_in_watchlist, source,
                    is_mayhem_mode, mayhem_checked_at,
                    exclusion_reason, mayhem_source,
                    mayhem_conflict,
                    create_event_version, event_checked_at,
                    event_detection_latency_ms,
                    event_token_program,
                    event_token_total_supply_raw,
                    event_virtual_token_reserves_raw,
                    event_real_token_reserves_raw,
                    curve_confirmed, curve_confirmed_at,
                    event_is_cashback_enabled,
                    event_quote_mint,
                    event_virtual_quote_reserves_raw
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'BONDING_EVENT', ?, ?, ?, ?, 0, 0,
                        'EVENT_ONLY', ?, 'Pump CreateEvent',
                        ?, ?, ?, 'CREATE_EVENT', 0,
                        ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    name = excluded.name,
                    symbol = excluded.symbol,
                    uri = excluded.uri,
                    bonding_curve = excluded.bonding_curve,
                    creator = excluded.creator,
                    lifecycle_state = excluded.lifecycle_state,
                    progress_pct = COALESCE(
                        excluded.progress_pct,
                        new_launches.progress_pct
                    ),
                    curve_price_sol = COALESCE(
                        excluded.curve_price_sol,
                        new_launches.curve_price_sol
                    ),
                    curve_price_usd = COALESCE(
                        excluded.curve_price_usd,
                        new_launches.curve_price_usd
                    ),
                    real_quote_reserves_sol = COALESCE(
                        excluded.real_quote_reserves_sol,
                        new_launches.real_quote_reserves_sol
                    ),
                    is_mayhem_mode = excluded.is_mayhem_mode,
                    mayhem_checked_at = excluded.mayhem_checked_at,
                    exclusion_reason = excluded.exclusion_reason,
                    mayhem_source = 'CREATE_EVENT',
                    create_event_version = excluded.create_event_version,
                    event_checked_at = excluded.event_checked_at,
                    event_detection_latency_ms =
                        excluded.event_detection_latency_ms,
                    event_token_program = excluded.event_token_program,
                    event_token_total_supply_raw =
                        excluded.event_token_total_supply_raw,
                    event_virtual_token_reserves_raw =
                        excluded.event_virtual_token_reserves_raw,
                    event_real_token_reserves_raw =
                        excluded.event_real_token_reserves_raw,
                    event_is_cashback_enabled =
                        excluded.event_is_cashback_enabled,
                    event_quote_mint = excluded.event_quote_mint,
                    event_virtual_quote_reserves_raw =
                        excluded.event_virtual_quote_reserves_raw,
                    is_in_watchlist = excluded.is_in_watchlist
                """,
                (
                    safe_detected_at(block_time),
                    now_iso(),
                    slot,
                    signature,
                    event_index,
                    event["mint"],
                    event["name"],
                    event["symbol"],
                    event["uri"],
                    event["bonding_curve"],
                    event["creator"],
                    event["progress_pct"],
                    event["curve_price_sol"],
                    None,
                    event["real_quote_reserves_sol"],
                    int(event["mint"] in known_watchlist),
                    event["is_mayhem_mode"],
                    now_iso(),
                    (
                        "MAYHEM_MODE_EXCLUDED"
                        if event["is_mayhem_mode"]
                        else None
                    ),
                    event["create_event_version"],
                    now_iso(),
                    max(
                        0.0,
                        (
                            datetime.now(timezone.utc).timestamp()
                            - float(event["timestamp"])
                        )
                        * 1000.0,
                    ),
                    event["token_program"],
                    str(event["token_total_supply"]),
                    str(event["virtual_token_reserves"]),
                    str(event["real_token_reserves"]),
                    event["is_cashback_enabled"],
                    event["quote_mint"],
                    str(event["virtual_quote_reserves"]),
                ),
            )
            if not existed:
                added += 1

        total = connection.execute(
            "SELECT COUNT(*) FROM new_launches"
        ).fetchone()[0]
        write_state(connection, "radar_last_event", now_iso())
        write_state(
            connection,
            "radar_launches_detected",
            str(total),
        )
        connection.commit()

    return added


def websocket_url(http_url: str) -> str:
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=scheme))


async def rpc_call(
    client: httpx.AsyncClient,
    method: str,
    params: list[Any],
) -> Any:
    response = await client.post(
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


def decode_u64(data: bytes, offset: int) -> int:
    if offset + 8 > len(data):
        raise ValueError("Compte trop court.")
    return struct.unpack_from("<Q", data, offset)[0]


def decode_global_initial_reserves(
    account: dict[str, Any] | None,
) -> int:
    if not account:
        return FALLBACK_INITIAL_REAL_TOKEN_RESERVES

    try:
        raw = base64.b64decode(account["data"][0])
        if len(raw) < 97 or raw[:8] != GLOBAL_DISCRIMINATOR:
            return FALLBACK_INITIAL_REAL_TOKEN_RESERVES

        value = decode_u64(raw, 89)
        return value or FALLBACK_INITIAL_REAL_TOKEN_RESERVES
    except Exception:
        return FALLBACK_INITIAL_REAL_TOKEN_RESERVES


def decode_curve_account(
    account: dict[str, Any] | None,
    initial_real_reserves: int,
    sol_price_usd: float,
) -> dict[str, Any]:
    empty = {
        "account_exists": 0,
        "lifecycle_state": "NO_ACCOUNT",
        "progress_pct": None,
        "curve_price_sol": None,
        "curve_price_usd": None,
        "real_quote_reserves_sol": None,
        "complete": None,
        "is_mayhem_mode": None,
        "rpc_status": "NO_ACCOUNT",
    }

    if not account:
        return empty

    try:
        raw = base64.b64decode(account["data"][0])
        owner_valid = account.get("owner") == PUMP_PROGRAM_ID

        if len(raw) < 49:
            return {
                **empty,
                "account_exists": 1,
                "rpc_status": "ACCOUNT_TOO_SHORT",
            }

        discriminator_valid = raw[:8] == BONDING_CURVE_DISCRIMINATOR
        virtual_token = decode_u64(raw, 8)
        virtual_quote = decode_u64(raw, 16)
        real_token = decode_u64(raw, 24)
        real_quote = decode_u64(raw, 32)
        complete = int(bool(raw[48]))
        # Pump BondingCurve layout:
        # discriminator 8 + reserves/supply 40 + complete 1
        # + creator pubkey 32 = byte 81 for is_mayhem_mode.
        is_mayhem_mode = (
            int(bool(raw[81]))
            if len(raw) >= 82
            else None
        )

        progress = (
            1.0 - real_token / max(initial_real_reserves, 1)
        ) * 100.0
        progress = max(0.0, min(100.0, progress))

        curve_price_sol = None
        if virtual_token > 0:
            curve_price_sol = (
                virtual_quote / LAMPORTS_PER_SOL
            ) / (
                virtual_token / (10**TOKEN_DECIMALS)
            )

        return {
            "account_exists": 1,
            "lifecycle_state": (
                "CURVE_COMPLETE" if complete else "BONDING"
            ),
            "progress_pct": progress,
            "curve_price_sol": curve_price_sol,
            "curve_price_usd": (
                curve_price_sol * sol_price_usd
                if curve_price_sol and sol_price_usd
                else None
            ),
            "real_quote_reserves_sol": (
                real_quote / LAMPORTS_PER_SOL
            ),
            "complete": complete,
            "is_mayhem_mode": is_mayhem_mode,
            "rpc_status": (
                "OK"
                if owner_valid and discriminator_valid
                else "UNVERIFIED_ACCOUNT"
            ),
        }
    except Exception:
        return {
            **empty,
            "account_exists": 1,
            "rpc_status": "DECODE_ERROR",
        }


async def fetch_accounts(
    client: httpx.AsyncClient,
    addresses: list[str],
) -> list[dict[str, Any] | None]:
    result = await rpc_call(
        client,
        "getMultipleAccounts",
        [
            addresses,
            {
                "encoding": "base64",
                "commitment": "confirmed",
            },
        ],
    )
    return (result or {}).get("value", [])


async def enrich_recent_launches(
    rpc_client: httpx.AsyncClient,
    track_limit: int,
) -> None:
    with connect_db() as connection:
        launches = connection.execute(
            """
            SELECT
                id,
                mint,
                bonding_curve,
                is_mayhem_mode,
                mayhem_conflict,
                last_updated_at,
                detected_at
            FROM new_launches
            ORDER BY
                CASE
                    WHEN is_mayhem_mode IS NULL THEN 0
                    WHEN last_updated_at IS NULL THEN 1
                    WHEN datetime(last_updated_at)
                        < datetime('now', '-15 seconds')
                    THEN 2
                    ELSE 3
                END,
                CASE
                    WHEN is_mayhem_mode IS NULL
                    THEN datetime(detected_at)
                    ELSE datetime(last_updated_at)
                END ASC,
                datetime(detected_at) DESC
            LIMIT ?
            """,
            (track_limit,),
        ).fetchall()

        sol_row = connection.execute(
            "SELECT value FROM bot_state WHERE key='sol_price_usd'"
        ).fetchone()
        sol_price_usd = (
            float(sol_row["value"])
            if sol_row and sol_row["value"]
            else 0.0
        )

    if not launches:
        return

    global_accounts = await fetch_accounts(
        rpc_client,
        [GLOBAL_ACCOUNT],
    )
    initial_reserves = decode_global_initial_reserves(
        global_accounts[0] if global_accounts else None
    )

    launch_by_curve = {
        row["bonding_curve"]: row
        for row in launches
    }
    curves = list(launch_by_curve)
    decoded_by_id: dict[int, dict[str, Any]] = {}

    for start in range(0, len(curves), 100):
        chunk = curves[start : start + 100]
        accounts = await fetch_accounts(rpc_client, chunk)

        for curve, account in zip(chunk, accounts):
            row = launch_by_curve[curve]
            decoded_by_id[row["id"]] = decode_curve_account(
                account,
                initial_reserves,
                sol_price_usd,
            )

    current_watchlist = watchlist_mints()

    with connect_db() as connection:
        for row in launches:
            decoded = decoded_by_id.get(row["id"])
            if not decoded:
                continue

            connection.execute(
                """
                UPDATE new_launches
                SET last_updated_at = ?,
                    lifecycle_state = ?,
                    progress_pct = ?,
                    curve_price_sol = ?,
                    curve_price_usd = ?,
                    real_quote_reserves_sol = ?,
                    complete = ?,
                    account_exists = ?,
                    rpc_status = ?,
                    curve_confirmed = CASE
                        WHEN ? = 'OK' THEN 1
                        ELSE curve_confirmed
                    END,
                    curve_confirmed_at = CASE
                        WHEN ? = 'OK' THEN ?
                        ELSE curve_confirmed_at
                    END,
                    mayhem_conflict = CASE
                        WHEN ? IS NOT NULL
                         AND is_mayhem_mode IS NOT NULL
                         AND ? != is_mayhem_mode
                        THEN 1
                        ELSE mayhem_conflict
                    END,
                    is_mayhem_mode = CASE
                        WHEN ? IS NOT NULL
                         AND is_mayhem_mode IS NOT NULL
                         AND ? != is_mayhem_mode
                        THEN 1
                        ELSE COALESCE(?, is_mayhem_mode)
                    END,
                    mayhem_checked_at = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE mayhem_checked_at
                    END,
                    mayhem_source = CASE
                        WHEN ? IS NOT NULL
                        THEN 'CREATE_EVENT+CURVE'
                        ELSE mayhem_source
                    END,
                    exclusion_reason = CASE
                        WHEN ? IS NOT NULL
                         AND is_mayhem_mode IS NOT NULL
                         AND ? != is_mayhem_mode
                        THEN 'MAYHEM_STATUS_CONFLICT'
                        WHEN ? = 1
                        THEN 'MAYHEM_MODE_EXCLUDED'
                        WHEN ? = 0
                        THEN NULL
                        ELSE exclusion_reason
                    END,
                    is_in_watchlist = ?
                WHERE id = ?
                """,
                (
                    now_iso(),
                    decoded["lifecycle_state"],
                    decoded["progress_pct"],
                    decoded["curve_price_sol"],
                    decoded["curve_price_usd"],
                    decoded["real_quote_reserves_sol"],
                    decoded["complete"],
                    decoded["account_exists"],
                    decoded["rpc_status"],
                    decoded["rpc_status"],
                    decoded["rpc_status"],
                    now_iso(),
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    now_iso(),
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    decoded["is_mayhem_mode"],
                    int(row["mint"] in current_watchlist),
                    row["id"],
                ),
            )

        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN is_mayhem_mode = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN is_mayhem_mode = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN is_mayhem_mode IS NULL THEN 1 ELSE 0 END)
            FROM new_launches
            """
        ).fetchone()
        write_state(
            connection,
            "mayhem_detected_count",
            str(int(counts[0] or 0)),
        )
        write_state(
            connection,
            "mayhem_excluded_count",
            str(int(counts[0] or 0)),
        )
        write_state(
            connection,
            "mayhem_unknown_count",
            str(int(counts[2] or 0)),
        )
        immediate_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM new_launches
            WHERE mayhem_source LIKE 'CREATE_EVENT%'
              AND is_mayhem_mode IS NOT NULL
            """
        ).fetchone()[0]
        conflict_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM new_launches
            WHERE mayhem_conflict=1
            """
        ).fetchone()[0]
        write_state(
            connection,
            "event_mayhem_immediate_count",
            str(int(immediate_count or 0)),
        )
        write_state(
            connection,
            "event_mayhem_conflict_count",
            str(int(conflict_count or 0)),
        )
        connection.commit()


async def backfill_recent_transactions(
    rpc_client: httpx.AsyncClient,
    limit: int,
) -> int:
    if limit <= 0:
        return 0

    signatures = await rpc_call(
        rpc_client,
        "getSignaturesForAddress",
        [
            PUMP_PROGRAM_ID,
            {
                "limit": min(limit, 50),
                "commitment": "confirmed",
            },
        ],
    )

    added = 0
    for item in reversed(signatures or []):
        signature = item.get("signature")
        if not signature or item.get("err"):
            continue

        transaction = await rpc_call(
            rpc_client,
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if not transaction:
            continue

        logs = (
            (transaction.get("meta") or {}).get("logMessages")
            or []
        )
        added += save_launches(
            signature=signature,
            slot=transaction.get("slot"),
            logs=logs,
            block_time=transaction.get("blockTime"),
        )
        await asyncio.sleep(0.08)

    return added


async def subscribe_and_listen(
    ws_url: str,
    rpc_client: httpx.AsyncClient,
    commitment: str,
    enrichment_seconds: float,
    track_limit: int,
) -> None:
    async with websockets.connect(
        ws_url,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=4_000_000,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {
                            "mentions": [PUMP_PROGRAM_ID],
                        },
                        {
                            "commitment": commitment,
                        },
                    ],
                }
            )
        )

        confirmation = json.loads(await websocket.recv())
        if "error" in confirmation:
            raise RuntimeError(
                f"logsSubscribe: {confirmation['error']}"
            )

        with connect_db() as connection:
            write_state(connection, "radar_status", "RUNNING")
            write_state(connection, "radar_last_error", "")
            connection.commit()

        print(
            f"Abonnement logsSubscribe actif : "
            f"{confirmation.get('result')}"
        )

        loop = asyncio.get_running_loop()
        next_enrichment = loop.time()
        rpc_cooldown_until = 0.0

        while running:
            timeout = max(
                0.5,
                min(10.0, next_enrichment - loop.time()),
            )

            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raw_message = None

            if raw_message:
                message = json.loads(raw_message)

                if message.get("method") == "logsNotification":
                    result = (
                        message.get("params", {})
                        .get("result", {})
                    )
                    context = result.get("context", {})
                    value = result.get("value", {})

                    if not value.get("err"):
                        added = save_launches(
                            signature=str(value.get("signature", "")),
                            slot=context.get("slot"),
                            logs=value.get("logs") or [],
                        )
                        if added:
                            print(
                                f"{datetime.now().strftime('%H:%M:%S')} | "
                                f"{added} nouveau(x) lancement(s)"
                            )

            if loop.time() >= next_enrichment:
                if loop.time() < rpc_cooldown_until:
                    next_enrichment = min(
                        rpc_cooldown_until,
                        loop.time() + enrichment_seconds,
                    )
                else:
                    try:
                        await enrich_recent_launches(
                            rpc_client,
                            track_limit,
                        )
                        with connect_db() as connection:
                            write_state(
                                connection,
                                "radar_status",
                                "RUNNING",
                            )
                            write_state(
                                connection,
                                "radar_rpc_status",
                                "OK",
                            )
                            write_state(
                                connection,
                                "radar_last_error",
                                "",
                            )
                            write_state(
                                connection,
                                "radar_last_enrichment",
                                now_iso(),
                            )
                            connection.commit()
                    except Exception as error:
                        message = str(error)
                        cooldown = (
                            30
                            if "429" in message
                            or "rate" in message.lower()
                            else 15
                        )
                        rpc_cooldown_until = (
                            loop.time() + cooldown
                        )
                        concise = (
                            "RPC Solana limité (429), pause automatique"
                            if cooldown == 30
                            else f"Enrichissement RPC reporté : "
                            f"{type(error).__name__}"
                        )
                        print(concise)
                        with connect_db() as connection:
                            write_state(
                                connection,
                                "radar_rpc_status",
                                "COOLDOWN",
                            )
                            write_state(
                                connection,
                                "radar_last_error",
                                concise,
                            )
                            write_state(
                                connection,
                                "radar_rpc_cooldown_seconds",
                                cooldown,
                            )
                            connection.commit()

                    next_enrichment = (
                        loop.time() + enrichment_seconds
                    )


async def main() -> None:
    global running

    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    config = load_json(CONFIG_PATH)
    if not config.get("radar_enabled", True):
        print("Le radar est désactivé dans config.json.")
        return

    rpc_url = os.getenv(
        "SOLANA_RPC_URL",
        str(config.get("solana_rpc_url")),
    )
    configured_ws = str(
        config.get("solana_ws_url") or ""
    ).strip()
    ws_url = configured_ws or websocket_url(rpc_url)

    commitment = str(
        config.get("radar_commitment", "confirmed")
    )
    backfill_limit = int(
        config.get("radar_backfill_signatures", 15)
    )
    track_limit = int(
        config.get("radar_track_limit", 100)
    )
    enrichment_seconds = float(
        config.get("radar_enrichment_seconds", 10)
    )

    acquire_lock()

    print("=" * 70)
    print("SOLPULSE V12.2 — RADAR ÉVÉNEMENTIEL STABLE")
    print("=" * 70)
    print(f"WebSocket : {ws_url}")
    print(f"RPC HTTP  : {rpc_url}")
    print("Mayhem et réserves initiales sont disponibles depuis CreateEvent; la courbe RPC devient une confirmation, pas un prérequis au paper pilot.")
    print("Le radar transmet immédiatement les données au mode acquisition paper.")
    print()

    rpc_client = httpx.AsyncClient(
        base_url=rpc_url,
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(
            max_connections=4,
            max_keepalive_connections=4,
        ),
    )

    try:
        try:
            added = await backfill_recent_transactions(
                rpc_client,
                backfill_limit,
            )
            print(
                f"Backfill terminé : "
                f"{added} lancement(s) ajouté(s)."
            )
        except Exception as error:
            print(f"Backfill non disponible : {error}")

        reconnect_delay = 2.0

        while running:
            try:
                await subscribe_and_listen(
                    ws_url=ws_url,
                    rpc_client=rpc_client,
                    commitment=commitment,
                    enrichment_seconds=enrichment_seconds,
                    track_limit=track_limit,
                )
                reconnect_delay = 2.0
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                running = False
            except Exception as error:
                message = str(error)
                print(
                    f"WebSocket interrompu : {message}\n"
                    f"Nouvelle tentative dans "
                    f"{reconnect_delay:.0f} secondes."
                )

                with connect_db() as connection:
                    write_state(
                        connection,
                        "radar_status",
                        "RECONNECTING",
                    )
                    write_state(
                        connection,
                        "radar_last_error",
                        message[:500],
                    )
                    connection.commit()

                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 1.8,
                    30.0,
                )
    finally:
        await rpc_client.aclose()
        with connect_db() as connection:
            write_state(
                connection,
                "radar_status",
                "STOPPED",
            )
            connection.commit()
        release_lock()
        print("New Coin Radar arrêté proprement.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        running = False
