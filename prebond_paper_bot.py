
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import signal
import sqlite3
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from runtime_utils import connect_sqlite

import httpx

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
CONFIG_PATH = BASE_DIR / "config.json"
LOCK_PATH = BASE_DIR / "data" / "prebond_bot.lock"

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GLOBAL_ACCOUNT = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
WSOL_ADDRESS = "So11111111111111111111111111111111111111112"

BONDING_CURVE_DISCRIMINATOR = hashlib.sha256(
    b"account:BondingCurve"
).digest()[:8]
GLOBAL_DISCRIMINATOR = hashlib.sha256(b"account:Global").digest()[:8]

TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000
FALLBACK_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
STRATEGY = "hybrid_prebond_research_v1"

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_MAP = {character: index for index, character in enumerate(BASE58_ALPHABET)}

ED25519_P = 2**255 - 19
ED25519_D = (
    -121665 * pow(121666, ED25519_P - 2, ED25519_P)
) % ED25519_P
ED25519_I = pow(2, (ED25519_P - 1) // 4, ED25519_P)

running = True


class DexRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(10, retry_after_seconds)
        super().__init__("DEX_RATE_LIMIT_429")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


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


def b58decode(value: str) -> bytes:
    number = 0
    for character in value:
        if character not in BASE58_MAP:
            raise ValueError(f"Caractère base58 invalide : {character}")
        number = number * 58 + BASE58_MAP[character]

    decoded = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    zero_count = len(value) - len(value.lstrip("1"))
    return b"\x00" * zero_count + decoded


def b58encode(value: bytes) -> str:
    zero_count = len(value) - len(value.lstrip(b"\x00"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * zero_count + encoded


def is_ed25519_point(encoded: bytes) -> bool:
    if len(encoded) != 32:
        return False

    y = int.from_bytes(encoded, "little") & ((1 << 255) - 1)
    sign = (encoded[31] >> 7) & 1
    if y >= ED25519_P:
        return False

    y_squared = y * y % ED25519_P
    denominator = (ED25519_D * y_squared + 1) % ED25519_P
    if denominator == 0:
        return False

    x_squared = (
        (y_squared - 1)
        * pow(denominator, ED25519_P - 2, ED25519_P)
        % ED25519_P
    )
    x = pow(x_squared, (ED25519_P + 3) // 8, ED25519_P)

    if (x * x - x_squared) % ED25519_P != 0:
        x = x * ED25519_I % ED25519_P
    if (x * x - x_squared) % ED25519_P != 0:
        return False
    if x == 0 and sign:
        return False
    return True


def create_program_address(
    seeds: list[bytes],
    program_id: bytes,
) -> bytes:
    if len(seeds) > 16:
        raise ValueError("Trop de seeds PDA.")
    if any(len(seed) > 32 for seed in seeds):
        raise ValueError("Une seed PDA dépasse 32 octets.")

    digest = hashlib.sha256(
        b"".join(seeds)
        + program_id
        + b"ProgramDerivedAddress"
    ).digest()

    if is_ed25519_point(digest):
        raise ValueError("Adresse PDA sur la courbe Ed25519.")
    return digest


def find_program_address(
    seeds: list[bytes],
    program_id: bytes,
) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        try:
            return create_program_address(
                seeds + [bytes([bump])],
                program_id,
            ), bump
        except ValueError:
            continue
    raise RuntimeError("Impossible de dériver le PDA.")


def derive_bonding_curve_address(mint: str) -> str:
    program_bytes = b58decode(PUMP_PROGRAM_ID)
    mint_bytes = b58decode(mint)
    if len(program_bytes) != 32 or len(mint_bytes) != 32:
        raise ValueError("Programme ou mint invalide.")
    pda, _ = find_program_address(
        [b"bonding-curve", mint_bytes],
        program_bytes,
    )
    return b58encode(pda)


def connect() -> sqlite3.Connection:
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
        age_seconds = time.time() - LOCK_PATH.stat().st_mtime
        if age_seconds < 20:
            print("Un collecteur V5 fonctionne déjà.")
            sys.exit(1)
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def stop_handler(*_: object) -> None:
    global running
    running = False


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
        raise RuntimeError(
            f"RPC {method}: {payload['error']}"
        )
    return payload.get("result")


def fetch_multiple_accounts(
    client: httpx.Client,
    addresses: list[str],
) -> list[dict[str, Any] | None]:
    result = rpc_call(
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
    if not result or "value" not in result:
        raise RuntimeError("Réponse getMultipleAccounts incomplète.")
    return result["value"]


def decode_u64(data: bytes, offset: int) -> int:
    if len(data) < offset + 8:
        raise ValueError("Compte trop court pour u64.")
    return struct.unpack_from("<Q", data, offset)[0]


def decode_global_account(
    account: dict[str, Any] | None,
) -> int:
    if not account:
        return FALLBACK_INITIAL_REAL_TOKEN_RESERVES

    raw = base64.b64decode(account["data"][0])
    if len(raw) < 97 or raw[:8] != GLOBAL_DISCRIMINATOR:
        return FALLBACK_INITIAL_REAL_TOKEN_RESERVES

    # Anchor discriminator (8), initialized (1), authority (32),
    # fee recipient (32), initial virtual token (8),
    # initial virtual SOL (8), then initial real token reserves.
    initial_real_token_reserves = decode_u64(raw, 89)
    return (
        initial_real_token_reserves
        if initial_real_token_reserves > 0
        else FALLBACK_INITIAL_REAL_TOKEN_RESERVES
    )


def decode_bonding_curve_account(
    account: dict[str, Any] | None,
    curve_address: str,
    initial_real_token_reserves: int,
    sol_price_usd: float,
) -> dict[str, Any]:
    empty = {
        "bonding_curve_address": curve_address,
        "account_exists": 0,
        "owner_valid": 0,
        "discriminator_valid": 0,
        "virtual_token_reserves_raw": None,
        "virtual_quote_reserves_raw": None,
        "real_token_reserves_raw": None,
        "real_quote_reserves_raw": None,
        "token_total_supply_raw": None,
        "complete": None,
        "creator": None,
        "is_mayhem_mode": None,
        "initial_real_token_reserves_raw": initial_real_token_reserves,
        "progress_pct": None,
        "curve_price_sol": None,
        "curve_price_usd": None,
        "real_quote_reserves_sol": None,
        "rpc_status": "NO_ACCOUNT",
    }
    if not account:
        return empty

    owner_valid = int(account.get("owner") == PUMP_PROGRAM_ID)
    try:
        raw = base64.b64decode(account["data"][0])
    except Exception:
        return {
            **empty,
            "account_exists": 1,
            "owner_valid": owner_valid,
            "rpc_status": "BAD_BASE64",
        }

    if len(raw) < 49:
        return {
            **empty,
            "account_exists": 1,
            "owner_valid": owner_valid,
            "rpc_status": "ACCOUNT_TOO_SHORT",
        }

    discriminator_valid = int(
        raw[:8] == BONDING_CURVE_DISCRIMINATOR
    )

    try:
        virtual_token = decode_u64(raw, 8)
        virtual_quote = decode_u64(raw, 16)
        real_token = decode_u64(raw, 24)
        real_quote = decode_u64(raw, 32)
        total_supply = decode_u64(raw, 40)
        complete = int(bool(raw[48]))

        creator = (
            b58encode(raw[49:81])
            if len(raw) >= 81
            else None
        )
        is_mayhem_mode = (
            int(bool(raw[81]))
            if len(raw) >= 82
            else None
        )

        progress = (
            1.0
            - real_token / max(initial_real_token_reserves, 1)
        ) * 100.0
        progress = max(0.0, min(100.0, progress))

        curve_price_sol = 0.0
        if virtual_token > 0:
            curve_price_sol = (
                virtual_quote / LAMPORTS_PER_SOL
            ) / (
                virtual_token / (10**TOKEN_DECIMALS)
            )

        return {
            "bonding_curve_address": curve_address,
            "account_exists": 1,
            "owner_valid": owner_valid,
            "discriminator_valid": discriminator_valid,
            "virtual_token_reserves_raw": virtual_token,
            "virtual_quote_reserves_raw": virtual_quote,
            "real_token_reserves_raw": real_token,
            "real_quote_reserves_raw": real_quote,
            "token_total_supply_raw": total_supply,
            "complete": complete,
            "creator": creator,
            "is_mayhem_mode": is_mayhem_mode,
            "initial_real_token_reserves_raw": initial_real_token_reserves,
            "progress_pct": progress,
            "curve_price_sol": curve_price_sol or None,
            "curve_price_usd": (
                curve_price_sol * sol_price_usd
                if curve_price_sol and sol_price_usd
                else None
            ),
            "real_quote_reserves_sol": (
                real_quote / LAMPORTS_PER_SOL
            ),
            "rpc_status": (
                "OK"
                if owner_valid and discriminator_valid
                else "UNVERIFIED_ACCOUNT"
            ),
        }
    except (ValueError, struct.error):
        return {
            **empty,
            "account_exists": 1,
            "owner_valid": owner_valid,
            "discriminator_valid": discriminator_valid,
            "rpc_status": "DECODE_ERROR",
        }


def fetch_dex_pairs(
    client: httpx.Client,
    addresses: list[str],
) -> list[dict[str, Any]]:
    joined = ",".join(addresses + [WSOL_ADDRESS])
    response = client.get(
        f"/tokens/v1/solana/{joined}",
        headers={
            "Accept": "application/json",
            "User-Agent": "SOLPULSE-PREBOND/5.0",
        },
    )
    if response.status_code == 429:
        retry_after = to_int(
            response.headers.get("Retry-After"),
            60,
        )
        raise DexRateLimitError(retry_after)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Réponse DEX Screener inattendue.")
    return [
        pair for pair in payload
        if isinstance(pair, dict)
        and pair.get("chainId") == "solana"
    ]


def liquidity_usd(pair: dict[str, Any]) -> float:
    return to_float((pair.get("liquidity") or {}).get("usd"))


def select_best_pair(
    pairs: list[dict[str, Any]],
    mint: str,
) -> dict[str, Any] | None:
    candidates = [
        pair for pair in pairs
        if (pair.get("baseToken") or {}).get("address") == mint
    ]
    return max(candidates, key=liquidity_usd) if candidates else None


def find_sol_price_usd(pairs: list[dict[str, Any]]) -> float:
    candidates = [
        pair for pair in pairs
        if (pair.get("baseToken") or {}).get("address") == WSOL_ADDRESS
        and to_float(pair.get("priceUsd")) > 0
    ]
    if not candidates:
        return 0.0
    return to_float(
        max(candidates, key=liquidity_usd).get("priceUsd")
    )


def previous_price(
    connection: sqlite3.Connection,
    mint: str,
    seconds_ago: int,
) -> float:
    cutoff = (now_utc() - timedelta(seconds=seconds_ago)).isoformat()
    row = connection.execute(
        """
        SELECT COALESCE(price_sol, 0) AS price_sol
        FROM market_snapshots
        WHERE token_mint = ?
          AND timestamp <= ?
          AND price_sol IS NOT NULL
        ORDER BY datetime(timestamp) DESC
        LIMIT 1
        """,
        (mint, cutoff),
    ).fetchone()
    return to_float(row["price_sol"]) if row else 0.0


def percentage_change(current: float, previous: float) -> float:
    if current <= 0 or previous <= 0:
        return 0.0
    return (current / previous - 1.0) * 100.0


def score_market(
    lifecycle: str,
    progress: float | None,
    liquidity: float,
    volume_5m: float,
    buys_5m: int,
    sells_5m: int,
    change_5m: float,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if lifecycle == "BONDING":
        score += 12
        reasons.append("Bonding curve active")
        if progress is not None:
            if 20 <= progress <= 85:
                score += 22
                reasons.append("Progression dans la zone de recherche")
            elif 5 <= progress < 20:
                score += 10
                reasons.append("Courbe encore jeune")
            elif 85 < progress < 98:
                score += 12
                reasons.append("Graduation proche")
            elif progress >= 98:
                score -= 10
                reasons.append("Graduation imminente, risque d'exécution")
    elif lifecycle == "BONDED":
        score += 15
        reasons.append("Token gradué avec marché DEX")
    elif lifecycle == "GRADUATING":
        score -= 15
        reasons.append("Migration en cours")
    else:
        reasons.append("État Pump non confirmé")

    if liquidity >= 100_000:
        score += 20
        reasons.append("Liquidité élevée")
    elif liquidity >= 25_000:
        score += 13
        reasons.append("Liquidité acceptable")
    elif lifecycle == "BONDING":
        score += 5
    else:
        score -= 8

    if volume_5m >= 50_000:
        score += 20
        reasons.append("Volume 5 min élevé")
    elif volume_5m >= 5_000:
        score += 12
        reasons.append("Volume 5 min actif")
    elif volume_5m > 0:
        score += 4

    ratio = buys_5m / max(sells_5m, 1)
    if ratio >= 1.7:
        score += 21
        reasons.append("Forte pression acheteuse")
    elif ratio >= 1.15:
        score += 13
        reasons.append("Pression acheteuse positive")
    elif ratio < 0.75:
        score -= 12
        reasons.append("Pression vendeuse")

    if 2 <= change_5m <= 20:
        score += 18
        reasons.append("Momentum favorable")
    elif 0 < change_5m < 2:
        score += 7
    elif change_5m > 35:
        score -= 15
        reasons.append("Mouvement trop étendu")
    elif change_5m < -10:
        score -= 15
        reasons.append("Baisse rapide")

    return max(0.0, min(100.0, score)), reasons


def save_market_snapshot(
    connection: sqlite3.Connection,
    token: dict[str, str],
    pair: dict[str, Any] | None,
    sol_price_usd: float,
    lifecycle: str,
    progress: float | None,
) -> tuple[dict[str, Any], list[str]]:
    if pair is None:
        snapshot = {
            "token_mint": token["address"],
            "token_name": token["label"],
            "symbol": token["label"],
            "price_sol": None,
            "price_usd": None,
            "change_1m_pct": 0.0,
            "change_5m_pct": 0.0,
            "change_h1_pct": 0.0,
            "liquidity_usd": 0.0,
            "volume_5m_usd": 0.0,
            "volume_h1_usd": 0.0,
            "buys_5m": 0,
            "sells_5m": 0,
            "market_cap_usd": None,
            "fdv_usd": None,
            "pair_created_at": None,
            "pair_address": None,
            "dex_id": None,
            "source_url": None,
            "score": 0.0,
            "data_status": "NO_PAIR",
        }
        reasons = ["Aucune paire DEX détectée"]
    else:
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        txns = pair.get("txns") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        liquidity = pair.get("liquidity") or {}

        price_usd = to_float(pair.get("priceUsd"))
        quote_symbol = str(quote.get("symbol") or "").upper()
        quote_address = quote.get("address")

        price_sol = 0.0
        if quote_address == WSOL_ADDRESS or quote_symbol in {"SOL", "WSOL"}:
            price_sol = to_float(pair.get("priceNative"))
        elif price_usd > 0 and sol_price_usd > 0:
            price_sol = price_usd / sol_price_usd

        previous_1m = previous_price(
            connection,
            token["address"],
            60,
        )
        change_1m = percentage_change(price_sol, previous_1m)
        change_5m = to_float(change.get("m5"))
        change_h1 = to_float(change.get("h1"))
        m5 = txns.get("m5") or {}
        buys = to_int(m5.get("buys"))
        sells = to_int(m5.get("sells"))
        liquidity_value = to_float(liquidity.get("usd"))
        volume_5m = to_float(volume.get("m5"))

        score, reasons = score_market(
            lifecycle=lifecycle,
            progress=progress,
            liquidity=liquidity_value,
            volume_5m=volume_5m,
            buys_5m=buys,
            sells_5m=sells,
            change_5m=change_5m,
        )

        snapshot = {
            "token_mint": token["address"],
            "token_name": str(base.get("name") or token["label"]),
            "symbol": str(base.get("symbol") or token["label"]),
            "price_sol": price_sol or None,
            "price_usd": price_usd or None,
            "change_1m_pct": change_1m,
            "change_5m_pct": change_5m,
            "change_h1_pct": change_h1,
            "liquidity_usd": liquidity_value,
            "volume_5m_usd": volume_5m,
            "volume_h1_usd": to_float(volume.get("h1")),
            "buys_5m": buys,
            "sells_5m": sells,
            "market_cap_usd": to_float(pair.get("marketCap")) or None,
            "fdv_usd": to_float(pair.get("fdv")) or None,
            "pair_created_at": to_int(pair.get("pairCreatedAt")) or None,
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
            "source_url": pair.get("url"),
            "score": score,
            "data_status": (
                "OK" if price_sol > 0 or price_usd > 0 else "NO_PRICE"
            ),
        }

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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            snapshot["token_mint"],
            snapshot["token_name"],
            snapshot["symbol"],
            snapshot["price_sol"],
            snapshot["price_usd"],
            snapshot["change_1m_pct"],
            snapshot["change_5m_pct"],
            snapshot["change_h1_pct"],
            snapshot["liquidity_usd"],
            snapshot["volume_5m_usd"],
            snapshot["volume_h1_usd"],
            snapshot["buys_5m"],
            snapshot["sells_5m"],
            snapshot["market_cap_usd"],
            snapshot["fdv_usd"],
            snapshot["pair_created_at"],
            snapshot["pair_address"],
            snapshot["dex_id"],
            snapshot["source_url"],
            snapshot["score"],
            snapshot["data_status"],
        ),
    )
    return snapshot, reasons


def determine_lifecycle(
    curve: dict[str, Any],
    dex_pair_exists: bool,
) -> str:
    if not curve["account_exists"]:
        return "DEX_ONLY" if dex_pair_exists else "UNKNOWN"
    if curve["complete"]:
        return "BONDED" if dex_pair_exists else "GRADUATING"
    return "BONDING"


def save_bonding_snapshot(
    connection: sqlite3.Connection,
    token: dict[str, str],
    curve: dict[str, Any],
    token_name: str,
    symbol: str,
    lifecycle: str,
) -> None:
    connection.execute(
        """
        INSERT INTO bonding_snapshots (
            timestamp, token_mint, token_name, symbol,
            bonding_curve_address, account_exists, owner_valid,
            discriminator_valid, virtual_token_reserves_raw,
            virtual_quote_reserves_raw, real_token_reserves_raw,
            real_quote_reserves_raw, token_total_supply_raw,
            complete, creator, is_mayhem_mode,
            initial_real_token_reserves_raw, progress_pct,
            curve_price_sol, curve_price_usd,
            real_quote_reserves_sol, lifecycle_state, rpc_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            token["address"],
            token_name,
            symbol,
            curve["bonding_curve_address"],
            curve["account_exists"],
            curve["owner_valid"],
            curve["discriminator_valid"],
            curve["virtual_token_reserves_raw"],
            curve["virtual_quote_reserves_raw"],
            curve["real_token_reserves_raw"],
            curve["real_quote_reserves_raw"],
            curve["token_total_supply_raw"],
            curve["complete"],
            curve["creator"],
            curve["is_mayhem_mode"],
            curve["initial_real_token_reserves_raw"],
            curve["progress_pct"],
            curve["curve_price_sol"],
            curve["curve_price_usd"],
            curve["real_quote_reserves_sol"],
            lifecycle,
            curve["rpc_status"],
        ),
    )


def latest_rows_by_mint(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, sqlite3.Row]:
    if table not in {"market_snapshots", "bonding_snapshots"}:
        raise ValueError("Table non autorisée.")
    rows = connection.execute(
        f"""
        SELECT source.*
        FROM {table} source
        JOIN (
            SELECT token_mint, MAX(id) AS max_id
            FROM {table}
            GROUP BY token_mint
        ) latest ON latest.max_id = source.id
        """
    ).fetchall()
    return {row["token_mint"]: row for row in rows}


def latest_portfolio(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM portfolio_snapshots
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Portefeuille absent.")
    return row


def open_positions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
    ).fetchall()


def constant_product_buy_quote(
    virtual_token_raw: int,
    virtual_quote_raw: int,
    real_token_raw: int,
    amount_sol: float,
    fee_bps: int,
) -> tuple[float, float, float]:
    gross_quote_raw = int(amount_sol * LAMPORTS_PER_SOL)
    net_quote_raw = int(gross_quote_raw * (10_000 - fee_bps) / 10_000)
    if (
        virtual_token_raw <= 0
        or virtual_quote_raw <= 0
        or net_quote_raw <= 0
    ):
        return 0.0, 0.0, 0.0

    invariant = virtual_token_raw * virtual_quote_raw
    new_virtual_quote = virtual_quote_raw + net_quote_raw
    new_virtual_token = (invariant + new_virtual_quote - 1) // new_virtual_quote
    token_out_raw = max(0, virtual_token_raw - new_virtual_token)
    token_out_raw = min(token_out_raw, max(real_token_raw, 0))
    tokens_out = token_out_raw / (10**TOKEN_DECIMALS)

    spot_price = (
        virtual_quote_raw / LAMPORTS_PER_SOL
    ) / (
        virtual_token_raw / (10**TOKEN_DECIMALS)
    )
    effective_price = amount_sol / tokens_out if tokens_out > 0 else 0.0
    impact_pct = (
        (effective_price / spot_price - 1.0) * 100.0
        if spot_price > 0 and effective_price > 0
        else 0.0
    )
    return tokens_out, effective_price, max(0.0, impact_pct)


def constant_product_sell_quote(
    virtual_token_raw: int,
    virtual_quote_raw: int,
    tokens: float,
    fee_bps: int,
) -> tuple[float, float]:
    token_in_raw = int(tokens * (10**TOKEN_DECIMALS))
    if (
        virtual_token_raw <= 0
        or virtual_quote_raw <= 0
        or token_in_raw <= 0
    ):
        return 0.0, 0.0

    invariant = virtual_token_raw * virtual_quote_raw
    new_virtual_token = virtual_token_raw + token_in_raw
    new_virtual_quote = invariant // new_virtual_token
    gross_quote_raw = max(0, virtual_quote_raw - new_virtual_quote)
    net_quote_raw = int(gross_quote_raw * (10_000 - fee_bps) / 10_000)
    output_sol = net_quote_raw / LAMPORTS_PER_SOL

    spot_value = tokens * (
        virtual_quote_raw / LAMPORTS_PER_SOL
    ) / (
        virtual_token_raw / (10**TOKEN_DECIMALS)
    )
    impact_pct = (
        max(0.0, (1.0 - output_sol / spot_value) * 100.0)
        if spot_value > 0
        else 0.0
    )
    return output_sol, impact_pct


def dex_execution_model(
    amount_sol: float,
    sol_price_usd: float,
    liquidity: float,
) -> tuple[float, float, int]:
    order_usd = amount_sol * max(sol_price_usd, 1.0)
    liquidity = max(liquidity, 1.0)
    impact = min(10.0, 0.12 + order_usd / liquidity * 180.0)
    slippage = min(10.0, 0.20 + impact * 0.65)
    return impact, slippage, 950


def update_positions(
    connection: sqlite3.Connection,
    market_map: dict[str, sqlite3.Row],
    bonding_map: dict[str, sqlite3.Row],
    sol_price_usd: float,
    fee_bps: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    max_holding_minutes: float,
) -> tuple[float, float]:
    portfolio = latest_portfolio(connection)
    cash = float(portfolio["cash_sol"])
    realized_total = float(portfolio["realized_pnl_sol"])

    for position in open_positions(connection):
        mint = position["token_mint"]
        market = market_map.get(mint)
        bonding = bonding_map.get(mint)
        mode = position["market_mode"]
        tokens = float(position["tokens_received"])

        current_value = 0.0
        current_price_sol = 0.0
        current_price_usd = 0.0
        impact = 0.0
        slippage = 0.0
        lifecycle = bonding["lifecycle_state"] if bonding else "UNKNOWN"

        if (
            mode == "BONDING"
            and bonding
            and bonding["virtual_token_reserves_raw"]
            and bonding["virtual_quote_reserves_raw"]
        ):
            current_value, impact = constant_product_sell_quote(
                int(bonding["virtual_token_reserves_raw"]),
                int(bonding["virtual_quote_reserves_raw"]),
                tokens,
                fee_bps,
            )
            current_price_sol = to_float(bonding["curve_price_sol"])
            current_price_usd = to_float(bonding["curve_price_usd"])
            slippage = impact
        elif market and market["price_sol"]:
            current_price_sol = to_float(market["price_sol"])
            current_price_usd = to_float(market["price_usd"])
            raw_value = tokens * current_price_sol
            impact, slippage, _ = dex_execution_model(
                raw_value,
                sol_price_usd,
                to_float(market["liquidity_usd"]),
            )
            current_value = raw_value * (1.0 - slippage / 100.0)
        else:
            continue

        entry_total = (
            float(position["entry_sol"])
            + float(position["entry_fees_sol"] or 0)
        )
        pnl_pct = (
            (current_value - entry_total) / entry_total * 100.0
            if entry_total > 0
            else 0.0
        )
        opened_at = datetime.fromisoformat(position["opened_at"])
        age_minutes = (now_utc() - opened_at).total_seconds() / 60.0

        exit_reason: str | None = None
        if pnl_pct <= stop_loss_pct:
            exit_reason = "STOP_LOSS"
        elif pnl_pct >= take_profit_pct:
            exit_reason = "TAKE_PROFIT"
        elif age_minutes >= max_holding_minutes:
            exit_reason = "TIME_EXIT"
        elif mode == "BONDING" and lifecycle == "GRADUATING":
            exit_reason = "CURVE_COMPLETE"
        elif (
            market
            and to_int(market["sells_5m"])
            >= max(8, to_int(market["buys_5m"]) * 2)
            and to_float(market["change_5m_pct"]) < -5
        ):
            exit_reason = "SELL_PRESSURE"

        connection.execute(
            """
            UPDATE positions
            SET current_price_sol = ?, current_price_usd = ?,
                current_value_sol = ?
            WHERE id = ?
            """,
            (
                current_price_sol,
                current_price_usd,
                current_value,
                position["id"],
            ),
        )

        if exit_reason is None:
            continue

        exit_fee = 0.00005
        exit_sol = max(0.0, current_value - exit_fee)
        pnl_sol = exit_sol - entry_total
        pnl_pct_final = (
            pnl_sol / entry_total * 100.0
            if entry_total > 0
            else 0.0
        )
        cash += exit_sol
        realized_total += pnl_sol

        connection.execute(
            """
            UPDATE positions
            SET closed_at = ?, exit_sol = ?, exit_price_sol = ?,
                current_value_sol = ?, exit_fees_sol = ?,
                realized_pnl_sol = ?, realized_pnl_pct = ?,
                exit_reason = ?, status = 'CLOSED'
            WHERE id = ?
            """,
            (
                now_iso(),
                exit_sol,
                current_price_sol,
                exit_sol,
                exit_fee,
                pnl_sol,
                pnl_pct_final,
                exit_reason,
                position["id"],
            ),
        )

        connection.execute(
            """
            INSERT INTO paper_orders (
                timestamp, token_mint, token_name, symbol,
                market_mode, side, requested_sol,
                expected_output, simulated_output,
                price_impact_pct, extra_slippage_pct,
                latency_ms, status, failure_reason
            )
            VALUES (?, ?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, 950,
                    'FILLED', NULL)
            """,
            (
                now_iso(),
                mint,
                position["token_name"],
                position["symbol"],
                mode,
                current_value,
                current_value,
                exit_sol,
                impact,
                slippage,
            ),
        )
        connection.execute(
            """
            INSERT INTO signals (
                timestamp, token_mint, token_name, symbol,
                lifecycle_state, decision, strategy, score,
                reasons_json
            )
            VALUES (?, ?, ?, ?, ?, 'SELL', ?, ?, ?)
            """,
            (
                now_iso(),
                mint,
                position["token_name"],
                position["symbol"],
                lifecycle,
                STRATEGY,
                to_float(market["score"]) if market else 0.0,
                json.dumps(
                    [
                        exit_reason,
                        f"PnL paper {pnl_pct_final:+.2f}%",
                    ],
                    ensure_ascii=False,
                ),
            ),
        )

    return cash, realized_total


def last_buy_time(
    connection: sqlite3.Connection,
    mint: str,
) -> datetime | None:
    row = connection.execute(
        """
        SELECT timestamp
        FROM signals
        WHERE token_mint = ? AND decision = 'BUY'
        ORDER BY id DESC
        LIMIT 1
        """,
        (mint,),
    ).fetchone()
    return datetime.fromisoformat(row["timestamp"]) if row else None


def maybe_open_positions(
    connection: sqlite3.Connection,
    market_map: dict[str, sqlite3.Row],
    bonding_map: dict[str, sqlite3.Row],
    sol_price_usd: float,
    cash: float,
    config: dict[str, Any],
) -> float:
    if not bool(config.get("paper_trading", True)):
        return cash

    position_size = float(config.get("position_size_sol", 0.05))
    max_positions = int(config.get("max_open_positions", 3))
    max_exposure = float(config.get("max_total_exposure_sol", 0.15))
    fee_bps = int(config.get("bonding_curve_fee_bps", 125))

    current = open_positions(connection)
    open_mints = {row["token_mint"] for row in current}
    exposure = sum(float(row["entry_sol"]) for row in current)

    candidates: list[tuple[float, str]] = []
    all_mints = set(market_map) | set(bonding_map)
    for mint in all_mints:
        market = market_map.get(mint)
        bonding = bonding_map.get(mint)
        score = to_float(market["score"]) if market else 0.0
        lifecycle = bonding["lifecycle_state"] if bonding else "UNKNOWN"
        if lifecycle == "BONDING":
            progress = to_float(bonding["progress_pct"])
            if 5 <= progress <= 95:
                score += 5
        candidates.append((score, mint))

    for score, mint in sorted(candidates, reverse=True):
        if mint in open_mints:
            continue
        if len(current) >= max_positions:
            break
        if exposure + position_size > max_exposure:
            break
        if cash < position_size + 0.00005 + 0.10:
            break
        if score < 68:
            continue

        market = market_map.get(mint)
        bonding = bonding_map.get(mint)
        lifecycle = bonding["lifecycle_state"] if bonding else "UNKNOWN"

        last_buy = last_buy_time(connection, mint)
        if last_buy and (now_utc() - last_buy).total_seconds() < 900:
            continue

        mode: str | None = None
        tokens_received = 0.0
        entry_price_sol = 0.0
        entry_price_usd = 0.0
        impact = 0.0
        slippage = 0.0

        if (
            lifecycle == "BONDING"
            and bonding
            and 5 <= to_float(bonding["progress_pct"]) <= 95
            and bonding["virtual_token_reserves_raw"]
            and bonding["virtual_quote_reserves_raw"]
            and bonding["real_token_reserves_raw"]
        ):
            mode = "BONDING"
            (
                tokens_received,
                entry_price_sol,
                impact,
            ) = constant_product_buy_quote(
                int(bonding["virtual_token_reserves_raw"]),
                int(bonding["virtual_quote_reserves_raw"]),
                int(bonding["real_token_reserves_raw"]),
                position_size,
                fee_bps,
            )
            entry_price_usd = entry_price_sol * sol_price_usd
            slippage = impact
        elif (
            lifecycle in {"BONDED", "DEX_ONLY"}
            and market
            and market["price_sol"]
            and to_float(market["liquidity_usd"]) >= 25_000
        ):
            mode = "DEX"
            spot_price = to_float(market["price_sol"])
            impact, slippage, _ = dex_execution_model(
                position_size,
                sol_price_usd,
                to_float(market["liquidity_usd"]),
            )
            entry_price_sol = spot_price * (1.0 + slippage / 100.0)
            entry_price_usd = to_float(market["price_usd"]) * (
                1.0 + slippage / 100.0
            )
            tokens_received = (
                position_size - 0.00005
            ) / entry_price_sol

        if not mode or tokens_received <= 0 or entry_price_sol <= 0:
            continue

        entry_fee = 0.00005
        current_value = tokens_received * (
            to_float(bonding["curve_price_sol"])
            if mode == "BONDING" and bonding
            else to_float(market["price_sol"])
        )
        cash -= position_size + entry_fee
        exposure += position_size

        token_name = (
            market["token_name"]
            if market and market["token_name"]
            else bonding["token_name"]
        )
        symbol = (
            market["symbol"]
            if market and market["symbol"]
            else bonding["symbol"]
        )

        connection.execute(
            """
            INSERT INTO positions (
                token_mint, token_name, symbol, market_mode,
                lifecycle_at_entry, bonding_curve_address,
                pair_address, source_url, opened_at, closed_at,
                entry_sol, exit_sol, tokens_received,
                entry_price_sol, entry_price_usd, exit_price_sol,
                current_price_sol, current_price_usd,
                current_value_sol, entry_liquidity_usd,
                entry_market_cap_usd, entry_bonding_progress_pct,
                entry_fees_sol, exit_fees_sol,
                realized_pnl_sol, realized_pnl_pct,
                strategy, exit_reason, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                    ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?,
                    ?, 0, NULL, NULL, ?, NULL, 'OPEN')
            """,
            (
                mint,
                token_name,
                symbol,
                mode,
                lifecycle,
                bonding["bonding_curve_address"] if bonding else None,
                market["pair_address"] if market else None,
                market["source_url"] if market else None,
                now_iso(),
                position_size,
                tokens_received,
                entry_price_sol,
                entry_price_usd,
                (
                    to_float(bonding["curve_price_sol"])
                    if mode == "BONDING" and bonding
                    else to_float(market["price_sol"])
                ),
                (
                    to_float(bonding["curve_price_usd"])
                    if mode == "BONDING" and bonding
                    else to_float(market["price_usd"])
                ),
                current_value,
                to_float(market["liquidity_usd"]) if market else 0.0,
                market["market_cap_usd"] if market else None,
                bonding["progress_pct"] if bonding else None,
                entry_fee,
                STRATEGY,
            ),
        )

        connection.execute(
            """
            INSERT INTO paper_orders (
                timestamp, token_mint, token_name, symbol,
                market_mode, side, requested_sol,
                expected_output, simulated_output,
                price_impact_pct, extra_slippage_pct,
                latency_ms, status, failure_reason
            )
            VALUES (?, ?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, 950,
                    'FILLED', NULL)
            """,
            (
                now_iso(),
                mint,
                token_name,
                symbol,
                mode,
                position_size,
                tokens_received,
                tokens_received,
                impact,
                slippage,
            ),
        )
        connection.execute(
            """
            INSERT INTO signals (
                timestamp, token_mint, token_name, symbol,
                lifecycle_state, decision, strategy, score,
                reasons_json
            )
            VALUES (?, ?, ?, ?, ?, 'BUY', ?, ?, ?)
            """,
            (
                now_iso(),
                mint,
                token_name,
                symbol,
                lifecycle,
                STRATEGY,
                score,
                json.dumps(
                    [
                        f"Mode {mode}",
                        f"État {lifecycle}",
                        f"Score {score:.1f}/100",
                        (
                            f"Progression {to_float(bonding['progress_pct']):.1f}%"
                            if bonding
                            else "Pas de courbe Pump"
                        ),
                    ],
                    ensure_ascii=False,
                ),
            ),
        )

        current = open_positions(connection)
        open_mints.add(mint)

    return cash


def save_portfolio(
    connection: sqlite3.Connection,
    cash: float,
    realized_total: float,
) -> None:
    positions = open_positions(connection)
    open_value = sum(to_float(row["current_value_sol"]) for row in positions)
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
            timestamp, cash_sol, open_positions_value_sol,
            equity_sol, realized_pnl_sol, unrealized_pnl_sol
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


def prune(connection: sqlite3.Connection) -> None:
    for table, limit in (
        ("market_snapshots", 15000),
        ("bonding_snapshots", 15000),
        ("portfolio_snapshots", 15000),
    ):
        connection.execute(
            f"""
            DELETE FROM {table}
            WHERE id NOT IN (
                SELECT id FROM {table}
                ORDER BY id DESC
                LIMIT {limit}
            )
            """
        )


def main() -> None:
    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        sys.exit(1)

    watchlist = load_json(WATCHLIST_PATH)
    config = load_json(CONFIG_PATH)
    tokens = watchlist["tokens"]
    mints = [token["address"] for token in tokens]
    rpc_url = os.getenv(
        "SOLANA_RPC_URL",
        str(config.get("solana_rpc_url")),
    )
    poll_seconds = float(config.get("poll_seconds", 5))
    fee_bps = int(config.get("bonding_curve_fee_bps", 125))

    curve_addresses = {
        mint: derive_bonding_curve_address(mint)
        for mint in mints
    }

    # Self-test the pure-Python PDA implementation against the known Global PDA.
    derived_global, _ = find_program_address(
        [b"global"],
        b58decode(PUMP_PROGRAM_ID),
    )
    if b58encode(derived_global) != GLOBAL_ACCOUNT:
        raise RuntimeError("Échec de l'auto-test PDA.")

    acquire_lock()
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    print("=" * 72)
    print("SOLPULSE V12.2 — WATCHLIST RATE-LIMIT SAFE")
    print("=" * 72)
    print(f"RPC : {rpc_url}")
    print("DEX : DEX Screener")
    print("Données réelles, ordres fictifs.")
    print()
    for mint in mints:
        print(f"{mint}")
        print(f"  Curve PDA : {curve_addresses[mint]}")
    print()

    rpc_client = httpx.Client(
        base_url=rpc_url,
        timeout=httpx.Timeout(20.0),
    )
    dex_client = httpx.Client(
        base_url=DEXSCREENER_BASE_URL,
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    )

    dex_cooldown_until = 0.0
    dex_cooldown_reason = ""

    try:
        while running:
            started = time.monotonic()
            LOCK_PATH.touch(exist_ok=True)
            last_errors: list[str] = []

            pairs: list[dict[str, Any]] = []
            sol_price_usd = 0.0
            dex_ok = False
            rpc_ok = False

            dex_status_value = "WAITING"
            if time.monotonic() < dex_cooldown_until:
                remaining = max(1, int(dex_cooldown_until - time.monotonic()))
                dex_status_value = "COOLDOWN"
                dex_cooldown_reason = (
                    f"DEX Screener en pause automatique ({remaining} s)"
                )
                last_errors.append(dex_cooldown_reason)
            else:
                try:
                    pairs = fetch_dex_pairs(dex_client, mints)
                    sol_price_usd = find_sol_price_usd(pairs)
                    dex_ok = True
                    dex_status_value = "OK"
                    dex_cooldown_reason = ""
                except DexRateLimitError as error:
                    cooldown = max(
                        int(config.get("hybrid_market_cooldown_seconds", 60)),
                        error.retry_after_seconds,
                    )
                    dex_cooldown_until = time.monotonic() + cooldown
                    dex_status_value = "COOLDOWN"
                    dex_cooldown_reason = (
                        "DEX Screener a limité les requêtes (429). "
                        f"Pause automatique de {cooldown} s."
                    )
                    last_errors.append(dex_cooldown_reason)
                except Exception as error:
                    dex_status_value = "ERROR"
                    last_errors.append(
                        f"DEX indisponible : {type(error).__name__}"
                    )

            account_map: dict[str, dict[str, Any] | None] = {}
            initial_real_reserves = FALLBACK_INITIAL_REAL_TOKEN_RESERVES
            try:
                rpc_addresses = [
                    GLOBAL_ACCOUNT,
                    *[curve_addresses[mint] for mint in mints],
                ]
                accounts = fetch_multiple_accounts(
                    rpc_client,
                    rpc_addresses,
                )
                initial_real_reserves = decode_global_account(accounts[0])
                account_map = {
                    mint: accounts[index + 1]
                    for index, mint in enumerate(mints)
                }
                rpc_ok = True
            except Exception as error:
                last_errors.append(f"RPC: {error}")
                account_map = {mint: None for mint in mints}

            with connect() as connection:
                previous_sol = connection.execute(
                    "SELECT value FROM bot_state WHERE key='sol_price_usd'"
                ).fetchone()
                if sol_price_usd <= 0 and previous_sol:
                    sol_price_usd = to_float(previous_sol["value"])

                combined: list[tuple[dict[str, str], dict[str, Any], dict[str, Any], list[str]]] = []

                for token in tokens:
                    mint = token["address"]
                    pair = select_best_pair(pairs, mint)
                    curve = decode_bonding_curve_account(
                        account_map.get(mint),
                        curve_addresses[mint],
                        initial_real_reserves,
                        sol_price_usd,
                    )
                    lifecycle = determine_lifecycle(
                        curve,
                        pair is not None,
                    )
                    market, reasons = save_market_snapshot(
                        connection,
                        token,
                        pair,
                        sol_price_usd,
                        lifecycle,
                        curve["progress_pct"],
                    )

                    # If no usable DEX price exists while on the curve,
                    # use the on-chain curve price as the real market mark.
                    if (
                        lifecycle == "BONDING"
                        and curve["curve_price_sol"]
                        and not market["price_sol"]
                    ):
                        connection.execute(
                            """
                            UPDATE market_snapshots
                            SET price_sol = ?, price_usd = ?,
                                data_status = 'CURVE_PRICE',
                                score = ?
                            WHERE id = last_insert_rowid()
                            """,
                            (
                                curve["curve_price_sol"],
                                curve["curve_price_usd"],
                                max(
                                    market["score"],
                                    score_market(
                                        lifecycle,
                                        curve["progress_pct"],
                                        market["liquidity_usd"],
                                        market["volume_5m_usd"],
                                        market["buys_5m"],
                                        market["sells_5m"],
                                        market["change_5m_pct"],
                                    )[0],
                                ),
                            ),
                        )
                        market["price_sol"] = curve["curve_price_sol"]
                        market["price_usd"] = curve["curve_price_usd"]
                        market["data_status"] = "CURVE_PRICE"

                    save_bonding_snapshot(
                        connection,
                        token,
                        curve,
                        market["token_name"],
                        market["symbol"],
                        lifecycle,
                    )
                    combined.append((token, market, curve, reasons))

                market_map = latest_rows_by_mint(
                    connection,
                    "market_snapshots",
                )
                bonding_map = latest_rows_by_mint(
                    connection,
                    "bonding_snapshots",
                )

                if not bool(
                    config.get("central_portfolio_engine", False)
                ):
                    cash, realized = update_positions(
                        connection,
                        market_map,
                        bonding_map,
                        sol_price_usd,
                        fee_bps,
                        float(config.get("stop_loss_pct", -8)),
                        float(config.get("take_profit_pct", 15)),
                        float(config.get("max_holding_minutes", 45)),
                    )
                    cash = maybe_open_positions(
                        connection,
                        market_map,
                        bonding_map,
                        sol_price_usd,
                        cash,
                        config,
                    )
                    save_portfolio(connection, cash, realized)

                prune(connection)

                write_state(
                    connection,
                    "status",
                    "RUNNING" if rpc_ok and dex_ok else "DATA_ERROR",
                )
                write_state(
                    connection,
                    "dex_status",
                    dex_status_value,
                )
                write_state(
                    connection,
                    "dex_cooldown_remaining_seconds",
                    str(
                        max(
                            0,
                            int(
                                dex_cooldown_until
                                - time.monotonic()
                            ),
                        )
                    ),
                )
                write_state(
                    connection,
                    "rpc_status",
                    "OK" if rpc_ok else "ERROR",
                )
                write_state(connection, "last_tick", now_iso())
                write_state(
                    connection,
                    "last_error",
                    " | ".join(last_errors),
                )
                write_state(
                    connection,
                    "sol_price_usd",
                    str(sol_price_usd),
                )
                connection.commit()

                latest_bonding = latest_rows_by_mint(
                    connection,
                    "bonding_snapshots",
                )
                state_counts: dict[str, int] = {}
                for row in latest_bonding.values():
                    lifecycle = row["lifecycle_state"]
                    state_counts[lifecycle] = state_counts.get(lifecycle, 0) + 1

                portfolio = latest_portfolio(connection)
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} | "
                    f"RPC {'OK' if rpc_ok else 'ERR'} | "
                    f"DEX {dex_status_value} | "
                    f"{state_counts} | "
                    f"Équité {float(portfolio['equity_sol']):.4f} SOL | "
                    f"Positions {len(open_positions(connection))}"
                )

            elapsed = time.monotonic() - started
            time.sleep(max(0.5, poll_seconds - elapsed))

    finally:
        rpc_client.close()
        dex_client.close()
        try:
            with connect() as connection:
                write_state(connection, "status", "STOPPED")
                connection.commit()
        except Exception:
            pass
        LOCK_PATH.unlink(missing_ok=True)
        print("Collecteur V5 arrêté proprement.")


if __name__ == "__main__":
    main()
