
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "trading.db"
CONFIG_PATH = BASE_DIR / "config.json"


@dataclass
class SimulatedTrade:
    mint: str
    symbol: str
    entry_at: str
    exit_at: str
    entry_price_sol: float
    exit_price_sol: float
    position_size_sol: float
    tokens_received: float
    entry_safety_score: float
    entry_qualification_score: float
    entry_progress_pct: float
    exit_progress_pct: float
    pnl_sol: float
    pnl_pct: float
    exit_reason: str
    holding_seconds: float


@dataclass
class BacktestSummary:
    run_id: int | None
    created_at: str
    name: str
    sample_count: int
    candidate_count: int
    trade_count: int
    wins: int
    losses: int
    win_rate: float
    pnl_sol: float
    return_pct: float
    max_drawdown_sol: float
    ending_equity_sol: float
    status: str
    notes: str
    trades: list[SimulatedTrade]


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


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def default_parameters() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    return {
        "initial_capital_sol": float(
            config.get("backtest_default_capital_sol", 1.0)
        ),
        "position_size_sol": float(
            config.get(
                "backtest_default_position_size_sol",
                0.02,
            )
        ),
        "observation_seconds": float(
            config.get(
                "backtest_default_observation_seconds",
                90,
            )
        ),
        "min_samples": int(
            config.get("backtest_default_min_samples", 5)
        ),
        "min_safety_score": float(
            config.get(
                "backtest_default_min_safety_score",
                72,
            )
        ),
        "min_progress_pct": float(
            config.get(
                "backtest_default_min_progress_pct",
                3,
            )
        ),
        "max_progress_pct": float(
            config.get(
                "backtest_default_max_progress_pct",
                90,
            )
        ),
        "min_progress_delta_pct": float(
            config.get(
                "backtest_default_min_progress_delta_pct",
                0.10,
            )
        ),
        "max_price_change_pct": float(
            config.get(
                "backtest_default_max_price_change_pct",
                35,
            )
        ),
        "stop_loss_pct": float(
            config.get(
                "backtest_default_stop_loss_pct",
                -8,
            )
        ),
        "take_profit_pct": float(
            config.get(
                "backtest_default_take_profit_pct",
                15,
            )
        ),
        "max_holding_minutes": float(
            config.get(
                "backtest_default_max_holding_minutes",
                20,
            )
        ),
        "curve_fee_bps": int(
            config.get(
                "backtest_default_curve_fee_bps",
                125,
            )
        ),
        "network_fee_sol": 0.000005,
        "min_qualification_score": float(
            config.get("qualification_min_score", 78)
        ),
        "min_stable_ratio": float(
            config.get("qualification_min_stable_ratio", 0.80)
        ),
        "fast_observation_seconds": float(
            config.get(
                "qualification_fast_observation_seconds",
                60,
            )
        ),
        "fast_progress_delta_pct": float(
            config.get(
                "qualification_fast_progress_delta_pct",
                3.0,
            )
        ),
        "fast_max_price_change_pct": float(
            config.get(
                "qualification_fast_max_price_change_pct",
                15,
            )
        ),
        "break_even_trigger_pct": float(
            config.get("backtest_break_even_trigger_pct", 50)
        ),
        "break_even_floor_pct": float(
            config.get("backtest_break_even_floor_pct", 1.0)
        ),
        "hybrid_observation_seconds": float(
            config.get("hybrid_market_observation_seconds", 60)
        ),
        "hybrid_min_samples": int(
            config.get("hybrid_market_min_samples", 6)
        ),
        "hybrid_min_liquidity_usd": float(
            config.get(
                "hybrid_market_min_liquidity_usd",
                10000,
            )
        ),
        "hybrid_min_volume_5m_usd": float(
            config.get(
                "hybrid_market_min_volume_5m_usd",
                1000,
            )
        ),
        "hybrid_min_buys_5m": int(
            config.get("hybrid_market_min_buys_5m", 8)
        ),
        "hybrid_min_sells_5m": int(
            config.get("hybrid_market_min_sells_5m", 2)
        ),
        "hybrid_max_buy_sell_ratio": float(
            config.get(
                "hybrid_market_max_buy_sell_ratio",
                8,
            )
        ),
        "hybrid_max_price_change_pct": float(
            config.get(
                "hybrid_market_max_price_change_pct",
                30,
            )
        ),
        "hybrid_trade_fee_bps": int(
            config.get("hybrid_market_trade_fee_bps", 30)
        ),
        "hybrid_slippage_bps": int(
            config.get("hybrid_market_slippage_bps", 50)
        ),
        "exclude_mayhem": bool(
            config.get("backtest_exclude_mayhem", True)
        ),
        "require_safety_qualified": True,
        "require_complete_analysis": True,
        "one_trade_per_mint": True,
    }


def connect_db(
    db_path: Path = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def load_samples(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT *
        FROM research_samples
        ORDER BY mint, datetime(timestamp), id
        """
    ).fetchall()


def group_samples(
    samples: list[sqlite3.Row],
) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for sample in samples:
        groups.setdefault(
            str(sample["mint"]),
            [],
        ).append(sample)
    return groups


def is_safety_eligible(
    sample: sqlite3.Row,
    parameters: dict[str, Any],
) -> bool:
    score = to_float(sample["safety_score"])
    hard_reject = bool(sample["safety_hard_reject"])

    if parameters.get("exclude_mayhem", True):
        if "is_mayhem_mode" not in sample.keys():
            return False
        if sample["is_mayhem_mode"] is None:
            return False
        if bool(sample["is_mayhem_mode"]):
            return False

    if hard_reject:
        return False

    if (
        parameters["require_safety_qualified"]
        and sample["safety_decision"] != "QUALIFIED"
    ):
        return False

    return score >= float(parameters["min_safety_score"])


def is_market_eligible(
    sample: sqlite3.Row,
    parameters: dict[str, Any],
) -> bool:
    price = to_float(sample["curve_price_sol"])
    mode = str(
        sample["market_mode"]
        if "market_mode" in sample.keys()
        else "BONDING"
    )

    if mode == "MIGRATED_DEX":
        liquidity = to_float(sample["liquidity_usd"])
        volume = to_float(sample["volume_5m_usd"])
        buys = to_int(sample["buys_5m"])
        sells = to_int(sample["sells_5m"])
        ratio = (
            buys / sells
            if sells > 0
            else float("inf")
        )
        return (
            price > 0
            and liquidity
            >= float(parameters["hybrid_min_liquidity_usd"])
            and volume
            >= float(parameters["hybrid_min_volume_5m_usd"])
            and buys >= int(parameters["hybrid_min_buys_5m"])
            and sells >= int(parameters["hybrid_min_sells_5m"])
            and ratio
            <= float(parameters["hybrid_max_buy_sell_ratio"])
        )

    progress = to_float(sample["progress_pct"], -1)
    return (
        sample["lifecycle_state"] == "BONDING"
        and price > 0
        and float(parameters["min_progress_pct"])
        <= progress
        <= float(parameters["max_progress_pct"])
    )


def find_entry_index(
    samples: list[sqlite3.Row],
    parameters: dict[str, Any],
) -> tuple[int | None, int]:
    anchor_index: int | None = None
    anchor_time: datetime | None = None
    anchor_progress = 0.0
    anchor_price = 0.0
    stable_samples = 0
    candidate_seen = 0
    anchor_mode = ""

    for index, sample in enumerate(samples):
        safety_ok = is_safety_eligible(
            sample,
            parameters,
        )
        market_ok = is_market_eligible(
            sample,
            parameters,
        )
        mode = str(
            sample["market_mode"]
            if "market_mode" in sample.keys()
            else "BONDING"
        )

        if safety_ok:
            candidate_seen = 1

        if not (safety_ok and market_ok):
            anchor_index = None
            anchor_time = None
            stable_samples = 0
            anchor_mode = ""
            continue

        current_time = parse_time(sample["timestamp"])
        progress = to_float(sample["progress_pct"])
        price = to_float(sample["curve_price_sol"])

        if anchor_index is None or anchor_mode != mode:
            anchor_index = index
            anchor_time = current_time
            anchor_progress = progress
            anchor_price = price
            stable_samples = 1
            anchor_mode = mode
        else:
            stable_samples += 1

        elapsed = (
            current_time - anchor_time
        ).total_seconds()
        progress_delta = progress - anchor_progress
        price_change = (
            (price / anchor_price - 1.0) * 100.0
            if anchor_price > 0
            else 0.0
        )
        observed_samples = index - anchor_index + 1
        stable_ratio = (
            stable_samples / max(observed_samples, 1)
        )
        qualification_value = to_float(
            sample["qualification_score"]
        )

        if mode == "MIGRATED_DEX":
            checks = [
                elapsed
                >= float(
                    parameters["hybrid_observation_seconds"]
                ),
                stable_samples
                >= int(parameters["hybrid_min_samples"]),
                stable_ratio
                >= float(parameters["min_stable_ratio"]),
                abs(price_change)
                <= float(
                    parameters[
                        "hybrid_max_price_change_pct"
                    ]
                ),
                qualification_value
                >= float(parameters["min_qualification_score"]),
            ]
        else:
            fast_track = (
                progress_delta
                >= float(
                    parameters["fast_progress_delta_pct"]
                )
                and 0 <= price_change
                <= float(
                    parameters[
                        "fast_max_price_change_pct"
                    ]
                )
                and stable_ratio >= 0.85
                and stable_samples
                >= int(parameters["min_samples"])
            )
            required_observation = (
                float(parameters["fast_observation_seconds"])
                if fast_track
                else float(parameters["observation_seconds"])
            )
            checks = [
                elapsed >= required_observation,
                stable_samples
                >= int(parameters["min_samples"]),
                stable_ratio
                >= float(parameters["min_stable_ratio"]),
                progress_delta
                >= float(
                    parameters["min_progress_delta_pct"]
                ),
                abs(price_change)
                <= float(parameters["max_price_change_pct"]),
                qualification_value
                >= float(parameters["min_qualification_score"]),
            ]

        if all(checks):
            return index, candidate_seen

    return None, candidate_seen

def simulate_trade(
    samples: list[sqlite3.Row],
    entry_index: int,
    parameters: dict[str, Any],
) -> SimulatedTrade:
    entry = samples[entry_index]
    position_size = float(parameters["position_size_sol"])
    entry_mode = str(
        entry["market_mode"]
        if "market_mode" in entry.keys()
        else "BONDING"
    )
    effective_fee_bps = (
        int(parameters["hybrid_trade_fee_bps"])
        + int(parameters["hybrid_slippage_bps"])
        if entry_mode == "MIGRATED_DEX"
        else int(parameters["curve_fee_bps"])
    )
    fee_rate = effective_fee_bps / 10_000.0
    network_fee = float(parameters["network_fee_sol"])

    entry_price = to_float(entry["curve_price_sol"])
    investable = position_size * (1.0 - fee_rate)
    tokens = investable / entry_price
    entry_total_cost = position_size + network_fee

    exit_sample = samples[-1]
    exit_reason = "END_OF_DATA"
    exit_value = (
        tokens
        * to_float(exit_sample["curve_price_sol"], entry_price)
        * (1.0 - fee_rate)
        - network_fee
    )
    peak_pnl_pct = -100.0
    break_even_armed = False
    break_even_trigger = float(
        parameters["break_even_trigger_pct"]
    )
    break_even_floor = float(
        parameters["break_even_floor_pct"]
    )

    entry_time = parse_time(entry["timestamp"])
    max_holding_seconds = (
        float(parameters["max_holding_minutes"]) * 60.0
    )

    for sample in samples[entry_index + 1 :]:
        price = to_float(sample["curve_price_sol"])
        if price <= 0:
            continue

        current_time = parse_time(sample["timestamp"])
        holding_seconds = (
            current_time - entry_time
        ).total_seconds()
        current_value = (
            tokens * price * (1.0 - fee_rate)
            - network_fee
        )
        pnl_pct = (
            (current_value - entry_total_cost)
            / entry_total_cost
            * 100.0
        )

        safety_rejected = (
            sample["safety_decision"] == "REJECTED"
            or bool(sample["safety_hard_reject"])
        )
        curve_complete = (
            sample["lifecycle_state"]
            in {"CURVE_COMPLETE", "GRADUATING", "BONDED"}
        )

        peak_pnl_pct = max(peak_pnl_pct, pnl_pct)
        if peak_pnl_pct >= break_even_trigger:
            break_even_armed = True

        reason: str | None = None
        if safety_rejected:
            reason = "SAFETY_DOWNGRADE"
        elif break_even_armed and pnl_pct <= break_even_floor:
            reason = "BREAK_EVEN_STOP"
        elif pnl_pct <= float(parameters["stop_loss_pct"]):
            reason = "STOP_LOSS"
        elif pnl_pct >= float(parameters["take_profit_pct"]):
            reason = "TAKE_PROFIT"
        elif curve_complete:
            reason = "CURVE_COMPLETE"
        elif holding_seconds >= max_holding_seconds:
            reason = "TIME_EXIT"

        if reason:
            exit_sample = sample
            exit_reason = reason
            exit_value = current_value
            break

    exit_price = to_float(
        exit_sample["curve_price_sol"],
        entry_price,
    )
    exit_time = parse_time(exit_sample["timestamp"])
    holding_seconds = max(
        0.0,
        (exit_time - entry_time).total_seconds(),
    )
    pnl_sol = exit_value - entry_total_cost
    pnl_pct = (
        pnl_sol / entry_total_cost * 100.0
        if entry_total_cost > 0
        else 0.0
    )

    return SimulatedTrade(
        mint=str(entry["mint"]),
        symbol=str(entry["symbol"] or ""),
        entry_at=str(entry["timestamp"]),
        exit_at=str(exit_sample["timestamp"]),
        entry_price_sol=entry_price,
        exit_price_sol=exit_price,
        position_size_sol=position_size,
        tokens_received=tokens,
        entry_safety_score=to_float(
            entry["safety_score"]
        ),
        entry_qualification_score=to_float(
            entry["qualification_score"]
        ),
        entry_progress_pct=to_float(
            entry["progress_pct"]
        ),
        exit_progress_pct=to_float(
            exit_sample["progress_pct"]
        ),
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        holding_seconds=holding_seconds,
    )


def calculate_max_drawdown(
    initial_capital: float,
    trades: list[SimulatedTrade],
) -> float:
    equity = initial_capital
    peak = equity
    max_drawdown = 0.0

    for trade in sorted(
        trades,
        key=lambda item: item.exit_at,
    ):
        equity += trade.pnl_sol
        peak = max(peak, equity)
        max_drawdown = min(
            max_drawdown,
            equity - peak,
        )

    return max_drawdown


def persist_run(
    connection: sqlite3.Connection,
    name: str,
    parameters: dict[str, Any],
    summary: BacktestSummary,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO backtest_runs (
            created_at, name, parameters_json,
            sample_count, candidate_count,
            trade_count, win_rate, pnl_sol,
            return_pct, max_drawdown_sol,
            ending_equity_sol, status, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary.created_at,
            name,
            json.dumps(
                parameters,
                ensure_ascii=False,
                sort_keys=True,
            ),
            summary.sample_count,
            summary.candidate_count,
            summary.trade_count,
            summary.win_rate,
            summary.pnl_sol,
            summary.return_pct,
            summary.max_drawdown_sol,
            summary.ending_equity_sol,
            summary.status,
            summary.notes,
        ),
    )
    run_id = int(cursor.lastrowid)

    for trade in summary.trades:
        connection.execute(
            """
            INSERT INTO backtest_trades (
                run_id, mint, symbol,
                entry_at, exit_at,
                entry_price_sol, exit_price_sol,
                position_size_sol, tokens_received,
                entry_safety_score,
                entry_qualification_score,
                entry_progress_pct,
                exit_progress_pct,
                pnl_sol, pnl_pct,
                exit_reason, holding_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                trade.mint,
                trade.symbol,
                trade.entry_at,
                trade.exit_at,
                trade.entry_price_sol,
                trade.exit_price_sol,
                trade.position_size_sol,
                trade.tokens_received,
                trade.entry_safety_score,
                trade.entry_qualification_score,
                trade.entry_progress_pct,
                trade.exit_progress_pct,
                trade.pnl_sol,
                trade.pnl_pct,
                trade.exit_reason,
                trade.holding_seconds,
            ),
        )

    connection.commit()
    return run_id


def run_backtest(
    parameters: dict[str, Any] | None = None,
    name: str = "Stable Paper Pilot V12 Replay",
    db_path: Path = DEFAULT_DB_PATH,
    save: bool = True,
) -> BacktestSummary:
    merged_parameters = default_parameters()
    if parameters:
        merged_parameters.update(parameters)

    with connect_db(db_path) as connection:
        samples = load_samples(connection)
        groups = group_samples(samples)

        trades: list[SimulatedTrade] = []
        candidate_count = 0

        for mint_samples in groups.values():
            entry_index, candidate_seen = find_entry_index(
                mint_samples,
                merged_parameters,
            )
            candidate_count += candidate_seen

            if entry_index is None:
                continue

            trades.append(
                simulate_trade(
                    mint_samples,
                    entry_index,
                    merged_parameters,
                )
            )

        initial_capital = float(
            merged_parameters["initial_capital_sol"]
        )
        total_pnl = sum(
            trade.pnl_sol
            for trade in trades
        )
        ending_equity = initial_capital + total_pnl
        wins = sum(
            1 for trade in trades
            if trade.pnl_sol > 0
        )
        losses = sum(
            1 for trade in trades
            if trade.pnl_sol < 0
        )
        win_rate = (
            wins / len(trades) * 100.0
            if trades
            else 0.0
        )
        return_pct = (
            total_pnl / initial_capital * 100.0
            if initial_capital > 0
            else 0.0
        )
        max_drawdown = calculate_max_drawdown(
            initial_capital,
            trades,
        )

        status = (
            "COMPLETED"
            if samples
            else "NO_DATA"
        )
        notes = (
            "Approximation événementielle à partir des prix de courbe "
            "enregistrés. Pas de reconstruction transaction par transaction."
        )

        summary = BacktestSummary(
            run_id=None,
            created_at=now_iso(),
            name=name,
            sample_count=len(samples),
            candidate_count=candidate_count,
            trade_count=len(trades),
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            pnl_sol=total_pnl,
            return_pct=return_pct,
            max_drawdown_sol=max_drawdown,
            ending_equity_sol=ending_equity,
            status=status,
            notes=notes,
            trades=trades,
        )

        if save:
            run_id = persist_run(
                connection,
                name,
                merged_parameters,
                summary,
            )
            summary.run_id = run_id

        return summary


def summary_as_dict(
    summary: BacktestSummary,
) -> dict[str, Any]:
    payload = asdict(summary)
    payload["trades"] = [
        asdict(trade)
        for trade in summary.trades
    ]
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SOLPULSE V12 Stable Paper Pilot Replay & Backtest"
    )
    parser.add_argument(
        "--name",
        default="Backtest CLI V12",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
    )
    args = parser.parse_args()

    summary = run_backtest(
        name=args.name,
        save=not args.no_save,
    )
    print(
        json.dumps(
            summary_as_dict(summary),
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
