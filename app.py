
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backtest_engine import default_parameters, run_backtest
from runtime_utils import connect_sqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trading.db"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
CONFIG_PATH = BASE_DIR / "config.json"

st.set_page_config(
    page_title="SOLPULSE STABLE PAPER PILOT V12.2",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {
        --bg:#07090f;--panel:#10141f;--border:rgba(255,255,255,.075);
        --text:#f4f6fb;--muted:#8f98ac;--green:#25e6a5;
        --red:#ff5c78;--purple:#9b7cff;--cyan:#32d7ff;--amber:#ffbf69;
    }
    .stApp {
        background:
          radial-gradient(circle at 82% 2%,rgba(137,105,255,.11),transparent 29rem),
          radial-gradient(circle at 18% 0%,rgba(37,230,165,.055),transparent 25rem),
          var(--bg);
        color:var(--text);
    }
    [data-testid="stHeader"]{background:transparent}
    [data-testid="stSidebar"]{background:#0a0d14;border-right:1px solid var(--border)}
    .block-container{max-width:1700px;padding-top:1.1rem;padding-bottom:3rem}
    .brand{display:flex;align-items:center;gap:.75rem;margin:.15rem 0 .4rem}
    .brand-mark{
        display:grid;place-items:center;width:42px;height:42px;border-radius:13px;
        background:linear-gradient(145deg,var(--green),var(--purple));
        color:#07100e;font-weight:900
    }
    .brand-title{font-size:1.32rem;font-weight:760;letter-spacing:-.04em}
    .brand-subtitle,.muted,.section-note{color:var(--muted);font-size:.78rem}
    .topbar{
        display:flex;justify-content:space-between;align-items:flex-end;
        gap:1rem;margin-bottom:1rem
    }
    .kicker{
        color:var(--green);font-weight:700;font-size:.72rem;
        letter-spacing:.15em;text-transform:uppercase
    }
    .page-title{
        font-size:clamp(1.7rem,4vw,2.7rem);font-weight:780;
        letter-spacing:-.055em;margin:.08rem 0
    }
    .subtitle{color:var(--muted);font-size:.92rem}
    .status-online,.status-offline,.status-warn{
        display:inline-flex;align-items:center;gap:.45rem;padding:.48rem .72rem;
        border-radius:999px;font-size:.78rem;white-space:nowrap
    }
    .status-online{color:var(--green);background:rgba(37,230,165,.08);border:1px solid rgba(37,230,165,.25)}
    .status-offline{color:var(--red);background:rgba(255,92,120,.08);border:1px solid rgba(255,92,120,.25)}
    .status-warn{color:var(--amber);background:rgba(255,191,105,.08);border:1px solid rgba(255,191,105,.25)}
    .dot-online,.dot-offline,.dot-warn{width:7px;height:7px;border-radius:50%}
    .dot-online{background:var(--green);box-shadow:0 0 11px var(--green)}
    .dot-offline{background:var(--red);box-shadow:0 0 11px var(--red)}
    .dot-warn{background:var(--amber);box-shadow:0 0 11px var(--amber)}
    div[data-testid="stMetric"]{
        background:linear-gradient(145deg,rgba(21,26,39,.96),rgba(12,15,24,.96));
        border:1px solid var(--border);border-radius:18px;
        padding:15px 16px;min-height:112px
    }
    div[data-testid="stMetricLabel"]{
        color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.08em
    }
    div[data-testid="stMetricValue"]{font-size:1.45rem;font-weight:670;letter-spacing:-.035em}
    .section-head{
        display:flex;justify-content:space-between;align-items:baseline;
        margin:1.2rem 0 .55rem
    }
    .section-title{font-size:1rem;font-weight:650}
    .panel{
        background:linear-gradient(145deg,rgba(21,26,39,.9),rgba(11,14,22,.9));
        border:1px solid var(--border);border-radius:18px;padding:1rem
    }
    .positive{color:var(--green);font-weight:650}
    .negative{color:var(--red);font-weight:650}
    [data-testid="stDataFrame"]{border:1px solid var(--border);border-radius:16px;overflow:hidden}
    .live-note{color:var(--muted);font-size:.76rem;text-align:right;margin-top:.35rem}
    @media(max-width:800px){.topbar{align-items:flex-start;flex-direction:column}}
    </style>
    """,
    unsafe_allow_html=True,
)

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#9da5b8", family="Inter, system-ui, sans-serif"),
    margin=dict(l=8, r=8, t=38, b=8),
    hoverlabel=dict(
        bgcolor="#171b27",
        font_color="#f4f6fb",
        bordercolor="#2a3040",
    ),
)


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with connect_sqlite(
        DB_PATH,
        timeout_seconds=30,
    ) as connection:
        return pd.read_sql_query(
            sql,
            connection,
            params=params,
        )


def load_data():
    return (
        query(
            """
            SELECT
                positions.*,
                risk.peak_pnl_pct,
                risk.break_even_armed,
                risk.active_stop_pct
            FROM positions
            LEFT JOIN position_risk_state risk
                ON risk.position_id = positions.id
            ORDER BY datetime(positions.opened_at) DESC
            """
        ),
        query("SELECT * FROM portfolio_snapshots ORDER BY datetime(timestamp)"),
        query("SELECT * FROM signals ORDER BY datetime(timestamp) DESC LIMIT 500"),
        query("SELECT * FROM paper_orders ORDER BY datetime(timestamp) DESC LIMIT 500"),
        query("SELECT * FROM market_snapshots ORDER BY datetime(timestamp) DESC LIMIT 15000"),
        query("SELECT * FROM bonding_snapshots ORDER BY datetime(timestamp) DESC LIMIT 15000"),
        query("SELECT * FROM new_launches ORDER BY datetime(detected_at) DESC LIMIT 1000"),
        query("SELECT * FROM safety_assessments ORDER BY datetime(assessed_at) DESC LIMIT 1000"),
        query("SELECT * FROM qualification_candidates ORDER BY datetime(updated_at) DESC LIMIT 1000"),
        query("SELECT * FROM qualification_events ORDER BY datetime(timestamp) DESC LIMIT 1000"),
        query("SELECT * FROM bot_state"),
    )


def parse_dates(frame: pd.DataFrame, columns: list[str]) -> None:
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(
                frame[column],
                errors="coerce",
                utc=True,
            )


def latest_per_mint(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    return (
        frame.sort_values("timestamp")
        .groupby("token_mint", as_index=False)
        .tail(1)
    )


def state_map(frame: pd.DataFrame) -> dict[str, str]:
    if frame.empty:
        return {}
    return {
        str(row["key"]): str(row["value"])
        for _, row in frame.iterrows()
    }


def state_updated_map(
    frame: pd.DataFrame,
) -> dict[str, pd.Timestamp]:
    if frame.empty or "updated_at" not in frame.columns:
        return {}

    result: dict[str, pd.Timestamp] = {}
    for _, row in frame.iterrows():
        timestamp = pd.to_datetime(
            row["updated_at"],
            errors="coerce",
            utc=True,
        )
        if pd.notna(timestamp):
            result[str(row["key"])] = timestamp
    return result


def seconds_since(
    value: object,
) -> float:
    if value is None or value == "":
        return float("inf")
    try:
        timestamp = pd.to_datetime(
            value,
            errors="coerce",
            utc=True,
        )
        if pd.isna(timestamp):
            return float("inf")
        return max(
            0.0,
            (
                pd.Timestamp.now(tz="UTC")
                - timestamp
            ).total_seconds(),
        )
    except Exception:
        return float("inf")


def human_age(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "jamais"
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def read_log_tail(
    engine_key: str,
    line_count: int,
) -> str:
    safe_names = {
        "collector",
        "radar",
        "safety",
        "strategy",
        "recorder",
        "market",
    }
    if engine_key not in safe_names:
        return "Moteur inconnu."

    path = BASE_DIR / "logs" / f"{engine_key}.log"
    if not path.exists():
        return "Aucun log enregistré pour le moment."

    try:
        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        return "\n".join(lines[-max(1, line_count):])
    except Exception as error:
        return f"Lecture du log impossible : {error}"



def numeric_value(
    value: object,
    default: float = 0.0,
) -> float:
    try:
        if pd.isna(value):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def integer_value(
    value: object,
    default: int = 0,
) -> int:
    try:
        if pd.isna(value):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def timestamp_age_seconds(
    value: object,
) -> float:
    if value is None or pd.isna(value):
        return float("inf")
    try:
        timestamp = pd.to_datetime(
            value,
            errors="coerce",
            utc=True,
        )
        if pd.isna(timestamp):
            return float("inf")
        return max(
            0.0,
            (
                pd.Timestamp.now(tz="UTC")
                - timestamp
            ).total_seconds(),
        )
    except Exception:
        return float("inf")


def elapsed_seconds(
    start_value: object,
) -> float:
    return timestamp_age_seconds(start_value)


def check_item(
    passed: bool,
    criterion: str,
    current: str,
    target: str,
    *,
    hard_gate: bool = False,
) -> dict[str, object]:
    return {
        "Validé": bool(passed),
        "Critère": criterion,
        "Valeur actuelle": current,
        "Objectif": target,
        "Blocage critique": bool(hard_gate),
    }


def build_entry_ranking(
    launches: pd.DataFrame,
    safety: pd.DataFrame,
    qualifications: pd.DataFrame,
    config: dict[str, object],
) -> tuple[pd.DataFrame, dict[str, list[dict[str, object]]]]:
    """Rank every known token by the rules active for the current mode."""
    if launches.empty:
        return pd.DataFrame(), {}

    base = launches.copy()

    safety_columns = [
        "mint",
        "assessed_at",
        "safety_score",
        "decision",
        "hard_reject",
        "analysis_status",
        "holder_analysis_status",
        "provisional_score",
        "top1_pct",
        "top10_pct",
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "error_text",
    ]
    if not safety.empty:
        safety_view = safety[
            [
                column
                for column in safety_columns
                if column in safety.columns
            ]
        ].copy()
        safety_view = safety_view.rename(
            columns={
                "decision": "safety_decision",
                "analysis_status": "safety_analysis_status",
                "error_text": "safety_error_text",
            }
        )
        base = base.merge(
            safety_view,
            on="mint",
            how="left",
        )

    qualification_columns = [
        "mint",
        "state",
        "entry_mode",
        "qualification_score",
        "observation_samples",
        "stable_samples",
        "first_qualified_at",
        "ready_at",
        "current_progress_pct",
        "progress_delta_pct",
        "price_change_pct",
        "reason",
        "updated_at",
    ]
    if not qualifications.empty:
        qualification_view = qualifications[
            [
                column
                for column in qualification_columns
                if column in qualifications.columns
            ]
        ].copy()
        qualification_view = qualification_view.rename(
            columns={
                "state": "pipeline_state",
                "reason": "pipeline_reason",
                "updated_at": "candidate_updated_at",
            }
        )
        base = base.merge(
            qualification_view,
            on="mint",
            how="left",
        )

    defaults: dict[str, object] = {
        "assessed_at": pd.NaT,
        "safety_score": pd.NA,
        "safety_decision": "PENDING",
        "hard_reject": 0,
        "safety_analysis_status": "PENDING",
        "holder_analysis_status": "PENDING",
        "provisional_score": 1,
        "top1_pct": pd.NA,
        "top10_pct": pd.NA,
        "mint_authority_revoked": pd.NA,
        "freeze_authority_revoked": pd.NA,
        "safety_error_text": "",
        "pipeline_state": "PENDING",
        "entry_mode": "STRICT",
        "qualification_score": pd.NA,
        "observation_samples": 0,
        "stable_samples": 0,
        "first_qualified_at": pd.NaT,
        "ready_at": pd.NaT,
        "current_progress_pct": pd.NA,
        "progress_delta_pct": pd.NA,
        "price_change_pct": pd.NA,
        "pipeline_reason": "",
        "candidate_updated_at": pd.NaT,
        "is_mayhem_mode": pd.NA,
        "mayhem_conflict": 0,
        "mayhem_source": "",
    }
    for column, default in defaults.items():
        if column not in base.columns:
            base[column] = default

    text_defaults = {
        "symbol": "—",
        "name": "—",
        "market_mode": "BONDING",
        "lifecycle_state": "UNKNOWN",
        "pair_address": "",
        "pair_url": "",
        "rpc_status": "WAITING",
        "safety_decision": "PENDING",
        "safety_analysis_status": "PENDING",
        "holder_analysis_status": "PENDING",
        "safety_error_text": "",
        "pipeline_state": "PENDING",
        "entry_mode": "STRICT",
        "pipeline_reason": "",
        "mayhem_source": "",
    }
    for column, default in text_defaults.items():
        if column not in base.columns:
            base[column] = default
        else:
            base[column] = base[column].fillna(default)

    details_by_mint: dict[
        str,
        list[dict[str, object]],
    ] = {}
    summary_rows: list[dict[str, object]] = []

    acquisition_enabled = bool(
        config.get("acquisition_mode_enabled", False)
    )
    holder_limit = float(
        config.get(
            "acquisition_mode_require_top1_max_pct",
            config.get("safety_max_top1_pct", 3.5),
        )
    )
    safety_fresh_seconds = float(
        config.get(
            "qualification_max_safety_age_seconds",
            180,
        )
    )
    entry_delay = float(
        config.get(
            "acquisition_mode_entry_delay_seconds",
            3,
        )
    )

    for _, row in base.iterrows():
        mint = str(row.get("mint") or "")
        mode = (
            "MIGRATED_DEX"
            if str(row.get("market_mode") or "")
            == "MIGRATED_DEX"
            else "BONDING"
        )

        safety_score = numeric_value(
            row.get("safety_score"),
            0.0,
        )
        qualification_score = numeric_value(
            row.get("qualification_score"),
            0.0,
        )
        top1 = numeric_value(
            row.get("top1_pct"),
            -1.0,
        )
        hard_reject = bool(
            integer_value(row.get("hard_reject"), 0)
        )
        safety_decision = str(
            row.get("safety_decision") or "PENDING"
        )
        safety_analysis = str(
            row.get("safety_analysis_status")
            or "PENDING"
        )
        holder_analysis = str(
            row.get("holder_analysis_status")
            or "PENDING"
        )
        pipeline_state = str(
            row.get("pipeline_state") or "PENDING"
        )
        entry_mode = str(
            row.get("entry_mode")
            or (
                "ACQUISITION"
                if acquisition_enabled
                else "STRICT"
            )
        )
        safety_age = timestamp_age_seconds(
            row.get("assessed_at")
        )
        launch_age = timestamp_age_seconds(
            row.get("last_updated_at")
        )
        detected_age = timestamp_age_seconds(
            row.get("detected_at")
        )

        mayhem_value = row.get("is_mayhem_mode")
        mayhem_known = pd.notna(mayhem_value)
        is_mayhem = (
            bool(integer_value(mayhem_value, 0))
            if mayhem_known
            else False
        )
        mayhem_conflict = bool(
            integer_value(row.get("mayhem_conflict"), 0)
        )
        mayhem_label = (
            "CONFLIT — EXCLU"
            if mayhem_conflict
            else "OUI — EXCLU"
            if is_mayhem
            else "NON"
            if mayhem_known
            else "À VÉRIFIER"
        )

        pilot_path = (
            acquisition_enabled
            and pipeline_state != "ACQUISITION_READY"
            and entry_mode != "FULL_ACQUISITION"
        )
        top1_known = top1 >= 0
        mint_value = row.get("mint_authority_revoked")
        freeze_value = row.get("freeze_authority_revoked")
        mint_known = pd.notna(mint_value)
        freeze_known = pd.notna(freeze_value)
        mint_safe = (
            not mint_known
            or bool(integer_value(mint_value, 0))
        )
        freeze_safe = (
            not freeze_known
            or bool(integer_value(freeze_value, 0))
        )

        checks: list[dict[str, object]] = [
            check_item(
                mayhem_known and not is_mayhem,
                "Mayhem Mode désactivé",
                mayhem_label,
                "NON",
                hard_gate=True,
            ),
            check_item(
                not mayhem_conflict,
                "Aucun conflit Mayhem",
                (
                    "Concordance événement / courbe"
                    if not mayhem_conflict
                    else "Conflit détecté"
                ),
                "Aucun conflit",
                hard_gate=True,
            ),
            check_item(
                not hard_reject,
                "Aucun rejet critique connu",
                (
                    "Aucun hard reject"
                    if not hard_reject
                    else "Hard reject actif"
                ),
                "Aucun hard reject",
                hard_gate=True,
            ),
        ]

        if pilot_path:
            checks.extend(
                [
                    check_item(
                        not top1_known
                        or top1 <= holder_limit + 1e-9,
                        "Aucun dépassement holder connu",
                        (
                            f"{top1:.2f} %"
                            if top1_known
                            else "Pas encore mesuré"
                        ),
                        f"Inconnu ou ≤ {holder_limit:.2f} %",
                        hard_gate=True,
                    ),
                    check_item(
                        mint_safe,
                        "Mint authority non signalée active",
                        (
                            "Révoquée"
                            if mint_known and mint_safe
                            else "Non vérifiée"
                            if not mint_known
                            else "ACTIVE"
                        ),
                        "Révoquée ou non vérifiée",
                        hard_gate=True,
                    ),
                    check_item(
                        freeze_safe,
                        "Freeze authority non signalée active",
                        (
                            "Révoquée"
                            if freeze_known and freeze_safe
                            else "Non vérifiée"
                            if not freeze_known
                            else "ACTIVE"
                        ),
                        "Révoquée ou non vérifiée",
                        hard_gate=True,
                    ),
                    check_item(
                        detected_age
                        >= float(
                            config.get("paper_pilot_delay_seconds", 25)
                        ),
                        "Délai Paper Pilot",
                        human_age(detected_age),
                        (
                            "≥ "
                            f"{int(config.get('paper_pilot_delay_seconds', 25))} s"
                        ),
                    ),
                    check_item(
                        detected_age
                        <= float(
                            config.get(
                                "paper_pilot_max_event_age_seconds",
                                300,
                            )
                        ),
                        "CreateEvent encore exploitable",
                        human_age(detected_age),
                        (
                            "≤ "
                            f"{int(config.get('paper_pilot_max_event_age_seconds', 300))} s"
                        ),
                    ),
                ]
            )
        else:
            checks.extend(
                [
                    check_item(
                        safety_analysis == "COMPLETE",
                        "Analyse Safety",
                        safety_analysis,
                        "COMPLETE",
                        hard_gate=True,
                    ),
                    check_item(
                        holder_analysis == "COMPLETE",
                        "Analyse des holders",
                        holder_analysis,
                        "COMPLETE",
                        hard_gate=True,
                    ),
                    check_item(
                        top1_known
                        and top1 <= holder_limit + 1e-9,
                        "Plus gros holder hors pool",
                        (
                            f"{top1:.2f} %"
                            if top1_known
                            else "En attente"
                        ),
                        f"≤ {holder_limit:.2f} %",
                        hard_gate=True,
                    ),
                    check_item(
                        mint_known
                        and bool(integer_value(mint_value, 0)),
                        "Mint authority révoquée",
                        "OUI" if mint_known and mint_safe else "NON / en attente",
                        "OUI",
                        hard_gate=True,
                    ),
                    check_item(
                        freeze_known
                        and bool(integer_value(freeze_value, 0)),
                        "Freeze authority révoquée",
                        "OUI" if freeze_known and freeze_safe else "NON / en attente",
                        "OUI",
                        hard_gate=True,
                    ),
                    check_item(
                        safety_age <= safety_fresh_seconds,
                        "Fraîcheur de l’analyse Safety",
                        human_age(safety_age),
                        f"≤ {int(safety_fresh_seconds)} s",
                        hard_gate=True,
                    ),
                    check_item(
                        detected_age >= entry_delay,
                        "Délai minimal après détection",
                        human_age(detected_age),
                        f"≥ {entry_delay:g} s",
                    ),
                ]
            )

        if mode == "MIGRATED_DEX":
            market_age = timestamp_age_seconds(
                row.get("market_last_updated_at")
            )
            market_price = numeric_value(
                row.get("market_price_sol"),
                0.0,
            )
            checks.extend(
                [
                    check_item(
                        bool(row.get("pair_address")),
                        "Paire DEX disponible",
                        (
                            str(row.get("pair_address"))
                            if row.get("pair_address")
                            else "Aucune paire"
                        ),
                        "Paire active",
                        hard_gate=True,
                    ),
                    check_item(
                        market_price > 0,
                        "Prix DEX exploitable",
                        (
                            f"{market_price:.12f} SOL"
                            if market_price > 0
                            else "En attente"
                        ),
                        "> 0",
                        hard_gate=True,
                    ),
                    check_item(
                        market_age
                        <= float(
                            config.get(
                                "strategy_max_market_data_age_seconds",
                                20,
                            )
                        ),
                        "Données DEX fraîches",
                        human_age(market_age),
                        (
                            "≤ "
                            f"{int(config.get('strategy_max_market_data_age_seconds', 20))} s"
                        ),
                        hard_gate=True,
                    ),
                ]
            )
        else:
            curve_price = numeric_value(
                row.get("curve_price_sol"),
                0.0,
            )
            curve_active = (
                not bool(
                    integer_value(
                        row.get("complete"),
                        0,
                    )
                )
                and str(
                    row.get("lifecycle_state") or ""
                )
                not in {
                    "MIGRATED",
                    "CURVE_COMPLETE",
                    "GRADUATING",
                }
            )
            checks.extend(
                [
                    check_item(
                        curve_active,
                        "Bonding curve active",
                        str(
                            row.get("lifecycle_state")
                            or "UNKNOWN"
                        ),
                        "BONDING actif",
                        hard_gate=True,
                    ),
                    check_item(
                        curve_price > 0,
                        "Prix bonding exploitable",
                        (
                            f"{curve_price:.12f} SOL"
                            if curve_price > 0
                            else "En attente"
                        ),
                        "> 0",
                        hard_gate=True,
                    ),
                    check_item(
                        (
                            detected_age
                            <= float(
                                config.get(
                                    "paper_pilot_max_event_age_seconds",
                                    300,
                                )
                            )
                            if pilot_path
                            else launch_age
                            <= float(
                                config.get(
                                    "strategy_max_launch_data_age_seconds",
                                    35,
                                )
                            )
                        ),
                        (
                            "CreateEvent frais"
                            if pilot_path
                            else "Données bonding fraîches"
                        ),
                        human_age(
                            detected_age if pilot_path else launch_age
                        ),
                        (
                            "≤ "
                            + str(
                                int(
                                    config.get(
                                        "paper_pilot_max_event_age_seconds",
                                        300,
                                    )
                                    if pilot_path
                                    else config.get(
                                        "strategy_max_launch_data_age_seconds",
                                        35,
                                    )
                                )
                            )
                            + " s"
                        ),
                        hard_gate=True,
                    ),
                ]
            )

        if acquisition_enabled:
            checks.append(
                check_item(
                    pipeline_state in {
                        "PAPER_PILOT_READY",
                        "ACQUISITION_READY",
                        "PAPER_POSITION",
                    },
                    "Validation finale du pipeline",
                    pipeline_state,
                    "PAPER_PILOT_READY / ACQUISITION_READY",
                )
            )
        else:
            samples = integer_value(
                row.get("observation_samples"),
                0,
            )
            stable_samples = integer_value(
                row.get("stable_samples"),
                0,
            )
            stable_ratio = (
                stable_samples / samples
                if samples > 0
                else 0.0
            )
            price_change = numeric_value(
                row.get("price_change_pct"),
                0.0,
            )
            progress = numeric_value(
                row.get("current_progress_pct"),
                numeric_value(row.get("progress_pct"), -1.0),
            )
            progress_delta = numeric_value(
                row.get("progress_delta_pct"),
                0.0,
            )

            checks.extend(
                [
                    check_item(
                        safety_decision == "QUALIFIED",
                        "Décision Safety",
                        safety_decision,
                        "QUALIFIED",
                        hard_gate=True,
                    ),
                    check_item(
                        safety_score
                        >= float(
                            config.get(
                                "qualification_min_safety_score",
                                72,
                            )
                        ),
                        "Safety Score",
                        f"{safety_score:.1f} / 100",
                        (
                            "≥ "
                            f"{float(config.get('qualification_min_safety_score', 72)):.0f}"
                        ),
                    ),
                    check_item(
                        qualification_score
                        >= float(
                            config.get(
                                "qualification_min_score",
                                78,
                            )
                        ),
                        "Score de qualification",
                        f"{qualification_score:.1f} / 100",
                        (
                            "≥ "
                            f"{float(config.get('qualification_min_score', 78)):.0f}"
                        ),
                    ),
                    check_item(
                        samples
                        >= int(
                            config.get(
                                "qualification_min_samples",
                                8,
                            )
                        ),
                        "Nombre d’échantillons",
                        str(samples),
                        (
                            "≥ "
                            f"{int(config.get('qualification_min_samples', 8))}"
                        ),
                    ),
                    check_item(
                        stable_ratio
                        >= float(
                            config.get(
                                "qualification_min_stable_ratio",
                                0.8,
                            )
                        ),
                        "Stabilité des échantillons",
                        f"{stable_ratio * 100:.1f} %",
                        (
                            "≥ "
                            f"{float(config.get('qualification_min_stable_ratio', 0.8)) * 100:.0f} %"
                        ),
                    ),
                    check_item(
                        abs(price_change)
                        <= float(
                            config.get(
                                "qualification_max_price_change_pct",
                                20,
                            )
                        ),
                        "Variation du prix observée",
                        f"{price_change:+.2f} %",
                        (
                            "≤ ±"
                            f"{float(config.get('qualification_max_price_change_pct', 20)):.0f} %"
                        ),
                    ),
                ]
            )
            if mode == "BONDING":
                checks.extend(
                    [
                        check_item(
                            float(
                                config.get(
                                    "qualification_min_progress_pct",
                                    5,
                                )
                            )
                            <= progress
                            <= float(
                                config.get(
                                    "qualification_max_progress_pct",
                                    80,
                                )
                            ),
                            "Position dans la bonding curve",
                            (
                                f"{progress:.2f} %"
                                if progress >= 0
                                else "En attente"
                            ),
                            "5 % à 80 %",
                        ),
                        check_item(
                            progress_delta
                            >= float(
                                config.get(
                                    "qualification_min_progress_delta_pct",
                                    1,
                                )
                            ),
                            "Progression depuis le début",
                            f"{progress_delta:+.2f} point",
                            "≥ +1 point",
                        ),
                    ]
                )
            checks.append(
                check_item(
                    pipeline_state in {
                        "READY",
                        "PAPER_POSITION",
                    },
                    "Validation finale du pipeline",
                    pipeline_state,
                    "READY",
                )
            )

        passed_count = sum(
            1 for item in checks if bool(item["Validé"])
        )
        total_count = len(checks)
        missing_count = total_count - passed_count
        readiness_pct = (
            passed_count / total_count * 100.0
            if total_count
            else 0.0
        )
        hard_missing = [
            str(item["Critère"])
            for item in checks
            if bool(item["Blocage critique"])
            and not bool(item["Validé"])
        ]
        missing_labels = [
            str(item["Critère"])
            for item in checks
            if not bool(item["Validé"])
        ]

        if is_mayhem or mayhem_conflict:
            proximity = "IGNORÉ — MAYHEM"
            sort_group = 8
        elif not mayhem_known:
            proximity = "MAYHEM À VÉRIFIER"
            sort_group = 7
        elif pipeline_state == "PAPER_POSITION":
            proximity = "POSITION OUVERTE"
            sort_group = 0
        elif (
            pipeline_state
            in {"PAPER_PILOT_READY", "ACQUISITION_READY", "READY"}
            and missing_count == 0
        ):
            proximity = "PRÊT À ENTRER"
            sort_group = 1
        elif hard_reject:
            proximity = "REJETÉ"
            sort_group = 6
        elif pilot_path and not hard_missing:
            proximity = "PILOT EN ATTENTE"
            sort_group = 3
        elif safety_analysis != "COMPLETE":
            proximity = "SAFETY INCOMPLET"
            sort_group = 5
        elif missing_count <= 2:
            proximity = "TRÈS PROCHE"
            sort_group = 2
        elif readiness_pct >= 75:
            proximity = "PROCHE"
            sort_group = 3
        elif readiness_pct >= 50:
            proximity = "EN PROGRESSION"
            sort_group = 4
        else:
            proximity = "LOIN"
            sort_group = 5

        main_blocker = (
            hard_missing[0]
            if hard_missing
            else missing_labels[0]
            if missing_labels
            else "Aucun"
        )

        summary_rows.append(
            {
                "mint": mint,
                "Ticker": str(row.get("symbol") or "—"),
                "Nom": str(row.get("name") or "—"),
                "Marché": mode,
                "Mode d’entrée": entry_mode,
                "Mayhem": mayhem_label,
                "Source Mayhem": str(
                    row.get("mayhem_source") or "—"
                ),
                "Proximité": proximity,
                "Cases cochées": (
                    f"{passed_count} / {total_count}"
                ),
                "Cases validées": passed_count,
                "Cases totales": total_count,
                "Cases manquantes": missing_count,
                "Readiness %": readiness_pct,
                "Safety": safety_score,
                "Qualification": qualification_score,
                "Top holder %": (
                    top1 if top1 >= 0 else float("nan")
                ),
                "Pipeline": pipeline_state,
                "Blocage principal": main_blocker,
                "Critères manquants": " • ".join(
                    missing_labels[:5]
                ),
                "Raison pipeline": str(
                    row.get("pipeline_reason") or ""
                ),
                "Détection": row.get("detected_at"),
                "Paire DEX": row.get("pair_url"),
                "_sort_group": sort_group,
                "_hard_missing_count": len(hard_missing),
            }
        )
        details_by_mint[mint] = checks

    ranking = pd.DataFrame(summary_rows)
    if ranking.empty:
        return ranking, details_by_mint

    ranking = ranking.sort_values(
        [
            "_sort_group",
            "Readiness %",
            "_hard_missing_count",
            "Détection",
        ],
        ascending=[
            True,
            False,
            True,
            True,
        ],
        na_position="last",
    ).reset_index(drop=True)
    ranking.insert(
        0,
        "Rang",
        range(1, len(ranking) + 1),
    )
    return ranking, details_by_mint

def fmt_sol(value: float | int | None) -> str:
    number = float(value or 0)
    return f"{number:+.4f} SOL"


def maximum_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity - equity.cummax()).min())


watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

with st.sidebar:
    st.markdown(
        """
        <div class="brand">
            <div class="brand-mark">S</div>
            <div>
                <div class="brand-title">SOLPULSE</div>
                <div class="brand-subtitle">STABLE V12 • Paper Pilot opérationnel</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()
    page = st.radio(
        "Navigation",
        [
            "Cockpit V12.2",
            "Opportunités",
            "Positions",
            "Historique",
            "Radar hybride",
            "Safety Engine",
            "Diagnostic système",
            "Mode d’achat",
            "Analytics",
            "Replay & Backtest",
            "Watchlist",
        ],
    )
    st.divider()
    st.caption("SOURCES")
    st.success(
        "WebSocket : nouveaux launches\n\n"
        "Solana RPC : bonding + holders\n\n"
        "DEX Screener : PumpSwap et marchés migrés\n\n"
        "Recorder : replay hybride local"
    )
    st.caption("Rafraîchissement interface 2 s • moteurs auto-réparés")
    st.divider()
    st.caption("SÉCURITÉ")
    st.info(
        "Lecture on-chain uniquement\n\n"
        "Paper Pilot 0,01 SOL\n\n"
        "Acquisition complète 0,05 SOL\n\n"
        "Aucune clé privée"
    )


@st.fragment(run_every="2s")
def render_live(selected_page: str) -> None:
    (
        positions,
        portfolio,
        signals,
        orders,
        market,
        bonding,
        launches,
        safety,
        qualifications,
        qualification_events,
        bot_state,
    ) = load_data()

    parse_dates(positions, ["opened_at", "closed_at"])
    parse_dates(portfolio, ["timestamp"])
    parse_dates(signals, ["timestamp"])
    parse_dates(orders, ["timestamp"])
    parse_dates(market, ["timestamp"])
    parse_dates(bonding, ["timestamp"])
    parse_dates(
        launches,
        [
            "detected_at",
            "last_updated_at",
            "migrated_at",
            "market_last_updated_at",
        ],
    )
    parse_dates(safety, ["assessed_at"])
    parse_dates(
        qualifications,
        [
            "created_at",
            "first_qualified_at",
            "last_sample_at",
            "ready_at",
            "updated_at",
        ],
    )
    parse_dates(qualification_events, ["timestamp"])
    parse_dates(bot_state, ["updated_at"])

    if portfolio.empty:
        st.error("Base vide. Lance 02_REINITIALISER_1_SOL.bat.")
        return

    state = state_map(bot_state)
    state_updated = state_updated_map(bot_state)
    status = state.get("status", "STOPPED")
    rpc_status = state.get("rpc_status", "WAITING")
    dex_status = state.get("dex_status", "WAITING")
    error_text = state.get("last_error", "")
    sol_price_usd = float(state.get("sol_price_usd") or 0)

    latest_market = latest_per_mint(market)
    latest_bonding = latest_per_mint(bonding)

    combined = latest_bonding.copy()
    if not latest_market.empty:
        market_columns = [
            "token_mint", "price_sol", "price_usd",
            "change_1m_pct", "change_5m_pct", "change_h1_pct",
            "liquidity_usd", "volume_5m_usd",
            "buys_5m", "sells_5m", "market_cap_usd",
            "score", "data_status", "source_url",
            "pair_address", "dex_id",
        ]
        combined = combined.merge(
            latest_market[market_columns],
            on="token_mint",
            how="outer",
            suffixes=("_curve", "_dex"),
        )

    latest_portfolio = portfolio.iloc[-1]
    closed = positions[positions["status"] == "CLOSED"].copy()
    opened = positions[positions["status"] == "OPEN"].copy()

    equity = float(latest_portfolio["equity_sol"])
    cash = float(latest_portfolio["cash_sol"])
    realized = float(latest_portfolio["realized_pnl_sol"])
    unrealized = float(latest_portfolio["unrealized_pnl_sol"])
    initial_capital = float(portfolio.iloc[0]["equity_sol"])
    total_return = (
        (equity / initial_capital - 1) * 100
        if initial_capital
        else 0
    )

    wins = closed[closed["realized_pnl_sol"] > 0]
    losses = closed[closed["realized_pnl_sol"] < 0]
    win_rate = len(wins) / len(closed) * 100 if len(closed) else 0
    max_dd = maximum_drawdown(portfolio["equity_sol"].astype(float))

    collector_tick_age = seconds_since(
        state.get("last_tick")
    )
    last_tick = (
        f"il y a {human_age(collector_tick_age)}"
        if math.isfinite(collector_tick_age)
        else "jamais"
    )
    collector_stale = (
        collector_tick_age
        > float(
            config.get(
                "engine_heartbeat_stale_seconds",
                35,
            )
        )
    )

    if status == "RUNNING" and not collector_stale:
        status_class, dot_class, status_label = (
            "status-online", "dot-online", "SCANNER ON-CHAIN EN LIGNE"
        )
    elif status == "DATA_ERROR" and not collector_stale:
        status_class, dot_class, status_label = (
            "status-warn", "dot-warn", "SOURCE PARTIELLEMENT INDISPONIBLE"
        )
    elif collector_stale:
        status_class, dot_class, status_label = (
            "status-offline", "dot-offline", "COLLECTEUR PÉRIMÉ OU ARRÊTÉ"
        )
    else:
        status_class, dot_class, status_label = (
            "status-offline", "dot-offline", "COLLECTEUR ARRÊTÉ"
        )

    st.markdown(
        f"""
        <div class="topbar">
            <div>
                <div class="kicker">Paper Pilot • Pump CreateEvent • Solana</div>
                <div class="page-title">{selected_page}</div>
                <div class="subtitle">
                    Détection immédiate, achats paper automatiques et diagnostic lisible.
                </div>
            </div>
            <div>
                <div class="{status_class}">
                    <span class="{dot_class}"></span>{status_label}
                </div>
                <div class="live-note">
                    RPC {rpc_status} • DEX {dex_status} • tick {last_tick}
                    • superviseur {state.get("supervisor_status", "STOPPED")}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if error_text:
        st.warning(f"Dernière erreur : {error_text}")

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Équité", f"{equity:.4f} SOL", f"{total_return:+.2f} %")
    m2.metric("Cash", f"{cash:.4f} SOL", f"{len(opened)} position(s)")
    m3.metric("PnL réalisé", fmt_sol(realized), "Paper")
    m4.metric("PnL latent", fmt_sol(unrealized), "Paper")
    m5.metric("Win rate", f"{win_rate:.1f} %", f"{len(wins)}W / {len(losses)}L")
    m6.metric("SOL/USD", f"${sol_price_usd:,.2f}" if sol_price_usd else "—")

    pilot_entries = int(state.get("paper_pilot_entries_count") or 0)
    full_entries = int(state.get("acquisition_mode_entries_count") or 0)
    self_test = state.get("startup_self_test", "PENDING")
    banner = (
        "PAPER PILOT V12 ACTIF — après 25 secondes, un coin non-Mayhem "
        "avec un prix CreateEvent exploitable peut recevoir un achat test "
        "de 0,01 SOL, même si le RPC Safety est encore lent. Les violations "
        "critiques connues ferment ou bloquent la position. "
        f"Pilot: {pilot_entries} • complet: {full_entries} • self-test: {self_test}."
    )
    if self_test == "PASS":
        st.success(banner)
    else:
        st.warning(banner)

    if selected_page == "Cockpit V12.2":
        engine_specs = [
            ("radar", "Radar", "Nouveaux coins"),
            ("safety", "Safety", "Contrôles on-chain"),
            ("strategy", "Paper Pilot", "Achats paper"),
            ("market", "Marché DEX", "Coins migrés"),
            ("recorder", "Recorder", "Historique"),
            ("collector", "Watchlist", "Suivi manuel"),
        ]
        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'État du système</div><div class="section-note">'
            'Vert = moteur supervisé et actif</div></div>',
            unsafe_allow_html=True,
        )
        engine_columns = st.columns(6)
        for column, (key, label, role) in zip(
            engine_columns,
            engine_specs,
        ):
            engine_status = state.get(
                f"supervisor_{key}_status",
                "STOPPED",
            )
            restarts = int(
                state.get(f"supervisor_{key}_restarts") or 0
            )
            message = state.get(
                f"supervisor_{key}_message",
                "",
            )
            with column:
                st.metric(
                    label,
                    engine_status,
                    f"{restarts} redémarrage(s)",
                )
                st.caption(message or role)

        detected_count = len(launches)
        non_mayhem_count = int(
            (
                pd.to_numeric(
                    launches.get("is_mayhem_mode", pd.Series(dtype=float)),
                    errors="coerce",
                )
                == 0
            ).sum()
        ) if not launches.empty else 0
        mayhem_count = int(
            (
                pd.to_numeric(
                    launches.get("is_mayhem_mode", pd.Series(dtype=float)),
                    errors="coerce",
                )
                == 1
            ).sum()
        ) if not launches.empty else 0
        pilot_ready = int(
            (qualifications.get("state", pd.Series(dtype=str)) == "PAPER_PILOT_READY").sum()
        ) if not qualifications.empty else 0
        full_ready = int(
            (qualifications.get("state", pd.Series(dtype=str)) == "ACQUISITION_READY").sum()
        ) if not qualifications.empty else 0

        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Chaîne d’achat paper</div><div class="section-note">'
            'Objectif actuel : produire des trades mesurables</div></div>',
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Coins détectés", detected_count)
        c2.metric("Non-Mayhem", non_mayhem_count)
        c3.metric("Mayhem exclus", mayhem_count)
        c4.metric("Pilot prêts", pilot_ready)
        c5.metric("Acquisition complète", full_ready)
        c6.metric("Positions ouvertes", len(opened))

        if not opened.empty:
            current = opened.iloc[0]
            strategy_label = (
                "PAPER PILOT"
                if str(current.get("strategy")) == "paper_pilot_v12"
                else "ACQUISITION COMPLÈTE"
            )
            st.success(
                f"POSITION ACTIVE — {current.get('symbol', '—')} • "
                f"{strategy_label} • entrée {float(current.get('entry_sol') or 0):.4f} SOL • "
                f"valeur {float(current.get('current_value_sol') or 0):.4f} SOL."
            )
        elif pilot_ready or full_ready:
            st.info(
                "Un candidat est prêt. Le Paper Pilot Engine tentera "
                "l’achat au prochain cycle de deux secondes."
            )
        elif launches.empty:
            st.info(
                "Le radar attend le prochain CreateEvent Pump. "
                "Aucune anomalie de stratégie n’est visible."
            )
        else:
            latest_reason = ""
            if not qualifications.empty:
                latest_reason = str(
                    qualifications.iloc[0].get("reason") or ""
                )
            st.warning(
                "Aucun achat prêt pour l’instant. Blocage le plus récent : "
                + (latest_reason or "attente du délai Pilot ou d’un prix exploitable")
            )

        st.markdown("### File d’opportunités")
        if launches.empty:
            st.caption("Aucun coin détecté.")
        else:
            cockpit = launches.copy()
            if not qualifications.empty:
                cockpit = cockpit.merge(
                    qualifications[
                        [
                            column
                            for column in [
                                "mint", "state", "entry_mode",
                                "reason", "updated_at",
                            ]
                            if column in qualifications.columns
                        ]
                    ],
                    on="mint",
                    how="left",
                )
            if not safety.empty:
                cockpit = cockpit.merge(
                    safety[
                        [
                            column
                            for column in [
                                "mint", "analysis_status",
                                "holder_analysis_status", "top1_pct",
                                "hard_reject",
                            ]
                            if column in safety.columns
                        ]
                    ],
                    on="mint",
                    how="left",
                    suffixes=("", "_safety"),
                )
            cockpit["Mayhem"] = cockpit["is_mayhem_mode"].apply(
                lambda value: (
                    "EXCLU" if pd.notna(value) and int(value) == 1
                    else "NON" if pd.notna(value)
                    else "?"
                )
            )
            cockpit["Courbe"] = cockpit.get(
                "curve_confirmed",
                pd.Series(0, index=cockpit.index),
            ).apply(lambda value: "CONFIRMÉE" if int(value or 0) else "EVENT")
            display = [
                column
                for column in [
                    "symbol", "name", "Mayhem", "Courbe",
                    "state", "entry_mode", "analysis_status",
                    "holder_analysis_status", "top1_pct", "reason",
                ]
                if column in cockpit.columns
            ]
            st.dataframe(
                cockpit[display].head(20),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "symbol": "Token",
                    "name": "Nom",
                    "state": "Pipeline",
                    "entry_mode": "Mode",
                    "analysis_status": "Safety",
                    "holder_analysis_status": "Holders",
                    "top1_pct": st.column_config.NumberColumn(
                        "Top holder", format="%.2f %%"
                    ),
                    "reason": "Action / blocage",
                },
            )

        st.markdown("### Dernières exécutions paper")
        if orders.empty:
            st.caption("Aucun ordre paper enregistré.")
        else:
            st.dataframe(
                orders[
                    [
                        column
                        for column in [
                            "timestamp", "symbol", "side",
                            "requested_sol", "status",
                            "failure_reason",
                        ]
                        if column in orders.columns
                    ]
                ].head(12),
                use_container_width=True,
                hide_index=True,
            )

    elif selected_page == "Aperçu":
        left, right = st.columns([1.9, 1])
        with left:
            chart = portfolio[["timestamp", "equity_sol"]].dropna().tail(1200)
            fig = go.Figure(
                go.Scatter(
                    x=chart["timestamp"],
                    y=chart["equity_sol"],
                    mode="lines",
                    line=dict(color="#25e6a5", width=3),
                    fill="tozeroy",
                    fillcolor="rgba(37,230,165,.06)",
                    hovertemplate="%{y:.4f} SOL<extra></extra>",
                )
            )
            fig.add_hline(
                y=initial_capital,
                line_dash="dot",
                line_color="rgba(255,255,255,.25)",
                annotation_text="Capital initial",
            )
            fig.update_layout(
                **PLOTLY_LAYOUT,
                height=365,
                title="Courbe d’équité paper",
                showlegend=False,
                xaxis=dict(showgrid=False),
                yaxis=dict(
                    gridcolor="rgba(255,255,255,.055)",
                    ticksuffix=" SOL",
                ),
            )
            st.plotly_chart(
                fig,
                use_container_width=True,
                config={"displayModeBar": False},
            )

        with right:
            lifecycle_counts = (
                latest_bonding["lifecycle_state"].value_counts()
                if not latest_bonding.empty
                else pd.Series(dtype=int)
            )
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("#### Cycle Pump")
            for lifecycle in [
                "BONDING",
                "GRADUATING",
                "BONDED",
                "DEX_ONLY",
                "UNKNOWN",
            ]:
                st.write(
                    f"**{lifecycle} :** "
                    f"{int(lifecycle_counts.get(lifecycle, 0))}"
                )
            st.write(f"**Drawdown paper :** {max_dd:.4f} SOL")
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown(
            '<div class="section-head"><div class="section-title">État des cinq contrats</div>'
            '<div class="section-note">Source on-chain prioritaire</div></div>',
            unsafe_allow_html=True,
        )
        if combined.empty:
            st.info("En attente du premier appel RPC.")
        else:
            st.dataframe(
                combined[
                    [
                        "symbol", "lifecycle_state",
                        "progress_pct", "curve_price_usd",
                        "real_quote_reserves_sol",
                        "complete", "rpc_status",
                        "data_status",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "symbol": "Token",
                    "lifecycle_state": "État",
                    "progress_pct": st.column_config.ProgressColumn(
                        "Progression",
                        min_value=0,
                        max_value=100,
                        format="%.1f %%",
                    ),
                    "curve_price_usd": st.column_config.NumberColumn(
                        "Prix courbe",
                        format="$ %.10f",
                    ),
                    "real_quote_reserves_sol": st.column_config.NumberColumn(
                        "SOL courbe",
                        format="%.3f SOL",
                    ),
                    "complete": "Complete",
                    "rpc_status": "RPC",
                    "data_status": "DEX",
                },
            )




    elif selected_page == "Diagnostic système":
        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Santé des moteurs et raisons de blocage</div>'
            '<div class="section-note">'
            'Supervision automatique, données périmées et logs persistants'
            '</div></div>',
            unsafe_allow_html=True,
        )

        self_test_status = state.get("startup_self_test", "PENDING")
        self_test_details = state.get("startup_self_test_details", "")
        if self_test_status == "PASS":
            st.success(
                "Self-test de démarrage : PASS — syntaxe, SQLite et achat "
                "paper synthétique validés."
            )
        elif self_test_status == "FAIL":
            st.error(
                "Self-test de démarrage : FAIL — relance "
                "06_TESTER_SOLPULSE_V12_1.bat avant d'utiliser le bot."
            )
        else:
            st.warning(
                "Self-test non encore exécuté. Le lanceur V12 l'exécute "
                "automatiquement avant les moteurs."
            )
        if self_test_details:
            st.caption(self_test_details)

        stale_limit = float(
            config.get(
                "engine_heartbeat_stale_seconds",
                35,
            )
        )
        supervisor_age = seconds_since(
            state.get("supervisor_last_tick")
        )
        supervisor_ok = (
            state.get("supervisor_status") == "RUNNING"
            and supervisor_age
            <= float(
                config.get(
                    "supervisor_heartbeat_stale_seconds",
                    20,
                )
            )
        )

        if supervisor_ok:
            st.success(
                "Superviseur actif : les moteurs arrêtés sont "
                "redémarrés automatiquement."
            )
        else:
            st.error(
                "Superviseur absent ou périmé. Les moteurs ne sont "
                "peut-être plus protégés par le redémarrage automatique."
            )

        engine_definitions = [
            {
                "key": "collector",
                "label": "Watchlist Collector",
                "native": state.get("status", "STOPPED"),
                "heartbeat": state.get("last_tick"),
            },
            {
                "key": "radar",
                "label": "Radar hybride",
                "native": state.get("radar_status", "STOPPED"),
                "heartbeat": state_updated.get("radar_status"),
            },
            {
                "key": "market",
                "label": "Hybrid Market Scanner",
                "native": state.get(
                    "hybrid_market_status",
                    "STOPPED",
                ),
                "heartbeat": state.get(
                    "hybrid_market_last_scan"
                ),
            },
            {
                "key": "safety",
                "label": "Safety Recovery Engine",
                "native": state.get("safety_status", "STOPPED"),
                "heartbeat": state.get("safety_last_scan"),
            },
            {
                "key": "strategy",
                "label": "Paper Pilot Engine",
                "native": state.get(
                    "qualification_status",
                    "STOPPED",
                ),
                "heartbeat": state.get(
                    "qualification_last_cycle"
                ),
            },
            {
                "key": "recorder",
                "label": "Event Recorder",
                "native": state.get(
                    "recorder_status",
                    "STOPPED",
                ),
                "heartbeat": state.get(
                    "recorder_last_sample"
                ),
            },
        ]

        engine_rows: list[dict[str, object]] = []
        active_count = 0
        for definition in engine_definitions:
            key = str(definition["key"])
            heartbeat_age = seconds_since(
                definition["heartbeat"]
            )
            process_status = state.get(
                f"supervisor_{key}_status",
                "UNKNOWN",
            )
            native_status = str(definition["native"])

            if (
                process_status == "RUNNING"
                and heartbeat_age <= stale_limit
                and native_status
                in {
                    "RUNNING",
                    "DATA_ERROR",
                    "RECONNECTING",
                }
            ):
                health = (
                    "ACTIF"
                    if native_status == "RUNNING"
                    else "DÉGRADÉ"
                )
                active_count += 1
            elif process_status in {
                "RESTARTING",
                "COOLDOWN",
            }:
                health = process_status
            elif process_status == "RUNNING":
                health = "PÉRIMÉ"
            else:
                health = "ARRÊTÉ"

            engine_rows.append(
                {
                    "Moteur": definition["label"],
                    "Santé": health,
                    "Processus": process_status,
                    "État interne": native_status,
                    "Dernier heartbeat": human_age(
                        heartbeat_age
                    ),
                    "PID": state.get(
                        f"supervisor_{key}_pid",
                        "",
                    ),
                    "Démarrages": int(
                        state.get(
                            f"supervisor_{key}_restarts",
                            "0",
                        )
                        or 0
                    ),
                    "Dernier message": state.get(
                        f"supervisor_{key}_message",
                        "",
                    ),
                }
            )

        s1, s2, s3, s4 = st.columns(4)
        s1.metric(
            "Moteurs actifs",
            f"{active_count} / {len(engine_rows)}",
        )
        s2.metric(
            "Superviseur",
            "ACTIF" if supervisor_ok else "INACTIF",
            human_age(supervisor_age),
        )
        s3.metric(
            "SQLite",
            state.get("db_integrity", "UNKNOWN"),
        )
        st.caption(
            "Fast Safety : "
            f"{state.get('safety_queue_pending', '0')} coin(s) en file, "
            f"plus ancien depuis "
            f"{human_age(float(state.get('safety_oldest_pending_age_seconds') or 0))}, "
            f"{state.get('safety_starved_count', '0')} au-delà de 45 s."
        )

        s4.metric(
            "Dernière sauvegarde",
            (
                human_age(
                    seconds_since(
                        state.get("db_last_backup")
                    )
                )
                if state.get("db_last_backup")
                else "aucune"
            ),
        )

        st.dataframe(
            pd.DataFrame(engine_rows),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("### Pourquoi aucun trade n’est pris")

        blocker_counts: dict[str, int] = {}

        qualification_labels = {
            "observation": "Délai d’observation incomplet",
            "samples": "Pas assez d’échantillons",
            "stability": "Stabilité inférieure au seuil",
            "curve": "Bonding curve invalide",
            "active": "Bonding curve terminée",
            "progress": "Bonding hors de la plage 5–80 %",
            "delta": "Progression inférieure à +1 point",
            "price": "Variation du prix supérieure à ±20 %",
            "safety": "Safety Score inférieur à 72",
            "safety_floor": "Safety descendu sous 72",
            "safety_fresh": "Analyse Safety trop ancienne",
            "qualification": "Qualification inférieure à 78",
            "fresh_market": "Données DEX trop anciennes",
            "pair_age_min": "Paire DEX trop récente",
            "pair_age_max": "Paire DEX trop ancienne",
            "liquidity": "Liquidité DEX insuffisante",
            "volume": "Volume DEX 5 min insuffisant",
            "buys": "Pas assez d’achats DEX",
            "sells": "Pas assez de ventes DEX",
            "ratio": "Ratio achats/ventes trop élevé",
        }

        if not qualifications.empty:
            for _, candidate in qualifications.iterrows():
                reason = str(candidate.get("reason") or "")
                state_value = str(candidate.get("state") or "")

                if "données périmées" in reason.lower():
                    blocker_counts[
                        "Données de marché ou de sécurité périmées"
                    ] = blocker_counts.get(
                        "Données de marché ou de sécurité périmées",
                        0,
                    ) + 1

                reason_lower = reason.lower()
                if "contrôles restants :" in reason_lower:
                    raw_checks = reason_lower.split(
                        "contrôles restants :",
                        1,
                    )[1]
                    for raw_key in raw_checks.split(","):
                        key = raw_key.strip()
                        label = qualification_labels.get(
                            key,
                            f"Contrôle {key}",
                        )
                        blocker_counts[label] = (
                            blocker_counts.get(label, 0) + 1
                        )

                if state_value == "PAUSED" and not reason:
                    blocker_counts[
                        "Candidat en pause sans explication"
                    ] = blocker_counts.get(
                        "Candidat en pause sans explication",
                        0,
                    ) + 1

        safety_blockers: dict[str, int] = {}
        if not safety.empty:
            for _, assessment in safety.iterrows():
                if assessment.get("decision") != "REJECTED":
                    continue
                try:
                    warnings = json.loads(
                        assessment.get("warnings_json") or "[]"
                    )
                except Exception:
                    warnings = [
                        str(
                            assessment.get("warnings_json")
                            or "Rejet Safety non classé"
                        )
                    ]

                for warning in warnings:
                    warning_text = str(warning)
                    lower = warning_text.lower()
                    if "mayhem" in lower:
                        label = "Mayhem Mode — exclusion absolue"
                    elif "supply totale" in lower or (
                        "propriétaire contrôle" in lower
                    ):
                        label = "Holder hors pool supérieur à 3,5 %"
                    elif "mint authority" in lower:
                        label = "Mint authority encore active"
                    elif "freeze authority" in lower:
                        label = "Freeze authority encore active"
                    elif "top 10" in lower:
                        label = "Concentration Top 10"
                    elif "compte mint introuvable" in lower:
                        label = "Compte mint introuvable"
                    elif "analyse rpc" in lower:
                        label = "Analyse RPC indisponible"
                    else:
                        label = warning_text[:90]

                    safety_blockers[label] = (
                        safety_blockers.get(label, 0) + 1
                    )

        combined_blockers = dict(blocker_counts)
        for label, count in safety_blockers.items():
            combined_blockers[label] = (
                combined_blockers.get(label, 0) + count
            )

        if combined_blockers:
            blocker_frame = pd.DataFrame(
                [
                    {
                        "Raison": label,
                        "Tokens concernés": count,
                    }
                    for label, count in combined_blockers.items()
                ]
            ).sort_values(
                "Tokens concernés",
                ascending=False,
            )
            st.dataframe(
                blocker_frame,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(
                "Pas encore assez de candidats pour établir "
                "les raisons de blocage."
            )

        st.markdown("### Diagnostic individuel")

        diagnostic_tokens = pd.DataFrame()
        if not qualifications.empty:
            diagnostic_tokens = qualifications.copy()
            if not safety.empty:
                safety_columns = [
                    "mint",
                    "assessed_at",
                    "safety_score",
                    "decision",
                    "hard_reject",
                    "top1_pct",
                    "top10_pct",
                    "analysis_status",
                    "warnings_json",
                ]
                diagnostic_tokens = diagnostic_tokens.merge(
                    safety[safety_columns],
                    on="mint",
                    how="left",
                    suffixes=("_candidate", "_safety"),
                )

        if diagnostic_tokens.empty:
            st.info("Aucun candidat à diagnostiquer.")
        else:
            selected_mint = st.selectbox(
                "Token",
                diagnostic_tokens["mint"].tolist(),
                format_func=lambda mint: (
                    f"{diagnostic_tokens.loc[
                        diagnostic_tokens['mint'] == mint,
                        'symbol'
                    ].iloc[0]} — {mint[:8]}…{mint[-6:]}"
                ),
                key="diagnostics_token_selector",
            )
            token = diagnostic_tokens[
                diagnostic_tokens["mint"] == selected_mint
            ].iloc[0]

            safety_value = float(
                token.get("safety_score_safety")
                if pd.notna(
                    token.get("safety_score_safety")
                )
                else token.get("safety_score_candidate")
                or 0
            )
            qualification_value = float(
                token.get("qualification_score") or 0
            )
            sample_value = int(
                token.get("observation_samples") or 0
            )
            stable_value = int(
                token.get("stable_samples") or 0
            )
            stable_ratio = (
                stable_value / sample_value
                if sample_value
                else 0.0
            )
            progress_value = float(
                token.get("current_progress_pct") or 0
            )
            delta_value = float(
                token.get("progress_delta_pct") or 0
            )
            price_change_value = float(
                token.get("price_change_pct") or 0
            )
            top1_value = float(
                token.get("top1_pct") or 0
            )
            safety_age = seconds_since(
                token.get("assessed_at")
            )

            checks = [
                (
                    top1_value <= 3.5,
                    "Plus gros holder hors pool",
                    f"{top1_value:.2f} % / maximum 3,50 %",
                ),
                (
                    safety_value >= 72,
                    "Safety Score",
                    f"{safety_value:.1f} / minimum 72",
                ),
                (
                    qualification_value >= 78,
                    "Qualification",
                    f"{qualification_value:.1f} / minimum 78",
                ),
                (
                    sample_value >= 8,
                    "Échantillons",
                    f"{sample_value} / minimum 8",
                ),
                (
                    stable_ratio >= 0.80,
                    "Stabilité",
                    f"{stable_ratio * 100:.1f} % / minimum 80 %",
                ),
                (
                    5 <= progress_value <= 80,
                    "Progression de courbe",
                    f"{progress_value:.2f} % / plage 5–80 %",
                ),
                (
                    delta_value >= 1.0,
                    "Progression depuis observation",
                    f"{delta_value:+.2f} point / minimum +1",
                ),
                (
                    abs(price_change_value) <= 20,
                    "Variation de prix",
                    f"{price_change_value:+.2f} % / maximum ±20 %",
                ),
                (
                    safety_age <= 180,
                    "Fraîcheur Safety",
                    f"{human_age(safety_age)} / maximum 3 min",
                ),
            ]

            check_frame = pd.DataFrame(
                [
                    {
                        "Validé": "✓" if passed else "✗",
                        "Critère": label,
                        "Valeur": detail,
                    }
                    for passed, label, detail in checks
                ]
            )
            st.dataframe(
                check_frame,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"État : {token.get('state')} — "
                f"{token.get('reason') or 'aucune raison enregistrée'}"
            )

        st.markdown("### Incidents et redémarrages")
        incidents = query(
            """
            SELECT *
            FROM engine_incidents
            ORDER BY id DESC
            LIMIT 100
            """
        )
        parse_dates(incidents, ["timestamp"])
        if incidents.empty:
            st.success(
                "Aucun incident enregistré depuis le démarrage."
            )
        else:
            st.dataframe(
                incidents[
                    [
                        "timestamp",
                        "engine_label",
                        "event_type",
                        "exit_code",
                        "restart_count",
                        "message",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "timestamp": st.column_config.DatetimeColumn(
                        "Heure",
                        format="DD/MM HH:mm:ss",
                    ),
                    "engine_label": "Moteur",
                    "event_type": "Événement",
                    "exit_code": "Code",
                    "restart_count": "Démarrages",
                    "message": st.column_config.TextColumn(
                        "Message",
                        width="large",
                    ),
                },
            )

        st.markdown("### Dernières lignes des logs")
        log_engine = st.selectbox(
            "Moteur à inspecter",
            [
                "collector",
                "radar",
                "safety",
                "strategy",
                "recorder",
                "market",
            ],
            key="diagnostics_log_selector",
        )
        st.code(
            read_log_tail(
                log_engine,
                int(
                    config.get(
                        "diagnostics_recent_log_lines",
                        80,
                    )
                ),
            ),
            language="text",
        )

        st.caption(
            "Les logs sont conservés dans le dossier logs. "
            "La base est sauvegardée une fois par jour dans backups."
        )

    elif selected_page == "Mode d’achat":
        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Paper Pilot + acquisition complète</div>'
            '<div class="section-note">'
            'Deux voies, une seule position paper à la fois'
            '</div></div>',
            unsafe_allow_html=True,
        )

        st.info(
            "Le Paper Pilot sert à produire des trades rapidement pour "
            "tester toute la chaîne. Il ne prétend pas sélectionner des "
            "tokens rentables. L’acquisition complète reste la voie plus "
            "prudente lorsque les données Safety sont disponibles."
        )
        st.error(
            "Mayhem reste toujours interdit. Un conflit Mayhem, un hard "
            "reject, un holder connu au-dessus de 3,5 % ou une authority "
            "active connue bloque ou ferme une position Pilot."
        )

        pilot_col, full_col = st.columns(2)
        with pilot_col:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("### Paper Pilot")
            st.write("**Taille :** 0,01 SOL paper")
            st.write("**Déclenchement :** environ 25 secondes")
            st.write("**Prix :** réserves du CreateEvent, puis courbe RPC")
            st.write("**Safety complet :** non requis avant l’entrée")
            st.write("**Sortie maximale :** 120 secondes")
            st.write("**Contrôles après entrée :** continus")
            st.markdown("</div>", unsafe_allow_html=True)

        with full_col:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("### Acquisition complète")
            st.write("**Taille :** 0,05 SOL paper")
            st.write("**Safety :** COMPLETE")
            st.write("**Holders :** COMPLETE")
            st.write("**Top holder hors pool :** ≤ 3,5 %")
            st.write("**Mint/freeze authorities :** révoquées")
            st.write("**Durée maximale :** 5 minutes")
            st.markdown("</div>", unsafe_allow_html=True)

        bypass_col, risk_col = st.columns(2)
        with bypass_col:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("### Ignoré pendant la phase de validation")
            st.write("Safety Score et qualification minimums")
            st.write("Progression, momentum et stabilité")
            st.write("Nombre d’échantillons")
            st.write("Volume, liquidité et rentabilité attendue")
            st.markdown("</div>", unsafe_allow_html=True)

        with risk_col:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            st.markdown("### Gestion du risque paper")
            st.write("**Positions simultanées :** 1")
            st.write("**Stop initial :** −20 %")
            st.write("**Take-profit :** +100 %")
            st.write("**Break-even :** armé à +50 %, stop +1 %")
            st.write("**Transactions réelles :** désactivées")
            st.markdown("</div>", unsafe_allow_html=True)

        if opened.empty:
            st.caption("Aucune position paper ouverte actuellement.")
        else:
            st.markdown("### Position actuelle")
            st.dataframe(
                opened[
                    [
                        column
                        for column in [
                            "symbol", "strategy", "market_mode",
                            "opened_at", "entry_sol",
                            "current_value_sol", "peak_pnl_pct",
                            "break_even_armed", "active_stop_pct",
                        ]
                        if column in opened.columns
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

    elif selected_page in {"Classement d’entrée", "Opportunités"}:
        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Classement de proximité d’entrée</div>'
            '<div class="section-note">'
            'Tous les coins classés selon le nombre de critères validés'
            '</div></div>',
            unsafe_allow_html=True,
        )

        ranking, ranking_details = build_entry_ranking(
            launches,
            safety,
            qualifications,
            config,
        )

        st.info(
            "Le classement compte chaque case de la stratégie. "
            "V12 réserve des places immédiates aux nouveaux coins et protège aussi les plus anciens : "
            "un coin ne doit plus rester bloqué indéfiniment derrière les nouveaux. "
            "Comme le parcours DEX comporte davantage de contrôles que "
            "le parcours bonding, l’ordre principal utilise aussi le "
            "pourcentage de cases validées. Un rejet critique reste "
            "toujours classé derrière les candidats encore admissibles. "
            "Un coin Mayhem est classé IGNORÉ — MAYHEM et ne peut jamais "
            "devenir READY."
        )

        if ranking.empty:
            st.warning(
                "Aucun coin n’est encore disponible dans le radar."
            )
        else:
            active_candidates = ranking[
                ~ranking["Proximité"].isin(
                    [
                        "POSITION OUVERTE",
                        "REJETÉ",
                        "IGNORÉ — MAYHEM",
                        "MAYHEM À VÉRIFIER",
                    ]
                )
            ]
            nearest = (
                active_candidates.iloc[0]
                if not active_candidates.empty
                else ranking.iloc[0]
            )

            ready_count = int(
                ranking["Proximité"].isin(
                    ["PRÊT À ENTRER", "POSITION OUVERTE"]
                ).sum()
            )
            very_close_count = int(
                (ranking["Proximité"] == "TRÈS PROCHE").sum()
            )
            close_count = int(
                (ranking["Proximité"] == "PROCHE").sum()
            )
            incomplete_count = int(
                (
                    ranking["Proximité"]
                    == "SAFETY INCOMPLET"
                ).sum()
            )
            rejected_count = int(
                (ranking["Proximité"] == "REJETÉ").sum()
            )
            mayhem_count = int(
                (ranking["Proximité"] == "IGNORÉ — MAYHEM").sum()
            )
            mayhem_unknown_count = int(
                (
                    ranking["Proximité"]
                    == "MAYHEM À VÉRIFIER"
                ).sum()
            )

            m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)
            m1.metric("Coins classés", len(ranking))
            m2.metric("Prêts / position", ready_count)
            m3.metric("Très proches", very_close_count)
            m4.metric("Proches", close_count)
            m5.metric("Safety incomplet", incomplete_count)
            m6.metric("Rejetés", rejected_count)
            m7.metric("Mayhem exclus", mayhem_count)
            m8.metric("Mayhem à vérifier", mayhem_unknown_count)

            st.markdown("### Coin actuellement le plus proche")
            nearest_left, nearest_middle, nearest_right = st.columns(
                [1.4, 1, 1]
            )
            with nearest_left:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"## #{int(nearest['Rang'])} "
                    f"{nearest['Ticker']} — {nearest['Nom']}"
                )
                st.write(
                    f"**Statut :** {nearest['Proximité']}"
                )
                st.write(
                    f"**Marché :** {nearest['Marché']}"
                )
                st.write(
                    f"**Cases cochées :** "
                    f"{nearest['Cases cochées']}"
                )
                st.write(
                    f"**Blocage principal :** "
                    f"{nearest['Blocage principal']}"
                )
                st.code(str(nearest["mint"]))
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )
            with nearest_middle:
                st.metric(
                    "Proximité d’entrée",
                    f"{float(nearest['Readiness %']):.1f} %",
                    (
                        f"{int(nearest['Cases manquantes'])} "
                        "case(s) manquante(s)"
                    ),
                )
                st.metric(
                    "Safety",
                    f"{float(nearest['Safety']):.1f} / 100",
                )
            with nearest_right:
                st.metric(
                    "Qualification",
                    (
                        f"{float(nearest['Qualification']):.1f} "
                        "/ 100"
                    ),
                )
                top_holder = nearest["Top holder %"]
                st.metric(
                    "Plus gros holder",
                    (
                        f"{float(top_holder):.2f} %"
                        if pd.notna(top_holder)
                        else "En attente"
                    ),
                )

            st.markdown("### Filtres")

            filter_left, filter_middle, filter_right = st.columns(
                [1.4, 1, 1]
            )
            with filter_left:
                search_text = st.text_input(
                    "Rechercher un ticker, un nom ou un contrat",
                    key="entry_ranking_search",
                ).strip().lower()
            with filter_middle:
                market_options = sorted(
                    ranking["Marché"].dropna().unique().tolist()
                )
                selected_markets = st.multiselect(
                    "Marché",
                    market_options,
                    default=market_options,
                    key="entry_ranking_market_filter",
                )
            with filter_right:
                proximity_options = ranking[
                    "Proximité"
                ].dropna().unique().tolist()
                selected_proximities = st.multiselect(
                    "Classe",
                    proximity_options,
                    default=proximity_options,
                    key="entry_ranking_proximity_filter",
                )

            filtered = ranking[
                ranking["Marché"].isin(selected_markets)
                & ranking["Proximité"].isin(
                    selected_proximities
                )
            ].copy()

            if search_text:
                search_mask = (
                    filtered["Ticker"]
                    .astype(str)
                    .str.lower()
                    .str.contains(
                        search_text,
                        regex=False,
                    )
                    | filtered["Nom"]
                    .astype(str)
                    .str.lower()
                    .str.contains(
                        search_text,
                        regex=False,
                    )
                    | filtered["mint"]
                    .astype(str)
                    .str.lower()
                    .str.contains(
                        search_text,
                        regex=False,
                    )
                )
                filtered = filtered[search_mask]

            show_limit = st.number_input(
                "Nombre de coins affichés",
                min_value=1,
                max_value=len(ranking),
                value=min(200, len(ranking)),
                step=1,
                key="entry_ranking_limit",
            )

            table_columns = [
                "Rang",
                "Ticker",
                "Nom",
                "Marché",
                "Mode d’entrée",
                "Mayhem",
                "Source Mayhem",
                "Proximité",
                "Cases cochées",
                "Readiness %",
                "Safety",
                "Qualification",
                "Top holder %",
                "Pipeline",
                "Cases manquantes",
                "Blocage principal",
                "Critères manquants",
                "Paire DEX",
            ]

            if filtered.empty:
                st.warning(
                    "Aucun coin ne correspond aux filtres."
                )
            else:
                st.dataframe(
                    filtered[
                        table_columns
                    ].head(show_limit),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Rang": st.column_config.NumberColumn(
                            "Rang",
                            format="#%d",
                        ),
                        "Ticker": "Ticker",
                        "Nom": "Nom",
                        "Marché": "Marché",
                        "Mode d’entrée": "Mode",
                        "Mayhem": "Mayhem",
                        "Source Mayhem": "Source Mayhem",
                        "Proximité": "Classe",
                        "Cases cochées": "Cases",
                        "Readiness %": st.column_config.ProgressColumn(
                            "Proximité",
                            min_value=0,
                            max_value=100,
                            format="%.1f %%",
                        ),
                        "Safety": st.column_config.ProgressColumn(
                            "Safety",
                            min_value=0,
                            max_value=100,
                            format="%.1f",
                        ),
                        "Qualification": st.column_config.ProgressColumn(
                            "Qualification",
                            min_value=0,
                            max_value=100,
                            format="%.1f",
                        ),
                        "Top holder %": st.column_config.NumberColumn(
                            "Top holder",
                            format="%.2f %%",
                        ),
                        "Pipeline": "Pipeline",
                        "Cases manquantes": "Manquantes",
                        "Blocage principal": st.column_config.TextColumn(
                            "Blocage principal",
                            width="medium",
                        ),
                        "Critères manquants": st.column_config.TextColumn(
                            "Autres critères manquants",
                            width="large",
                        ),
                        "Paire DEX": st.column_config.LinkColumn(
                            "Paire",
                            display_text="Ouvrir",
                        ),
                    },
                )

                chart_data = filtered[
                    [
                        "Ticker",
                        "Readiness %",
                        "Proximité",
                    ]
                ].head(25)
                figure = go.Figure(
                    go.Bar(
                        x=chart_data["Readiness %"],
                        y=chart_data["Ticker"],
                        orientation="h",
                        text=chart_data["Proximité"],
                        textposition="auto",
                    )
                )
                ranking_layout = {
                    **PLOTLY_LAYOUT,
                    "height": max(
                        360,
                        len(chart_data) * 28,
                    ),
                    "yaxis": {
                        "autorange": "reversed",
                        "title": "",
                    },
                    "xaxis": {
                        "range": [0, 100],
                        "title": "Cases validées (%)",
                    },
                    "margin": {
                        "l": 10,
                        "r": 10,
                        "t": 20,
                        "b": 50,
                    },
                }
                figure.update_layout(**ranking_layout)
                st.plotly_chart(
                    figure,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

                st.markdown(
                    "### Détail des cases d’un coin"
                )
                selectable_mints = filtered[
                    "mint"
                ].head(show_limit).tolist()

                selected_mint = st.selectbox(
                    "Coin à examiner",
                    selectable_mints,
                    format_func=lambda mint: (
                        f"#{int(ranking.loc[
                            ranking['mint'] == mint,
                            'Rang'
                        ].iloc[0])} — "
                        f"{ranking.loc[
                            ranking['mint'] == mint,
                            'Ticker'
                        ].iloc[0]} — "
                        f"{mint[:8]}…{mint[-6:]}"
                    ),
                    key="entry_ranking_detail_selector",
                )

                selected_summary = ranking[
                    ranking["mint"] == selected_mint
                ].iloc[0]
                selected_checks = pd.DataFrame(
                    ranking_details.get(
                        selected_mint,
                        [],
                    )
                )

                summary_left, summary_right = st.columns(
                    [1, 1.5]
                )
                with summary_left:
                    st.markdown(
                        '<div class="panel">',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"### #{int(selected_summary['Rang'])} "
                        f"{selected_summary['Ticker']}"
                    )
                    st.write(
                        f"**Classe :** "
                        f"{selected_summary['Proximité']}"
                    )
                    st.write(
                        f"**Proximité :** "
                        f"{float(selected_summary['Readiness %']):.1f} %"
                    )
                    st.write(
                        f"**Cases :** "
                        f"{selected_summary['Cases cochées']}"
                    )
                    st.write(
                        f"**Manquantes :** "
                        f"{int(selected_summary['Cases manquantes'])}"
                    )
                    st.write(
                        f"**Raison du pipeline :** "
                        f"{selected_summary['Raison pipeline'] or '—'}"
                    )
                    st.code(selected_mint)
                    st.markdown(
                        "</div>",
                        unsafe_allow_html=True,
                    )
                with summary_right:
                    if selected_checks.empty:
                        st.info(
                            "Aucun détail disponible."
                        )
                    else:
                        selected_checks["État"] = (
                            selected_checks["Validé"]
                            .map(
                                {
                                    True: "✓ VALIDÉ",
                                    False: "✗ MANQUANT",
                                }
                            )
                        )
                        selected_checks["Type"] = (
                            selected_checks[
                                "Blocage critique"
                            ]
                            .map(
                                {
                                    True: "CRITIQUE",
                                    False: "STANDARD",
                                }
                            )
                        )
                        st.dataframe(
                            selected_checks[
                                [
                                    "État",
                                    "Critère",
                                    "Valeur actuelle",
                                    "Objectif",
                                    "Type",
                                ]
                            ],
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "État": "État",
                                "Critère": "Critère",
                                "Valeur actuelle": "Actuel",
                                "Objectif": "Objectif",
                                "Type": "Importance",
                            },
                        )

                missing_checks = (
                    selected_checks[
                        ~selected_checks["Validé"]
                    ]
                    if not selected_checks.empty
                    else pd.DataFrame()
                )
                if missing_checks.empty:
                    st.success(
                        "Toutes les cases sont cochées. Le coin est "
                        "prêt, déjà en position ou attend uniquement "
                        "le prochain cycle d’exécution."
                    )
                else:
                    critical_missing = missing_checks[
                        missing_checks["Blocage critique"]
                    ]
                    standard_missing = missing_checks[
                        ~missing_checks["Blocage critique"]
                    ]

                    if not critical_missing.empty:
                        st.error(
                            "Blocages critiques : "
                            + " • ".join(
                                critical_missing[
                                    "Critère"
                                ].astype(str).tolist()
                            )
                        )
                    if not standard_missing.empty:
                        st.warning(
                            "Étapes restantes : "
                            + " • ".join(
                                standard_missing[
                                    "Critère"
                                ].astype(str).tolist()
                            )
                        )

    elif selected_page == "Radar hybride":
        radar_status = state.get("radar_status", "STOPPED")
        radar_rpc_status = state.get(
            "radar_rpc_status",
            "WAITING",
        )
        market_status = state.get(
            "hybrid_market_status",
            "STOPPED",
        )
        market_cooldown_until = state.get(
            "hybrid_market_cooldown_until",
            "",
        )
        radar_error = state.get("radar_last_error", "")
        market_error = state.get(
            "hybrid_market_last_error",
            "",
        )

        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Radar hybride Pump</div>'
            '<div class="section-note">'
            'Nouveaux launches, bonding curves et coins migrés sur DEX'
            '</div></div>',
            unsafe_allow_html=True,
        )

        status_left, status_right = st.columns(2)
        with status_left:
            if radar_status == "RUNNING" and radar_rpc_status == "OK":
                st.success(
                    "Flux CreateEvent connecté : Mayhem et données initiales décodés immédiatement."
                )
            elif radar_status == "RUNNING" and radar_rpc_status == "COOLDOWN":
                st.warning(
                    "Flux CreateEvent connecté, mais enrichissement RPC "
                    "en pause automatique pour éviter un nouveau 429."
                )
            elif radar_status == "RUNNING":
                st.info(
                    "Flux CreateEvent connecté; premier enrichissement RPC "
                    "en attente."
                )
            elif radar_status == "RECONNECTING":
                st.warning("Reconnexion du WebSocket en cours.")
            else:
                st.error("New Coin Radar arrêté.")
        with status_right:
            if market_status == "RUNNING":
                st.success(
                    "Scanner DEX actif : cadence limitée et paires suivies."
                )
            elif market_status == "IDLE":
                st.info(
                    "Scanner DEX au repos : aucun coin proche de la migration."
                )
            elif market_status == "COOLDOWN":
                st.warning(
                    "Scanner DEX en pause automatique après un 429. "
                    f"Reprise prévue : {market_cooldown_until or 'bientôt'}."
                )
            elif market_status == "ERROR":
                st.warning("Scanner DEX dégradé; nouvel essai temporisé.")
            else:
                st.error("Hybrid Market Scanner arrêté.")

        if radar_error:
            st.caption(f"Radar : {radar_error}")
        if market_error:
            st.caption(f"Marché : {market_error}")

        if launches.empty:
            st.info(
                "Aucun lancement enregistré. Laisse le radar "
                "fonctionner quelques instants."
            )
        else:
            radar = launches.copy()

            if not safety.empty:
                safety_columns = [
                    "mint",
                    "safety_score",
                    "decision",
                    "hard_reject",
                    "analysis_status",
                    "holder_analysis_status",
                    "provisional_score",
                    "top1_pct",
                    "top10_pct",
                    "rpc_attempts",
                    "error_text",
                ]
                radar = radar.merge(
                    safety[safety_columns],
                    on="mint",
                    how="left",
                )
            else:
                radar["safety_score"] = pd.NA
                radar["decision"] = "PENDING"
                radar["hard_reject"] = 0
                radar["analysis_status"] = "PENDING"
                radar["holder_analysis_status"] = "PENDING"
                radar["provisional_score"] = 1
                radar["top1_pct"] = pd.NA
                radar["top10_pct"] = pd.NA
                radar["rpc_attempts"] = 0
                radar["error_text"] = ""

            if not qualifications.empty:
                qualification_columns = [
                    "mint",
                    "state",
                    "qualification_score",
                    "observation_samples",
                    "stable_samples",
                    "reason",
                ]
                radar = radar.merge(
                    qualifications[qualification_columns],
                    on="mint",
                    how="left",
                )
            else:
                radar["state"] = "PENDING"
                radar["qualification_score"] = pd.NA
                radar["observation_samples"] = 0
                radar["stable_samples"] = 0
                radar["reason"] = ""

            confirmation_pending_count = int(
                radar["last_updated_at"].isna().sum()
            )
            if confirmation_pending_count:
                st.info(
                    f"{confirmation_pending_count} coin(s) ont déjà leur "
                    "statut Mayhem et leurs données initiales issus de "
                    "CreateEvent, mais attendent encore la confirmation "
                    "BondingCurve pour le devis paper actualisé."
                )

            radar["decision"] = radar["decision"].fillna("PENDING")
            radar["state"] = radar["state"].fillna("PENDING")
            radar["market_mode"] = (
                radar["market_mode"]
                .fillna("BONDING")
            )
            radar["score_type"] = radar.apply(
                lambda row: (
                    "PROVISOIRE"
                    if int(
                        row.get("provisional_score")
                        if pd.notna(
                            row.get("provisional_score")
                        )
                        else 0
                    ) == 1
                    or str(row.get("analysis_status"))
                    != "COMPLETE"
                    else "COMPLET"
                ),
                axis=1,
            )
            radar["display_price_usd"] = radar.apply(
                lambda row: (
                    row.get("market_price_usd")
                    if row.get("market_mode") == "MIGRATED_DEX"
                    and pd.notna(row.get("market_price_usd"))
                    else row.get("curve_price_usd")
                ),
                axis=1,
            )
            radar["display_price_sol"] = radar.apply(
                lambda row: (
                    row.get("market_price_sol")
                    if row.get("market_mode") == "MIGRATED_DEX"
                    and pd.notna(row.get("market_price_sol"))
                    else row.get("curve_price_sol")
                ),
                axis=1,
            )
            current_time = pd.Timestamp.now(tz="UTC")
            radar["age_seconds"] = (
                current_time - radar["detected_at"]
            ).dt.total_seconds().clip(lower=0)
            radar["âge"] = radar["age_seconds"].apply(
                lambda seconds: (
                    f"{int(seconds)} s"
                    if seconds < 60
                    else f"{int(seconds // 60)} min"
                    if seconds < 3600
                    else f"{seconds / 3600:.1f} h"
                ),
            )

            radar["Mayhem"] = radar["is_mayhem_mode"].apply(
                lambda value: (
                    "OUI — EXCLU"
                    if pd.notna(value) and bool(int(value))
                    else "NON"
                    if pd.notna(value)
                    else "À VÉRIFIER"
                )
            )
            total = len(radar)
            mayhem_count = int(
                (radar["Mayhem"] == "OUI — EXCLU").sum()
            )
            bonding_count = int(
                (radar["market_mode"] == "BONDING").sum()
            )
            migrated_count = int(
                (radar["market_mode"] == "MIGRATED_DEX").sum()
            )
            pair_count = int(radar["pair_address"].notna().sum())
            score_count = int(radar["safety_score"].notna().sum())
            complete_score_count = int(
                (radar["score_type"] == "COMPLET").sum()
            )
            ready_count = int(
                radar["state"].isin(
                    ["READY", "PAPER_POSITION"]
                ).sum()
            )

            a, b, c, d, e, f, g, h, i, j = st.columns(10)
            a.metric("Détectés", total)
            b.metric("Bonding", bonding_count)
            c.metric("Migrés DEX", migrated_count)
            d.metric("Avec paire", pair_count)
            e.metric("Scores visibles", score_count)
            f.metric("Scores complets", complete_score_count)
            g.metric("Ready / position", ready_count)
            h.metric("Mayhem exclus", mayhem_count)
            i.metric("Mayhem immédiat", immediate_count)
            j.metric("Conflits", conflict_count)

            display_columns = [
                "detected_at",
                "âge",
                "symbol",
                "name",
                "market_mode",
                "Mayhem",
                "mayhem_source",
                "mayhem_conflict",
                "create_event_version",
                "event_detection_latency_ms",
                "lifecycle_state",
                "progress_pct",
                "display_price_usd",
                "market_liquidity_usd",
                "market_volume_5m_usd",
                "market_buys_5m",
                "market_sells_5m",
                "safety_score",
                "score_type",
                "holder_analysis_status",
                "decision",
                "qualification_score",
                "state",
                "pair_url",
            ]

            st.dataframe(
                radar[display_columns].head(500),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "detected_at": st.column_config.DatetimeColumn(
                        "Détection",
                        format="DD/MM HH:mm:ss",
                    ),
                    "âge": "Âge",
                    "symbol": "Ticker",
                    "name": "Nom",
                    "market_mode": "Marché",
                    "Mayhem": "Mayhem",
                    "mayhem_source": "Source Mayhem",
                    "mayhem_conflict": st.column_config.CheckboxColumn(
                        "Conflit"
                    ),
                    "create_event_version": "CreateEvent",
                    "event_detection_latency_ms": st.column_config.NumberColumn(
                        "Latence événement",
                        format="%.0f ms",
                    ),
                    "lifecycle_state": "Cycle",
                    "progress_pct": st.column_config.ProgressColumn(
                        "Bonding",
                        min_value=0,
                        max_value=100,
                        format="%.2f %%",
                    ),
                    "display_price_usd": st.column_config.NumberColumn(
                        "Prix",
                        format="$ %.10f",
                    ),
                    "market_liquidity_usd": st.column_config.NumberColumn(
                        "Liquidité DEX",
                        format="$ %.0f",
                    ),
                    "market_volume_5m_usd": st.column_config.NumberColumn(
                        "Volume 5 min",
                        format="$ %.0f",
                    ),
                    "market_buys_5m": "Achats 5 min",
                    "market_sells_5m": "Ventes 5 min",
                    "safety_score": st.column_config.ProgressColumn(
                        "Safety",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "score_type": "Type score",
                    "holder_analysis_status": "Holders",
                    "decision": "Décision",
                    "qualification_score": st.column_config.ProgressColumn(
                        "Qualification",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "state": "Pipeline",
                    "pair_url": st.column_config.LinkColumn(
                        "Paire DEX",
                        display_text="Ouvrir",
                    ),
                },
            )

            selected_mint = st.selectbox(
                "Inspecter un coin",
                radar["mint"].head(500).tolist(),
                format_func=lambda mint: (
                    f"{radar.loc[
                        radar['mint'] == mint,
                        'symbol'
                    ].iloc[0]} — {mint[:8]}…{mint[-6:]}"
                ),
                key="hybrid_radar_selector",
            )
            selected = radar[
                radar["mint"] == selected_mint
            ].iloc[0]

            left, right = st.columns(2)
            with left:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"### {selected['symbol']} — {selected['name']}"
                )
                st.code(selected["mint"])
                st.write(
                    f"**Marché :** {selected['market_mode']}"
                )
                st.write(
                    f"**Cycle :** {selected['lifecycle_state']}"
                )
                st.write(
                    f"**Safety :** "
                    f"{float(selected['safety_score']):.1f} / 100 "
                    f"({selected['score_type']})"
                    if pd.notna(selected["safety_score"])
                    else "**Safety :** en attente du premier cycle"
                )
                st.write(
                    f"**Holders :** "
                    f"{selected['holder_analysis_status']}"
                )
                st.write(
                    f"**Pipeline :** {selected['state']}"
                )
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

            with right:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                if selected["market_mode"] == "MIGRATED_DEX":
                    st.markdown("### Marché migré")
                    st.write(
                        f"**DEX :** {selected.get('dex_id') or '—'}"
                    )
                    st.write(
                        f"**Liquidité :** "
                        f"${float(selected.get('market_liquidity_usd') or 0):,.0f}"
                    )
                    st.write(
                        f"**Volume 5 min :** "
                        f"${float(selected.get('market_volume_5m_usd') or 0):,.0f}"
                    )
                    st.write(
                        f"**Achats / ventes 5 min :** "
                        f"{int(selected.get('market_buys_5m') or 0)} / "
                        f"{int(selected.get('market_sells_5m') or 0)}"
                    )
                    st.write(
                        f"**Token vault de pool :** "
                        f"`{selected.get('pool_base_token_account') or 'en attente'}`"
                    )
                else:
                    st.markdown("### Bonding curve")
                    st.write(
                        f"**Progression :** "
                        f"{float(selected.get('progress_pct') or 0):.2f} %"
                    )
                    st.write(
                        f"**SOL réels :** "
                        f"{float(selected.get('real_quote_reserves_sol') or 0):.4f}"
                    )
                    st.write(
                        f"**Bonding curve :** "
                        f"`{selected.get('bonding_curve') or '—'}`"
                    )
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

    elif selected_page == "Safety Engine":
        safety_status = state.get("safety_status", "STOPPED")
        safety_error = state.get("safety_last_error", "")
        safety_last_scan = state.get("safety_last_scan", "")
        queue_pending = int(
            state.get("safety_queue_pending") or 0
        )
        oldest_pending_age = float(
            state.get(
                "safety_oldest_pending_age_seconds"
            )
            or 0
        )
        starved_count = int(
            state.get("safety_starved_count") or 0
        )
        parallel_workers = int(
            state.get("safety_full_parallel_workers") or 0
        )
        rpc_limited = int(
            state.get("safety_rpc_rate_limited") or 0
        )

        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Safety prioritaire V12</div>'
            '<div class="section-note">'
            'Nouveaux coins prioritaires, anciens protégés contre la famine'
            '</div></div>',
            unsafe_allow_html=True,
        )

        if safety_status == "RUNNING":
            st.success(
                "Safety actif. Les nouveaux coins reçoivent d’abord "
                "un score provisoire, puis une analyse holders complète."
            )
        elif safety_status == "ERROR":
            st.warning(
                "Le Safety rencontre une erreur RPC, mais conserve "
                "les dernières analyses complètes valides."
            )
        else:
            st.error("Safety Recovery Engine arrêté.")

        if safety_last_scan:
            st.caption(f"Dernier cycle : {safety_last_scan}")
        if safety_error:
            st.caption(f"Dernière erreur : {safety_error}")

        if safety.empty:
            st.info(
                "Aucun score enregistré. Vérifie le superviseur et "
                "le fichier logs/safety.log."
            )
        else:
            safety_view = safety.copy()
            safety_view["score_type"] = safety_view.apply(
                lambda row: (
                    "PROVISOIRE"
                    if int(
                        row.get("provisional_score")
                        if pd.notna(
                            row.get("provisional_score")
                        )
                        else 0
                    ) == 1
                    or str(row.get("analysis_status"))
                    != "COMPLETE"
                    else "COMPLET"
                ),
                axis=1,
            )

            assessed = len(safety_view)
            complete = int(
                (safety_view["analysis_status"] == "COMPLETE").sum()
            )
            provisional = int(
                (safety_view["score_type"] == "PROVISOIRE").sum()
            )
            holder_complete = int(
                (
                    safety_view["holder_analysis_status"]
                    == "COMPLETE"
                ).sum()
            )
            qualified = int(
                (safety_view["decision"] == "QUALIFIED").sum()
            )
            rejected = int(
                (safety_view["decision"] == "REJECTED").sum()
            )

            a, b, c, d, e, f, g, h, i = st.columns(9)
            a.metric("Scores visibles", assessed)
            b.metric("Scores complets", complete)
            c.metric("Provisoires", provisional)
            d.metric("Holders complets", holder_complete)
            e.metric("Qualifiés", qualified)
            f.metric("Rejetés", rejected)
            g.metric("File Safety", queue_pending)
            h.metric(
                "Plus ancien en attente",
                human_age(oldest_pending_age),
                (
                    f"{starved_count} > 45 s"
                    if starved_count
                    else "aucun retard critique"
                ),
            )
            i.metric(
                "RPC parallèle",
                f"{parallel_workers} workers",
                f"{rpc_limited} limitations",
            )

            def decode_list(raw: object) -> str:
                try:
                    values = json.loads(str(raw or "[]"))
                    return " • ".join(
                        str(value) for value in values
                    )
                except Exception:
                    return str(raw or "")

            safety_view["raisons"] = (
                safety_view["reasons_json"]
                .apply(decode_list)
            )
            safety_view["alertes"] = (
                safety_view["warnings_json"]
                .apply(decode_list)
            )

            columns = [
                "assessed_at",
                "symbol",
                "token_name",
                "market_mode",
                "is_mayhem_mode",
                "safety_score",
                "score_type",
                "decision",
                "analysis_status",
                "holder_analysis_status",
                "mint_authority_revoked",
                "freeze_authority_revoked",
                "top1_pct",
                "top10_pct",
                "buys_5m",
                "sells_5m",
                "rpc_attempts",
                "error_text",
            ]
            st.dataframe(
                safety_view[columns],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "assessed_at": st.column_config.DatetimeColumn(
                        "Analyse",
                        format="DD/MM HH:mm:ss",
                    ),
                    "symbol": "Ticker",
                    "token_name": "Nom",
                    "market_mode": "Marché",
                    "is_mayhem_mode": st.column_config.CheckboxColumn(
                        "Mayhem"
                    ),
                    "safety_score": st.column_config.ProgressColumn(
                        "Safety",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "score_type": "Type score",
                    "decision": "Décision",
                    "analysis_status": "Analyse",
                    "holder_analysis_status": "Holders",
                    "mint_authority_revoked": st.column_config.CheckboxColumn(
                        "Mint révoqué"
                    ),
                    "freeze_authority_revoked": st.column_config.CheckboxColumn(
                        "Freeze révoqué"
                    ),
                    "top1_pct": st.column_config.NumberColumn(
                        "Top holder",
                        format="%.2f %%",
                    ),
                    "top10_pct": st.column_config.NumberColumn(
                        "Top 10",
                        format="%.2f %%",
                    ),
                    "buys_5m": "Achats 5 min",
                    "sells_5m": "Ventes 5 min",
                    "rpc_attempts": "Tentatives RPC",
                    "error_text": st.column_config.TextColumn(
                        "Erreur",
                        width="large",
                    ),
                },
            )

            selected_mint = st.selectbox(
                "Inspecter une note Safety",
                safety_view["mint"].tolist(),
                format_func=lambda mint: (
                    f"{safety_view.loc[
                        safety_view['mint'] == mint,
                        'symbol'
                    ].iloc[0]} — {mint[:8]}…{mint[-6:]}"
                ),
                key="safety_v11_selector",
            )
            selected = safety_view[
                safety_view["mint"] == selected_mint
            ].iloc[0]

            left, right = st.columns([1, 1.45])
            with left:
                score_value = float(
                    selected["safety_score"] or 0
                )
                gauge = go.Figure(
                    go.Indicator(
                        mode="gauge+number",
                        value=score_value,
                        number={"suffix": " / 100"},
                        title={
                            "text": (
                                f"{selected['decision']} — "
                                f"{selected['score_type']}"
                            )
                        },
                        gauge={
                            "axis": {"range": [0, 100]},
                            "bar": {"color": "#25e6a5"},
                            "steps": [
                                {
                                    "range": [0, 45],
                                    "color": "rgba(255,92,120,.18)",
                                },
                                {
                                    "range": [45, 72],
                                    "color": "rgba(255,191,105,.18)",
                                },
                                {
                                    "range": [72, 100],
                                    "color": "rgba(37,230,165,.14)",
                                },
                            ],
                        },
                    )
                )
                gauge.update_layout(
                    **PLOTLY_LAYOUT,
                    height=300,
                )
                st.plotly_chart(
                    gauge,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

            with right:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"### {selected['symbol']} — "
                    f"{selected['token_name']}"
                )
                st.code(selected["mint"])
                st.write(
                    f"**Marché :** {selected['market_mode']}"
                )
                st.write(
                    f"**Analyse générale :** "
                    f"{selected['analysis_status']}"
                )
                st.write(
                    f"**Analyse holders :** "
                    f"{selected['holder_analysis_status']}"
                )
                st.write(
                    f"**Top holder hors pool :** "
                    f"{float(selected['top1_pct'] or 0):.2f} %"
                )
                st.write(
                    f"**Pool ignorée :** "
                    f"`{selected.get('ignored_pool_token_account') or 'bonding curve / en attente'}`"
                )
                st.write(
                    f"**Source concentration :** "
                    f"{selected.get('concentration_source') or '—'}"
                )
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

            positive, warning = st.columns(2)
            with positive:
                st.markdown("#### Points positifs")
                if selected["raisons"]:
                    st.success(selected["raisons"])
                else:
                    st.info("Aucun point positif confirmé.")
            with warning:
                st.markdown("#### Alertes")
                if selected["alertes"]:
                    st.warning(selected["alertes"])
                else:
                    st.success("Aucune alerte enregistrée.")

            st.caption(
                "Un score PROVISOIRE est informatif et ne peut pas "
                "ouvrir une position. Une entrée exige une note "
                "COMPLÈTE et une analyse holders COMPLETE."
            )

    elif selected_page == "Qualification":
        pipeline_status = state.get(
            "qualification_status",
            "STOPPED",
        )
        pipeline_error = state.get(
            "qualification_last_error",
            "",
        )
        pipeline_cycle = state.get(
            "qualification_last_cycle",
            "",
        )

        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Qualification hybride</div>'
            '<div class="section-note">'
            'Bonding et MIGRATED_DEX partagent une seule position paper'
            '</div></div>',
            unsafe_allow_html=True,
        )

        if pipeline_status == "RUNNING":
            st.success(
                "Hybrid Strategy Engine en ligne."
            )
        elif pipeline_status == "ERROR":
            st.warning(
                "Le pipeline rencontre une erreur temporaire."
            )
        else:
            st.error("Hybrid Strategy Engine arrêté.")

        if pipeline_cycle:
            st.caption(f"Dernier cycle : {pipeline_cycle}")
        if pipeline_error:
            st.caption(f"Dernière erreur : {pipeline_error}")

        if qualifications.empty:
            st.info(
                "Aucun candidat. Le Safety doit d’abord produire "
                "une analyse complète et qualifiée."
            )
        else:
            pipeline = qualifications.copy()
            pipeline["market_mode"] = (
                pipeline["market_mode"]
                .fillna("BONDING")
            )
            bonding = int(
                (pipeline["market_mode"] == "BONDING").sum()
            )
            migrated = int(
                (
                    pipeline["market_mode"]
                    == "MIGRATED_DEX"
                ).sum()
            )
            observation = int(
                (pipeline["state"] == "OBSERVATION").sum()
            )
            ready = int(
                (pipeline["state"] == "READY").sum()
            )
            paper = int(
                (pipeline["state"] == "PAPER_POSITION").sum()
            )
            closed = int(
                (pipeline["state"] == "CLOSED").sum()
            )
            rejected = int(
                (pipeline["state"] == "REJECTED").sum()
            )

            a, b, c, d, e, f, g = st.columns(7)
            a.metric("Bonding", bonding)
            b.metric("Migrés DEX", migrated)
            c.metric("Observation", observation)
            d.metric("Ready", ready)
            e.metric("Position paper", paper)
            f.metric("Clôturés", closed)
            g.metric("Rejetés", rejected)

            display_columns = [
                "symbol",
                "token_name",
                "market_mode",
                "state",
                "safety_score",
                "qualification_score",
                "observation_samples",
                "stable_samples",
                "current_progress_pct",
                "price_change_pct",
                "liquidity_usd",
                "volume_5m_usd",
                "buys_5m",
                "sells_5m",
                "reason",
            ]
            st.dataframe(
                pipeline[display_columns],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "symbol": "Ticker",
                    "token_name": "Nom",
                    "market_mode": "Marché",
                    "state": "État",
                    "safety_score": st.column_config.ProgressColumn(
                        "Safety",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "qualification_score": st.column_config.ProgressColumn(
                        "Qualification",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "observation_samples": "Échantillons",
                    "stable_samples": "Stables",
                    "current_progress_pct": st.column_config.NumberColumn(
                        "Bonding",
                        format="%.2f %%",
                    ),
                    "price_change_pct": st.column_config.NumberColumn(
                        "Prix Δ",
                        format="%+.2f %%",
                    ),
                    "liquidity_usd": st.column_config.NumberColumn(
                        "Liquidité",
                        format="$ %.0f",
                    ),
                    "volume_5m_usd": st.column_config.NumberColumn(
                        "Volume 5 min",
                        format="$ %.0f",
                    ),
                    "buys_5m": "Achats",
                    "sells_5m": "Ventes",
                    "reason": st.column_config.TextColumn(
                        "Explication",
                        width="large",
                    ),
                },
            )

            selected_mint = st.selectbox(
                "Inspecter un candidat",
                pipeline["mint"].tolist(),
                format_func=lambda mint: (
                    f"{pipeline.loc[
                        pipeline['mint'] == mint,
                        'symbol'
                    ].iloc[0]} — {mint[:8]}…{mint[-6:]}"
                ),
                key="hybrid_qualification_selector",
            )
            selected = pipeline[
                pipeline["mint"] == selected_mint
            ].iloc[0]

            left, right = st.columns(2)
            with left:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"### {selected['symbol']} — "
                    f"{selected['token_name']}"
                )
                st.code(selected["mint"])
                st.write(
                    f"**Marché :** {selected['market_mode']}"
                )
                st.write(f"**État :** {selected['state']}")
                st.write(
                    f"**Safety / Qualification :** "
                    f"{float(selected['safety_score'] or 0):.1f} / "
                    f"{float(selected['qualification_score'] or 0):.1f}"
                )
                st.write(
                    f"**Échantillons :** "
                    f"{int(selected['observation_samples'] or 0)}, "
                    f"dont {int(selected['stable_samples'] or 0)} stables"
                )
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )
            with right:
                st.markdown(
                    '<div class="panel">',
                    unsafe_allow_html=True,
                )
                if selected["market_mode"] == "MIGRATED_DEX":
                    st.markdown("### Critères DEX")
                    st.write(
                        f"**Liquidité :** "
                        f"${float(selected['liquidity_usd'] or 0):,.0f}"
                    )
                    st.write(
                        f"**Volume 5 min :** "
                        f"${float(selected['volume_5m_usd'] or 0):,.0f}"
                    )
                    st.write(
                        f"**Achats / ventes :** "
                        f"{int(selected['buys_5m'] or 0)} / "
                        f"{int(selected['sells_5m'] or 0)}"
                    )
                else:
                    st.markdown("### Critères bonding")
                    st.write(
                        f"**Progression :** "
                        f"{float(selected['current_progress_pct'] or 0):.2f} %"
                    )
                    st.write(
                        f"**Progression Δ :** "
                        f"{float(selected['progress_delta_pct'] or 0):+.2f} point"
                    )
                st.write(
                    f"**Raison :** {selected['reason']}"
                )
                st.markdown(
                    "</div>",
                    unsafe_allow_html=True,
                )

            st.markdown("#### Journal des transitions")
            token_events = qualification_events[
                qualification_events["mint"] == selected_mint
            ].head(100)
            if token_events.empty:
                st.info("Aucune transition enregistrée.")
            else:
                st.dataframe(
                    token_events[
                        [
                            "timestamp",
                            "previous_state",
                            "new_state",
                            "qualification_score",
                            "reason",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "timestamp": st.column_config.DatetimeColumn(
                            "Heure",
                            format="DD/MM HH:mm:ss",
                        ),
                        "previous_state": "Avant",
                        "new_state": "Après",
                        "qualification_score": "Score",
                        "reason": st.column_config.TextColumn(
                            "Raison",
                            width="large",
                        ),
                    },
                )

    elif selected_page == "Replay & Backtest":
        recorder_status = state.get(
            "recorder_status",
            "STOPPED",
        )
        recorder_error = state.get(
            "recorder_last_error",
            "",
        )
        recorder_last_sample = state.get(
            "recorder_last_sample",
            "",
        )
        sample_count_state = int(
            state.get("recorder_sample_count") or 0
        )
        event_count_state = int(
            state.get("recorder_event_count") or 0
        )

        st.markdown(
            '<div class="section-head"><div class="section-title">'
            'Replay événementiel et laboratoire de backtest</div>'
            '<div class="section-note">'
            'Données enregistrées localement toutes les cinq secondes</div></div>',
            unsafe_allow_html=True,
        )

        if recorder_status == "RUNNING":
            st.success(
                "Event Recorder en ligne. "
                "Les observations sont conservées dans SQLite."
            )
        elif recorder_status == "ERROR":
            st.warning(
                "L’Event Recorder rencontre une erreur temporaire."
            )
        else:
            st.error(
                "L’Event Recorder est arrêté. Vérifie sa fenêtre noire."
            )

        if recorder_last_sample:
            st.caption(
                f"Dernier échantillon : {recorder_last_sample}"
            )
        if recorder_error:
            st.caption(f"Dernière erreur : {recorder_error}")

        runs_count = query(
            "SELECT COUNT(*) AS count FROM backtest_runs"
        )
        trades_count = query(
            "SELECT COUNT(*) AS count FROM backtest_trades"
        )
        recorded_tokens = query(
            "SELECT COUNT(DISTINCT mint) AS count FROM research_samples"
        )

        a, b, c, d, e = st.columns(5)
        a.metric("Échantillons", sample_count_state)
        b.metric("Événements", event_count_state)
        c.metric(
            "Tokens enregistrés",
            int(recorded_tokens.iloc[0]["count"])
            if not recorded_tokens.empty
            else 0,
        )
        d.metric(
            "Backtests",
            int(runs_count.iloc[0]["count"])
            if not runs_count.empty
            else 0,
        )
        e.metric(
            "Trades simulés",
            int(trades_count.iloc[0]["count"])
            if not trades_count.empty
            else 0,
        )

        token_summary = query(
            """
            SELECT
                mint,
                MAX(symbol) AS symbol,
                MAX(token_name) AS token_name,
                COUNT(*) AS samples,
                MIN(timestamp) AS first_sample,
                MAX(timestamp) AS last_sample,
                MAX(progress_pct) AS max_progress_pct,
                MAX(safety_score) AS max_safety_score,
                MAX(qualification_score) AS max_qualification_score
            FROM research_samples
            GROUP BY mint
            ORDER BY datetime(last_sample) DESC
            LIMIT 500
            """
        )

        if token_summary.empty:
            st.info(
                "Le recorder n’a pas encore accumulé de données. "
                "Laisse V12 fonctionner quelques minutes."
            )
        else:
            parse_dates(
                token_summary,
                ["first_sample", "last_sample"],
            )

            st.markdown("### Rejouer un token")
            selected_mint = st.selectbox(
                "Token enregistré",
                token_summary["mint"].tolist(),
                format_func=lambda mint: (
                    f"{token_summary.loc[token_summary['mint'] == mint, 'symbol'].iloc[0]}"
                    f" — {mint[:8]}…{mint[-6:]}"
                ),
                key="replay_token_selector",
            )

            samples = query(
                """
                SELECT *
                FROM research_samples
                WHERE mint = ?
                ORDER BY datetime(timestamp), id
                LIMIT 10000
                """,
                (selected_mint,),
            )
            events = query(
                """
                SELECT *
                FROM research_events
                WHERE mint = ?
                ORDER BY datetime(timestamp) DESC, id DESC
                LIMIT 500
                """,
                (selected_mint,),
            )
            parse_dates(samples, ["timestamp", "detected_at"])
            parse_dates(events, ["timestamp"])

            if not samples.empty:
                selected_symbol = str(
                    samples.iloc[-1]["symbol"] or ""
                )
                first_time = samples.iloc[0]["timestamp"]
                last_time = samples.iloc[-1]["timestamp"]
                duration_seconds = (
                    last_time - first_time
                ).total_seconds()

                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Échantillons token", len(samples))
                r2.metric(
                    "Durée enregistrée",
                    (
                        f"{duration_seconds / 60:.1f} min"
                        if duration_seconds < 3600
                        else f"{duration_seconds / 3600:.1f} h"
                    ),
                )
                r3.metric(
                    "Progression max",
                    f"{samples['progress_pct'].max():.2f} %"
                    if samples["progress_pct"].notna().any()
                    else "—",
                )
                r4.metric(
                    "Safety max",
                    f"{samples['safety_score'].max():.1f}"
                    if samples["safety_score"].notna().any()
                    else "—",
                )

                chart_left, chart_right = st.columns(2)

                with chart_left:
                    progress_figure = go.Figure()
                    progress_figure.add_trace(
                        go.Scatter(
                            x=samples["timestamp"],
                            y=samples["progress_pct"],
                            mode="lines",
                            line=dict(
                                color="#9b7cff",
                                width=2.5,
                            ),
                            name="Bonding",
                            hovertemplate="%{y:.2f}%<extra></extra>",
                        )
                    )
                    progress_figure.update_layout(
                        **PLOTLY_LAYOUT,
                        height=345,
                        title=(
                            f"{selected_symbol} — progression de courbe"
                        ),
                        showlegend=False,
                        xaxis=dict(showgrid=False),
                        yaxis=dict(
                            gridcolor="rgba(255,255,255,.055)",
                            range=[0, 100],
                            ticksuffix=" %",
                        ),
                    )
                    st.plotly_chart(
                        progress_figure,
                        use_container_width=True,
                        config={"displayModeBar": False},
                    )

                with chart_right:
                    price_samples = samples[
                        samples["curve_price_sol"].notna()
                    ]
                    price_figure = go.Figure()
                    price_figure.add_trace(
                        go.Scatter(
                            x=price_samples["timestamp"],
                            y=price_samples["curve_price_sol"],
                            mode="lines",
                            line=dict(
                                color="#32d7ff",
                                width=2.5,
                            ),
                            name="Prix",
                            hovertemplate="%{y:.12f} SOL<extra></extra>",
                        )
                    )
                    price_figure.update_layout(
                        **PLOTLY_LAYOUT,
                        height=345,
                        title=(
                            f"{selected_symbol} — prix de courbe"
                        ),
                        showlegend=False,
                        xaxis=dict(showgrid=False),
                        yaxis=dict(
                            gridcolor="rgba(255,255,255,.055)",
                            tickformat=".12f",
                        ),
                    )
                    st.plotly_chart(
                        price_figure,
                        use_container_width=True,
                        config={"displayModeBar": False},
                    )

                scores_figure = go.Figure()
                scores_figure.add_trace(
                    go.Scatter(
                        x=samples["timestamp"],
                        y=samples["safety_score"],
                        mode="lines",
                        name="Safety",
                        line=dict(
                            color="#25e6a5",
                            width=2.3,
                        ),
                        hovertemplate="Safety %{y:.1f}<extra></extra>",
                    )
                )
                scores_figure.add_trace(
                    go.Scatter(
                        x=samples["timestamp"],
                        y=samples["qualification_score"],
                        mode="lines",
                        name="Qualification",
                        line=dict(
                            color="#ffbf69",
                            width=2.3,
                        ),
                        hovertemplate="Qualification %{y:.1f}<extra></extra>",
                    )
                )
                scores_figure.update_layout(
                    **PLOTLY_LAYOUT,
                    height=330,
                    title="Évolution des scores",
                    xaxis=dict(showgrid=False),
                    yaxis=dict(
                        gridcolor="rgba(255,255,255,.055)",
                        range=[0, 100],
                    ),
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.02,
                    ),
                )
                st.plotly_chart(
                    scores_figure,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

                st.markdown("### Journal événementiel")
                if events.empty:
                    st.info(
                        "Aucun changement d’état enregistré pour ce token."
                    )
                else:
                    st.dataframe(
                        events[
                            [
                                "timestamp",
                                "event_type",
                                "previous_value",
                                "new_value",
                                "source",
                                "details_json",
                            ]
                        ],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "timestamp": st.column_config.DatetimeColumn(
                                "Heure",
                                format="DD/MM HH:mm:ss",
                            ),
                            "event_type": "Événement",
                            "previous_value": "Avant",
                            "new_value": "Après",
                            "source": "Source",
                            "details_json": st.column_config.TextColumn(
                                "Détails",
                                width="large",
                            ),
                        },
                    )

        st.divider()
        st.markdown("### Lancer une simulation historique")

        defaults = default_parameters()

        with st.form("backtest_form_v9"):
            left, middle, right = st.columns(3)

            with left:
                test_name = st.text_input(
                    "Nom du test",
                    value=(
                        "Paper Pilot V12 "
                        + datetime.now().strftime("%d-%m %H:%M")
                    ),
                )
                initial_capital = st.number_input(
                    "Capital initial (SOL)",
                    min_value=0.1,
                    max_value=100.0,
                    value=float(
                        defaults["initial_capital_sol"]
                    ),
                    step=0.1,
                )
                position_size = st.number_input(
                    "Taille d’une position (SOL)",
                    min_value=0.001,
                    max_value=1.0,
                    value=float(
                        defaults["position_size_sol"]
                    ),
                    step=0.005,
                    format="%.3f",
                )
                min_safety = st.slider(
                    "Safety minimum",
                    min_value=0,
                    max_value=100,
                    value=int(
                        defaults["min_safety_score"]
                    ),
                )

            with middle:
                observation_seconds = st.number_input(
                    "Observation minimale (secondes)",
                    min_value=0,
                    max_value=3600,
                    value=int(
                        defaults["observation_seconds"]
                    ),
                    step=15,
                )
                min_samples = st.number_input(
                    "Échantillons minimum",
                    min_value=1,
                    max_value=100,
                    value=int(defaults["min_samples"]),
                    step=1,
                )
                min_progress = st.number_input(
                    "Bonding minimum (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(
                        defaults["min_progress_pct"]
                    ),
                    step=1.0,
                )
                max_progress = st.number_input(
                    "Bonding maximum (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(
                        defaults["max_progress_pct"]
                    ),
                    step=1.0,
                )

            with right:
                stop_loss = st.number_input(
                    "Stop-loss (%)",
                    min_value=-99.0,
                    max_value=-0.1,
                    value=float(defaults["stop_loss_pct"]),
                    step=1.0,
                )
                take_profit = st.number_input(
                    "Take-profit (%)",
                    min_value=0.1,
                    max_value=1000.0,
                    value=float(
                        defaults["take_profit_pct"]
                    ),
                    step=1.0,
                )
                max_hold = st.number_input(
                    "Durée maximale (minutes)",
                    min_value=1,
                    max_value=1440,
                    value=int(
                        defaults["max_holding_minutes"]
                    ),
                    step=1,
                )
                min_delta = st.number_input(
                    "Progression minimale Δ (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(
                        defaults["min_progress_delta_pct"]
                    ),
                    step=0.1,
                    format="%.2f",
                )

            submitted = st.form_submit_button(
                "Lancer le backtest",
                use_container_width=True,
            )

        if submitted:
            parameters = {
                "initial_capital_sol": initial_capital,
                "position_size_sol": position_size,
                "observation_seconds": observation_seconds,
                "min_samples": min_samples,
                "min_safety_score": min_safety,
                "min_progress_pct": min_progress,
                "max_progress_pct": max_progress,
                "min_progress_delta_pct": min_delta,
                "stop_loss_pct": stop_loss,
                "take_profit_pct": take_profit,
                "max_holding_minutes": max_hold,
            }

            try:
                summary = run_backtest(
                    parameters=parameters,
                    name=test_name,
                    db_path=DB_PATH,
                    save=True,
                )
                st.success(
                    f"Backtest #{summary.run_id} terminé : "
                    f"{summary.trade_count} trade(s), "
                    f"{summary.pnl_sol:+.4f} SOL."
                )
            except Exception as error:
                st.error(f"Échec du backtest : {error}")

        latest_runs = query(
            """
            SELECT *
            FROM backtest_runs
            ORDER BY id DESC
            LIMIT 30
            """
        )
        parse_dates(latest_runs, ["created_at"])

        if latest_runs.empty:
            st.info("Aucun backtest enregistré.")
        else:
            st.markdown("### Résultats enregistrés")
            st.dataframe(
                latest_runs[
                    [
                        "id",
                        "created_at",
                        "name",
                        "sample_count",
                        "candidate_count",
                        "trade_count",
                        "win_rate",
                        "pnl_sol",
                        "return_pct",
                        "max_drawdown_sol",
                        "ending_equity_sol",
                        "status",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "id": "Run",
                    "created_at": st.column_config.DatetimeColumn(
                        "Date",
                        format="DD/MM HH:mm:ss",
                    ),
                    "name": "Nom",
                    "sample_count": "Échantillons",
                    "candidate_count": "Candidats",
                    "trade_count": "Trades",
                    "win_rate": st.column_config.NumberColumn(
                        "Win rate",
                        format="%.1f %%",
                    ),
                    "pnl_sol": st.column_config.NumberColumn(
                        "PnL",
                        format="%+.4f SOL",
                    ),
                    "return_pct": st.column_config.NumberColumn(
                        "Rendement",
                        format="%+.2f %%",
                    ),
                    "max_drawdown_sol": st.column_config.NumberColumn(
                        "Drawdown",
                        format="%.4f SOL",
                    ),
                    "ending_equity_sol": st.column_config.NumberColumn(
                        "Équité finale",
                        format="%.4f SOL",
                    ),
                    "status": "État",
                },
            )

            selected_run_id = st.selectbox(
                "Détails d’un backtest",
                latest_runs["id"].tolist(),
                format_func=lambda run_id: (
                    f"#{run_id} — "
                    f"{latest_runs.loc[latest_runs['id'] == run_id, 'name'].iloc[0]}"
                ),
                key="backtest_run_selector",
            )
            run_trades = query(
                """
                SELECT *
                FROM backtest_trades
                WHERE run_id = ?
                ORDER BY datetime(entry_at)
                """,
                (int(selected_run_id),),
            )
            parse_dates(run_trades, ["entry_at", "exit_at"])

            if run_trades.empty:
                st.info(
                    "Ce scénario n’a trouvé aucune entrée conforme."
                )
            else:
                st.dataframe(
                    run_trades[
                        [
                            "symbol",
                            "entry_at",
                            "exit_at",
                            "entry_progress_pct",
                            "exit_progress_pct",
                            "entry_safety_score",
                            "entry_qualification_score",
                            "pnl_sol",
                            "pnl_pct",
                            "exit_reason",
                            "holding_seconds",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "symbol": "Token",
                        "entry_at": st.column_config.DatetimeColumn(
                            "Entrée",
                            format="DD/MM HH:mm:ss",
                        ),
                        "exit_at": st.column_config.DatetimeColumn(
                            "Sortie",
                            format="DD/MM HH:mm:ss",
                        ),
                        "entry_progress_pct": st.column_config.NumberColumn(
                            "Bonding entrée",
                            format="%.2f %%",
                        ),
                        "exit_progress_pct": st.column_config.NumberColumn(
                            "Bonding sortie",
                            format="%.2f %%",
                        ),
                        "entry_safety_score": st.column_config.NumberColumn(
                            "Safety",
                            format="%.1f",
                        ),
                        "entry_qualification_score": st.column_config.NumberColumn(
                            "Qualification",
                            format="%.1f",
                        ),
                        "pnl_sol": st.column_config.NumberColumn(
                            "PnL",
                            format="%+.4f SOL",
                        ),
                        "pnl_pct": st.column_config.NumberColumn(
                            "PnL %",
                            format="%+.2f %%",
                        ),
                        "exit_reason": "Sortie",
                        "holding_seconds": st.column_config.NumberColumn(
                            "Durée",
                            format="%.0f s",
                        ),
                    },
                )

        st.caption(
            "Le backtest V12 utilise des observations périodiques de la "
            "bonding curve. Il inclut une approximation des frais, mais ne "
            "reconstruit pas chaque transaction ni la latence réelle."
        )

    elif selected_page == "Pré-bond":
        st.markdown(
            '<div class="section-head"><div class="section-title">Bonding curves Pump.fun</div>'
            '<div class="section-note">Lecture directe des comptes PDA du programme Pump</div></div>',
            unsafe_allow_html=True,
        )
        if latest_bonding.empty:
            st.warning("Aucune donnée on-chain. Vérifie la fenêtre du collecteur.")
        else:
            st.dataframe(
                latest_bonding[
                    [
                        "symbol", "token_name", "lifecycle_state",
                        "progress_pct", "curve_price_sol",
                        "curve_price_usd", "real_quote_reserves_sol",
                        "real_token_reserves_raw", "complete",
                        "is_mayhem_mode", "bonding_curve_address",
                        "rpc_status",
                    ]
                ].sort_values("progress_pct", ascending=False),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "symbol": "Ticker",
                    "token_name": "Nom",
                    "lifecycle_state": "État",
                    "progress_pct": st.column_config.ProgressColumn(
                        "Bonding",
                        min_value=0,
                        max_value=100,
                        format="%.2f %%",
                    ),
                    "curve_price_sol": st.column_config.NumberColumn(
                        "Prix SOL",
                        format="%.12f",
                    ),
                    "curve_price_usd": st.column_config.NumberColumn(
                        "Prix USD",
                        format="$ %.10f",
                    ),
                    "real_quote_reserves_sol": st.column_config.NumberColumn(
                        "SOL réel courbe",
                        format="%.4f",
                    ),
                    "real_token_reserves_raw": st.column_config.NumberColumn(
                        "Tokens restants bruts",
                        format="%d",
                    ),
                    "complete": "Complète",
                    "is_mayhem_mode": "Mayhem",
                    "bonding_curve_address": st.column_config.TextColumn(
                        "PDA",
                        width="large",
                    ),
                    "rpc_status": "Décodage",
                },
            )

            choices = latest_bonding["token_mint"].tolist()
            selected_mint = st.selectbox(
                "Courbe à analyser",
                choices,
                format_func=lambda mint: (
                    f"{latest_bonding.loc[latest_bonding['token_mint'] == mint, 'symbol'].iloc[0]}"
                    f" — {mint[:8]}…{mint[-6:]}"
                ),
                key="bonding_chart",
            )
            history = (
                bonding[bonding["token_mint"] == selected_mint]
                .sort_values("timestamp")
                .tail(1200)
            )

            left, right = st.columns(2)
            with left:
                progress_fig = go.Figure(
                    go.Scatter(
                        x=history["timestamp"],
                        y=history["progress_pct"],
                        mode="lines",
                        line=dict(color="#9b7cff", width=2.5),
                        hovertemplate="%{y:.2f}%<extra></extra>",
                    )
                )
                progress_fig.update_layout(
                    **PLOTLY_LAYOUT,
                    height=350,
                    title="Progression vers la fin de courbe",
                    showlegend=False,
                    xaxis=dict(showgrid=False),
                    yaxis=dict(
                        gridcolor="rgba(255,255,255,.055)",
                        range=[0, 100],
                        ticksuffix=" %",
                    ),
                )
                st.plotly_chart(
                    progress_fig,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

            with right:
                price_history = history[history["curve_price_usd"].notna()]
                price_fig = go.Figure(
                    go.Scatter(
                        x=price_history["timestamp"],
                        y=price_history["curve_price_usd"],
                        mode="lines",
                        line=dict(color="#32d7ff", width=2.5),
                        hovertemplate="$%{y:.10f}<extra></extra>",
                    )
                )
                price_fig.update_layout(
                    **PLOTLY_LAYOUT,
                    height=350,
                    title="Prix calculé depuis les réserves on-chain",
                    showlegend=False,
                    xaxis=dict(showgrid=False),
                    yaxis=dict(
                        gridcolor="rgba(255,255,255,.055)",
                        tickformat=".10f",
                    ),
                )
                st.plotly_chart(
                    price_fig,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

            st.caption(
                "BONDING = courbe active • GRADUATING = courbe complète sans marché DEX "
                "confirmé • BONDED = courbe complète et paire DEX détectée."
            )

    elif selected_page == "Scanner hybride":
        if combined.empty:
            st.info("En attente des données.")
        else:
            display = combined.copy()
            display["prix_actif_usd"] = display["curve_price_usd"].where(
                display["lifecycle_state"] == "BONDING",
                display["price_usd"],
            )
            st.dataframe(
                display[
                    [
                        "symbol", "token_name", "lifecycle_state",
                        "progress_pct", "prix_actif_usd",
                        "change_5m_pct", "liquidity_usd",
                        "volume_5m_usd", "buys_5m", "sells_5m",
                        "score", "rpc_status", "data_status",
                        "source_url",
                    ]
                ].sort_values("score", ascending=False),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "symbol": "Ticker",
                    "token_name": "Nom",
                    "lifecycle_state": "Cycle",
                    "progress_pct": st.column_config.ProgressColumn(
                        "Bonding",
                        min_value=0,
                        max_value=100,
                        format="%.1f %%",
                    ),
                    "prix_actif_usd": st.column_config.NumberColumn(
                        "Prix actif",
                        format="$ %.10f",
                    ),
                    "change_5m_pct": st.column_config.NumberColumn(
                        "5 min",
                        format="%+.2f %%",
                    ),
                    "liquidity_usd": st.column_config.NumberColumn(
                        "Liquidité DEX",
                        format="$ %.0f",
                    ),
                    "volume_5m_usd": st.column_config.NumberColumn(
                        "Volume 5 min",
                        format="$ %.0f",
                    ),
                    "buys_5m": "Achats",
                    "sells_5m": "Ventes",
                    "score": st.column_config.ProgressColumn(
                        "Score recherche",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "rpc_status": "RPC",
                    "data_status": "DEX",
                    "source_url": st.column_config.LinkColumn(
                        "Marché",
                        display_text="Ouvrir",
                    ),
                },
            )

    elif selected_page == "Positions":
        if opened.empty:
            st.info("Aucune position paper ouverte.")
        else:
            view = opened.copy()
            view["pnl_latent_sol"] = (
                view["current_value_sol"]
                - view["entry_sol"]
                - view["entry_fees_sol"]
            )
            view["pnl_latent_pct"] = (
                view["pnl_latent_sol"]
                / (view["entry_sol"] + view["entry_fees_sol"])
                * 100
            )
            st.dataframe(
                view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "opened_at": st.column_config.DatetimeColumn(
                        "Ouverture",
                        format="DD/MM HH:mm:ss",
                    ),
                    "entry_sol": st.column_config.NumberColumn(
                        "Entrée",
                        format="%.4f SOL",
                    ),
                    "current_value_sol": st.column_config.NumberColumn(
                        "Valeur",
                        format="%.4f SOL",
                    ),
                    "pnl_latent_sol": st.column_config.NumberColumn(
                        "PnL",
                        format="%+.4f SOL",
                    ),
                    "pnl_latent_pct": st.column_config.NumberColumn(
                        "PnL %",
                        format="%+.2f %%",
                    ),
                    "peak_pnl_pct": st.column_config.NumberColumn(
                        "Pic PnL",
                        format="%+.2f %%",
                    ),
                    "break_even_armed": st.column_config.CheckboxColumn(
                        "Break-even"
                    ),
                    "active_stop_pct": st.column_config.NumberColumn(
                        "Stop actif",
                        format="%+.2f %%",
                    ),
                    "entry_bonding_progress_pct": st.column_config.NumberColumn(
                        "Bonding entrée",
                        format="%.1f %%",
                    ),
                },
            )

    elif selected_page == "Historique":
        st.dataframe(
            positions,
            use_container_width=True,
            hide_index=True,
            column_config={
                "opened_at": st.column_config.DatetimeColumn(
                    "Ouverture",
                    format="DD/MM HH:mm:ss",
                ),
                "closed_at": st.column_config.DatetimeColumn(
                    "Fermeture",
                    format="DD/MM HH:mm:ss",
                ),
                "entry_sol": st.column_config.NumberColumn(
                    "Entrée",
                    format="%.4f SOL",
                ),
                "exit_sol": st.column_config.NumberColumn(
                    "Sortie",
                    format="%.4f SOL",
                ),
                "realized_pnl_sol": st.column_config.NumberColumn(
                    "PnL",
                    format="%+.4f SOL",
                ),
                "realized_pnl_pct": st.column_config.NumberColumn(
                    "PnL %",
                    format="%+.2f %%",
                ),
                "source_url": st.column_config.LinkColumn(
                    "Marché",
                    display_text="Ouvrir",
                ),
            },
        )
        st.download_button(
            "Exporter les trades",
            positions.to_csv(index=False).encode("utf-8"),
            "solpulse_prebond_trades.csv",
            "text/csv",
        )

    elif selected_page == "Signaux":
        if signals.empty:
            st.info("Aucun signal paper.")
        else:
            view = signals.copy()
            def decode_reasons(raw: str | None) -> str:
                try:
                    return " • ".join(json.loads(raw)) if raw else ""
                except Exception:
                    return str(raw or "")
            view["raisons"] = view["reasons_json"].apply(decode_reasons)
            st.dataframe(
                view[
                    [
                        "timestamp", "symbol", "lifecycle_state",
                        "decision", "score", "strategy", "raisons",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "timestamp": st.column_config.DatetimeColumn(
                        "Heure",
                        format="DD/MM HH:mm:ss",
                    ),
                    "symbol": "Token",
                    "lifecycle_state": "Cycle",
                    "decision": "Décision",
                    "score": st.column_config.ProgressColumn(
                        "Score",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    ),
                    "strategy": "Stratégie",
                    "raisons": st.column_config.TextColumn(
                        "Raisons",
                        width="large",
                    ),
                },
            )

    elif selected_page == "Analytics":
        if closed.empty:
            st.info("Attends la clôture d’au moins un trade paper.")
        else:
            a, b, c, d = st.columns(4)
            a.metric("Trades", len(closed))
            b.metric(
                "Gain moyen",
                fmt_sol(wins["realized_pnl_sol"].mean() if len(wins) else 0),
            )
            c.metric(
                "Perte moyenne",
                fmt_sol(losses["realized_pnl_sol"].mean() if len(losses) else 0),
            )
            d.metric("Espérance", fmt_sol(closed["realized_pnl_sol"].mean()))

            pnl = closed.sort_values("closed_at").copy()
            pnl["trade"] = range(1, len(pnl) + 1)
            fig = go.Figure(
                go.Bar(
                    x=pnl["trade"],
                    y=pnl["realized_pnl_sol"],
                    marker=dict(
                        color=[
                            "#25e6a5" if value >= 0 else "#ff5c78"
                            for value in pnl["realized_pnl_sol"]
                        ]
                    ),
                    hovertemplate="Trade %{x}<br>%{y:+.4f} SOL<extra></extra>",
                )
            )
            fig.update_layout(
                **PLOTLY_LAYOUT,
                height=390,
                title="PnL par trade",
                xaxis=dict(showgrid=False),
                yaxis=dict(
                    gridcolor="rgba(255,255,255,.055)",
                    title="SOL",
                ),
            )
            st.plotly_chart(
                fig,
                use_container_width=True,
                config={"displayModeBar": False},
            )

    elif selected_page == "Exécution":
        if orders.empty:
            st.info("Aucun ordre paper.")
        else:
            filled = orders[orders["status"] == "FILLED"]
            a, b, c, d = st.columns(4)
            a.metric("Ordres", len(orders))
            a_value = (
                filled["extra_slippage_pct"].mean()
                if len(filled)
                else 0
            )
            b.metric("Coût moyen", f"{a_value:.3f} %")
            c.metric(
                "Impact moyen",
                f"{filled['price_impact_pct'].mean():.3f} %"
                if len(filled)
                else "0 %",
            )
            d.metric(
                "Bonding / DEX",
                f"{(orders['market_mode'] == 'BONDING').sum()} / "
                f"{(orders['market_mode'] == 'DEX').sum()}",
            )
            st.dataframe(
                orders,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "timestamp": st.column_config.DatetimeColumn(
                        "Heure",
                        format="DD/MM HH:mm:ss",
                    ),
                    "requested_sol": st.column_config.NumberColumn(
                        "Demandé",
                        format="%.4f SOL",
                    ),
                    "price_impact_pct": st.column_config.NumberColumn(
                        "Impact",
                        format="%.3f %%",
                    ),
                    "extra_slippage_pct": st.column_config.NumberColumn(
                        "Coût",
                        format="%.3f %%",
                    ),
                },
            )

    elif selected_page == "Watchlist":
        watch_df = pd.DataFrame(watchlist["tokens"])
        if not combined.empty:
            metadata = combined[
                [
                    "token_mint", "symbol", "token_name",
                    "lifecycle_state", "bonding_curve_address",
                    "rpc_status", "source_url",
                ]
            ]
            watch_df = watch_df.merge(
                metadata,
                left_on="address",
                right_on="token_mint",
                how="left",
            )
        st.dataframe(
            watch_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "address": st.column_config.TextColumn(
                    "Mint",
                    width="large",
                ),
                "label": "Nom provisoire",
                "symbol": "Ticker",
                "token_name": "Nom",
                "lifecycle_state": "Cycle",
                "bonding_curve_address": st.column_config.TextColumn(
                    "PDA bonding curve",
                    width="large",
                ),
                "rpc_status": "RPC",
                "source_url": st.column_config.LinkColumn(
                    "DEX",
                    display_text="Ouvrir",
                ),
            },
        )
        st.caption(
            "Cette version vérifie les cinq contrats de la watchlist. "
            "La découverte automatique de tous les nouveaux launches viendra ensuite."
        )


if not DB_PATH.exists():
    st.error("Base absente. Lance 02_REINITIALISER_1_SOL.bat.")
else:
    render_live(page)
