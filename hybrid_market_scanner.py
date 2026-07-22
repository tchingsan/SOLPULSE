
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
LOCK_PATH = BASE_DIR / "data" / "hybrid_market_scanner.lock"

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
PUMP_AMM_PROGRAM_ID = (
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
)
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

BASE58_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)

running = True
dex_cooldown_until = 0.0


class DexRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(10, retry_after_seconds)
        super().__init__(
            f"DEX_RATE_LIMIT_429; reprise dans "
            f"{self.retry_after_seconds} secondes"
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            print("Le Hybrid Market Scanner fonctionne déjà.")
            raise SystemExit(1)
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


def load_tracked_launches(
    limit: int,
) -> list[sqlite3.Row]:
    # DEX Screener is useful only when migration is plausible or when a
    # pair/position already exists. Querying every fresh bonding coin was
    # the source of unnecessary 429 errors in V11.3.
    with connect_db() as connection:
        return connection.execute(
            """
            SELECT DISTINCT launches.*
            FROM new_launches launches
            LEFT JOIN positions
                ON positions.token_mint = launches.mint
                AND positions.status = 'OPEN'
            WHERE launches.is_mayhem_mode = 0
              AND (
                    positions.id IS NOT NULL
                    OR launches.pair_address IS NOT NULL
                    OR launches.complete = 1
                    OR launches.lifecycle_state IN (
                        'CURVE_COMPLETE',
                        'GRADUATING',
                        'MIGRATED'
                    )
                    OR COALESCE(launches.progress_pct, 0) >= 80
              )
            ORDER BY
                CASE
                    WHEN positions.id IS NOT NULL THEN 0
                    WHEN launches.pair_address IS NOT NULL THEN 1
                    WHEN launches.complete = 1 THEN 2
                    ELSE 3
                END,
                CASE
                    WHEN launches.market_last_updated_at IS NULL
                    THEN datetime(launches.detected_at)
                    ELSE datetime(launches.market_last_updated_at)
                END ASC,
                launches.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_sol_price() -> float:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT value
            FROM bot_state
            WHERE key='sol_price_usd'
            """
        ).fetchone()
    return to_float(row["value"] if row else None)


def choose_pair(
    pairs: list[dict[str, Any]],
    mint: str,
    prefer_pumpswap: bool,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "") != "solana":
            continue

        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        if str(base.get("address") or "") != mint:
            continue

        quote_address = str(quote.get("address") or "")
        quote_symbol = str(quote.get("symbol") or "").upper()
        quote_is_supported = (
            quote_address in {WSOL_MINT, USDC_MINT}
            or quote_symbol in {"SOL", "WSOL", "USDC"}
        )
        if not quote_is_supported:
            continue

        candidates.append(pair)

    if not candidates:
        return None

    def rank(pair: dict[str, Any]) -> tuple[int, int, float, float]:
        dex_id = str(pair.get("dexId") or "").lower()
        quote = pair.get("quoteToken") or {}
        quote_address = str(quote.get("address") or "")
        quote_symbol = str(quote.get("symbol") or "").upper()
        is_pumpswap = int("pump" in dex_id)
        is_sol_quote = int(
            quote_address == WSOL_MINT
            or quote_symbol in {"SOL", "WSOL"}
        )
        liquidity = to_float(
            (pair.get("liquidity") or {}).get("usd")
        )
        volume = to_float(
            (pair.get("volume") or {}).get("h24")
        )
        return (
            is_pumpswap if prefer_pumpswap else 0,
            is_sol_quote,
            liquidity,
            volume,
        )

    return max(candidates, key=rank)


def fetch_pairs(
    dex_client: httpx.Client,
    mints: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not mints:
        return {}

    # One request per cycle, at most 30 mints.
    chunk = mints[:30]
    response = dex_client.get(
        f"/tokens/v1/solana/{','.join(chunk)}",
        headers={
            "Accept": "application/json",
            "User-Agent": "SOLPULSE-Rate-Limit-Recovery/11.3.1",
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
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for pair in payload:
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken") or {}
        mint = str(base.get("address") or "")
        if mint in chunk:
            grouped.setdefault(mint, []).append(pair)
    return grouped


def decode_pumpswap_pool(
    account: dict[str, Any] | None,
    expected_mint: str,
) -> dict[str, str] | None:
    if not account:
        return None
    if str(account.get("owner") or "") != PUMP_AMM_PROGRAM_ID:
        return None

    try:
        raw = base64.b64decode(account["data"][0])
    except Exception:
        return None

    # Anchor discriminator + current PumpSwap Pool layout:
    # base_mint offset 43, quote_mint 75,
    # pool_base_token_account 139, pool_quote_token_account 171.
    if len(raw) < 203:
        return None

    base_mint = b58encode(raw[43:75])
    quote_mint = b58encode(raw[75:107])
    pool_base = b58encode(raw[139:171])
    pool_quote = b58encode(raw[171:203])

    if base_mint != expected_mint:
        return None

    return {
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "pool_base_token_account": pool_base,
        "pool_quote_token_account": pool_quote,
    }


def fetch_pumpswap_pool_accounts(
    rpc_client: httpx.Client,
    pairs_by_mint: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    pair_addresses: list[str] = []
    mint_by_pair: dict[str, str] = {}

    for mint, pair in pairs_by_mint.items():
        dex_id = str(pair.get("dexId") or "").lower()
        pair_address = str(pair.get("pairAddress") or "")
        if "pump" not in dex_id or not pair_address:
            continue
        pair_addresses.append(pair_address)
        mint_by_pair[pair_address] = mint

    result: dict[str, dict[str, str]] = {}
    for start in range(0, len(pair_addresses), 100):
        chunk = pair_addresses[start : start + 100]
        rpc_result = rpc_call(
            rpc_client,
            "getMultipleAccounts",
            [
                chunk,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                },
            ],
        )
        accounts = (rpc_result or {}).get("value") or []

        for address, account in zip(chunk, accounts):
            mint = mint_by_pair[address]
            decoded = decode_pumpswap_pool(account, mint)
            if decoded:
                result[mint] = decoded

    return result


def pair_metrics(
    pair: dict[str, Any],
    sol_price_usd: float,
) -> dict[str, Any]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    liquidity = pair.get("liquidity") or {}

    price_usd = to_float(pair.get("priceUsd"))
    price_native = to_float(pair.get("priceNative"))
    quote_address = str(quote.get("address") or "")
    quote_symbol = str(quote.get("symbol") or "").upper()

    if (
        quote_address == WSOL_MINT
        or quote_symbol in {"SOL", "WSOL"}
    ):
        price_sol = price_native
    elif price_usd > 0 and sol_price_usd > 0:
        price_sol = price_usd / sol_price_usd
    else:
        price_sol = 0.0

    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}

    return {
        "token_name": str(base.get("name") or ""),
        "symbol": str(base.get("symbol") or ""),
        "price_sol": price_sol,
        "price_usd": price_usd,
        "change_5m_pct": to_float(changes.get("m5")),
        "change_h1_pct": to_float(changes.get("h1")),
        "liquidity_usd": to_float(liquidity.get("usd")),
        "volume_5m_usd": to_float(volume.get("m5")),
        "volume_h1_usd": to_float(volume.get("h1")),
        "buys_5m": to_int(m5.get("buys")),
        "sells_5m": to_int(m5.get("sells")),
        "buys_h1": to_int(h1.get("buys")),
        "sells_h1": to_int(h1.get("sells")),
        "market_cap_usd": to_float(pair.get("marketCap")),
        "fdv_usd": to_float(pair.get("fdv")),
        "pair_created_at": to_int(pair.get("pairCreatedAt")),
        "pair_address": str(pair.get("pairAddress") or ""),
        "dex_id": str(pair.get("dexId") or ""),
        "source_url": str(pair.get("url") or ""),
        "quote_mint": str(quote.get("address") or ""),
        "quote_symbol": quote_symbol,
    }


def update_database(
    launches: list[sqlite3.Row],
    selected_pairs: dict[str, dict[str, Any]],
    pool_details: dict[str, dict[str, str]],
    sol_price_usd: float,
) -> tuple[int, int]:
    pairs_found = 0
    migrated_count = 0
    timestamp = now_iso()

    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")

        for launch in launches:
            mint = str(launch["mint"])
            pair = selected_pairs.get(mint)

            if not pair:
                if not launch["pair_address"]:
                    connection.execute(
                        """
                        UPDATE new_launches
                        SET market_data_status='NO_PAIR'
                        WHERE mint=?
                        """,
                        (mint,),
                    )
                continue

            metrics = pair_metrics(pair, sol_price_usd)
            if metrics["price_sol"] <= 0:
                continue

            pairs_found += 1
            complete = bool(launch["complete"])
            dex_id_lower = str(
                metrics["dex_id"] or ""
            ).lower()
            migrated = (
                complete
                or str(launch["market_mode"] or "")
                == "MIGRATED_DEX"
                or str(launch["lifecycle_state"] or "")
                in {"CURVE_COMPLETE", "GRADUATING", "MIGRATED"}
                or (
                    "pump" in dex_id_lower
                    and mint in pool_details
                )
            )
            if migrated:
                lifecycle = "MIGRATED"
                market_mode = "MIGRATED_DEX"
                migrated_count += 1
            else:
                lifecycle = (
                    str(launch["lifecycle_state"] or "DEX_ACTIVE")
                    if str(launch["lifecycle_state"] or "")
                    == "BONDING"
                    else "DEX_ACTIVE"
                )
                market_mode = "DEX_ACTIVE"

            pool = pool_details.get(mint, {})
            migrated_at = (
                launch["migrated_at"]
                or (
                    datetime.fromtimestamp(
                        metrics["pair_created_at"] / 1000,
                        tz=timezone.utc,
                    ).isoformat()
                    if metrics["pair_created_at"] > 0
                    else timestamp
                )
                if migrated
                else launch["migrated_at"]
            )

            connection.execute(
                """
                UPDATE new_launches
                SET lifecycle_state=?,
                    market_mode=?,
                    migrated_at=?,
                    market_last_updated_at=?,
                    pair_address=?,
                    dex_id=?,
                    pair_url=?,
                    pool_base_token_account=COALESCE(?, pool_base_token_account),
                    pool_quote_token_account=COALESCE(?, pool_quote_token_account),
                    market_price_sol=?,
                    market_price_usd=?,
                    market_change_5m_pct=?,
                    market_change_h1_pct=?,
                    market_liquidity_usd=?,
                    market_volume_5m_usd=?,
                    market_volume_h1_usd=?,
                    market_buys_5m=?,
                    market_sells_5m=?,
                    market_cap_usd=?,
                    market_fdv_usd=?,
                    pair_created_at=?,
                    market_data_status='OK'
                WHERE mint=?
                """,
                (
                    lifecycle,
                    market_mode,
                    migrated_at,
                    timestamp,
                    metrics["pair_address"],
                    metrics["dex_id"],
                    metrics["source_url"],
                    pool.get("pool_base_token_account"),
                    pool.get("pool_quote_token_account"),
                    metrics["price_sol"],
                    metrics["price_usd"],
                    metrics["change_5m_pct"],
                    metrics["change_h1_pct"],
                    metrics["liquidity_usd"],
                    metrics["volume_5m_usd"],
                    metrics["volume_h1_usd"],
                    metrics["buys_5m"],
                    metrics["sells_5m"],
                    metrics["market_cap_usd"],
                    metrics["fdv_usd"],
                    metrics["pair_created_at"],
                    mint,
                ),
            )

            connection.execute(
                """
                INSERT INTO market_snapshots (
                    timestamp, token_mint, token_name,
                    symbol, price_sol, price_usd,
                    change_1m_pct, change_5m_pct,
                    change_h1_pct, liquidity_usd,
                    volume_5m_usd, volume_h1_usd,
                    buys_5m, sells_5m,
                    market_cap_usd, fdv_usd,
                    pair_created_at, pair_address,
                    dex_id, source_url, score,
                    data_status
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, NULL, 'OK')
                """,
                (
                    timestamp,
                    mint,
                    metrics["token_name"]
                    or launch["name"],
                    metrics["symbol"]
                    or launch["symbol"]
                    or "UNKNOWN",
                    metrics["price_sol"],
                    metrics["price_usd"],
                    metrics["change_5m_pct"],
                    metrics["change_h1_pct"],
                    metrics["liquidity_usd"],
                    metrics["volume_5m_usd"],
                    metrics["volume_h1_usd"],
                    metrics["buys_5m"],
                    metrics["sells_5m"],
                    metrics["market_cap_usd"],
                    metrics["fdv_usd"],
                    metrics["pair_created_at"],
                    metrics["pair_address"],
                    metrics["dex_id"],
                    metrics["source_url"],
                ),
            )

        connection.execute(
            """
            DELETE FROM market_snapshots
            WHERE id NOT IN (
                SELECT id
                FROM market_snapshots
                ORDER BY id DESC
                LIMIT 100000
            )
            """
        )

        write_state(connection, "hybrid_market_status", "RUNNING")
        write_state(
            connection,
            "hybrid_market_last_scan",
            timestamp,
        )
        write_state(
            connection,
            "hybrid_market_last_error",
            "",
        )
        write_state(
            connection,
            "hybrid_market_pairs_found",
            pairs_found,
        )
        write_state(
            connection,
            "hybrid_market_migrated_count",
            migrated_count,
        )
        connection.commit()

    return pairs_found, migrated_count


def set_market_state(
    status: str,
    *,
    error: str = "",
    cooldown_until_iso: str = "",
) -> None:
    try:
        with connect_db() as connection:
            write_state(connection, "hybrid_market_status", status)
            write_state(
                connection,
                "hybrid_market_last_error",
                error[:500],
            )
            write_state(
                connection,
                "hybrid_market_cooldown_until",
                cooldown_until_iso,
            )
            connection.commit()
    except Exception:
        pass


def update_error(message: str) -> None:
    try:
        with connect_db() as connection:
            write_state(
                connection,
                "hybrid_market_status",
                "ERROR",
            )
            write_state(
                connection,
                "hybrid_market_last_error",
                message[:500],
            )
            connection.commit()
    except Exception:
        pass


def run_cycle(
    rpc_client: httpx.Client,
    dex_client: httpx.Client,
    config: dict[str, Any],
) -> tuple[int, int]:
    global dex_cooldown_until

    if time.monotonic() < dex_cooldown_until:
        return 0, 0

    launches = load_tracked_launches(
        min(30, int(config["hybrid_market_track_limit"]))
    )
    if not launches:
        set_market_state("IDLE")
        return 0, 0

    mints = [str(row["mint"]) for row in launches]
    raw_pairs = fetch_pairs(dex_client, mints)
    selected: dict[str, dict[str, Any]] = {}

    for launch in launches:
        mint = str(launch["mint"])
        pair = choose_pair(
            raw_pairs.get(mint, []),
            mint,
            bool(config.get("hybrid_market_prefer_pumpswap", True)),
        )
        if pair:
            selected[mint] = pair

    pool_details: dict[str, dict[str, str]] = {}
    if selected:
        try:
            pool_details = fetch_pumpswap_pool_accounts(
                rpc_client,
                selected,
            )
        except Exception as error:
            print(f"Décodage PumpSwap reporté : {error}")

    result = update_database(
        launches,
        selected,
        pool_details,
        get_sol_price(),
    )
    set_market_state("RUNNING")
    return result


def main() -> None:
    global dex_cooldown_until
    if not DB_PATH.exists():
        print("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    config = load_json(CONFIG_PATH)
    if not config.get("hybrid_market_enabled", True):
        print("Le Hybrid Market Scanner est désactivé.")
        return

    rpc_url = os.getenv(
        "SOLANA_RPC_URL",
        str(config.get("solana_rpc_url")),
    )
    interval = float(
        config.get("hybrid_market_scan_seconds", 5)
    )

    acquire_lock()
    print("=" * 76)
    print("SOLPULSE V12.2 — RATE-LIMIT SAFE DEX SCANNER")
    print("=" * 76)
    print("DEX interrogé seulement pour les migrations plausibles et paires actives.")
    print("Source marché : DEX Screener.")
    print("Décodage PumpSwap : pool et token vault de liquidité.")
    print("Paper trading uniquement.")
    print()

    rpc_client = httpx.Client(
        base_url=rpc_url,
        timeout=httpx.Timeout(20.0),
    )
    dex_client = httpx.Client(
        base_url=DEXSCREENER_BASE_URL,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    )

    try:
        while running:
            started = time.monotonic()
            LOCK_PATH.touch(exist_ok=True)

            try:
                pairs, migrated = run_cycle(
                    rpc_client,
                    dex_client,
                    config,
                )
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} | "
                    f"{pairs} paire(s) | {migrated} migré(s)"
                )
            except KeyboardInterrupt:
                break
            except DexRateLimitError as error:
                cooldown = int(
                    config.get(
                        "hybrid_market_cooldown_seconds",
                        error.retry_after_seconds,
                    )
                )
                cooldown = max(cooldown, error.retry_after_seconds)
                dex_cooldown_until = time.monotonic() + cooldown
                resume_at = datetime.fromtimestamp(
                    time.time() + cooldown,
                    tz=timezone.utc,
                ).isoformat()
                message = (
                    "DEX Screener a limité les requêtes (429). "
                    f"Pause automatique de {cooldown} s."
                )
                print(message)
                set_market_state(
                    "COOLDOWN",
                    error=message,
                    cooldown_until_iso=resume_at,
                )
            except Exception as error:
                cooldown = int(
                    config.get(
                        "hybrid_market_error_cooldown_seconds",
                        20,
                    )
                )
                dex_cooldown_until = time.monotonic() + cooldown
                message = str(error)
                print(f"Erreur Hybrid Market : {message}")
                update_error(message)

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    finally:
        rpc_client.close()
        dex_client.close()
        try:
            with connect_db() as connection:
                write_state(
                    connection,
                    "hybrid_market_status",
                    "STOPPED",
                )
                connection.commit()
        except Exception:
            pass
        release_lock()
        print("Hybrid Market Scanner arrêté proprement.")


if __name__ == "__main__":
    main()
