"""Trading Bot Dashboard — sidebar-nav Streamlit UI.

Run with:  streamlit run dashboard.py
or double-click dashboard.bat.
"""
from __future__ import annotations

import json
import statistics as _stats
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from src.utils.config import load_config
from src.utils.llm_client import llm_ping
from src.learning import (
    load_rules, set_rule_active, delete_rule,
    all_ticker_track_records,
    effective_weights, load_weight_history,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Trading Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Color constants
# ---------------------------------------------------------------------------
C_BUY   = "#3fb950"
C_SELL  = "#f85149"
C_INFO  = "#58a6ff"
C_WARN  = "#e3b341"
C_MUTED = "#8b949e"
GRADE_COLORS = {
    "A+": "#3fb950", "A": "#56d364", "B": "#e3b341",
    "C": "#8b949e",  "D": "#f0883e", "F": "#f85149",
}

# ---------------------------------------------------------------------------
# Custom CSS
# ---------------------------------------------------------------------------
st.markdown("""
<style>
/* Metric card background */
div[data-testid="metric-container"] {
    background-color: #0e1117;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 14px 18px;
}
/* Section sub-headers */
.section-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: #8b949e;
    margin-bottom: 6px;
}
/* Grade badges */
.badge {
    display: inline-block;
    padding: 2px 9px;
    border-radius: 5px;
    font-weight: 700;
    font-size: 12px;
    font-family: monospace;
}
.g-ap { background:#0d2e1a; color:#3fb950; border:1px solid #238636; }
.g-a  { background:#0d2e1a; color:#3fb950; border:1px solid #238636; }
.g-b  { background:#1a2e0d; color:#7ee787; border:1px solid #2ea043; }
.g-c  { background:#2e2200; color:#d29922; border:1px solid #9e6a03; }
.g-d  { background:#2e1600; color:#f0883e; border:1px solid #bd561d; }
.g-f  { background:#2e0d0d; color:#f85149; border:1px solid #da3633; }
/* Status pills */
.pill {
    display: inline-block;
    padding: 3px 11px;
    border-radius: 20px;
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.5px;
}
.p-green  { background:#0d2e1a; color:#3fb950; border:1px solid #238636; }
.p-red    { background:#2e0d0d; color:#f85149; border:1px solid #da3633; }
.p-yellow { background:#2e2200; color:#d29922; border:1px solid #9e6a03; }
.p-gray   { background:#161b22; color:#8b949e; border:1px solid #30363d; }
.p-blue   { background:#0d1f2e; color:#58a6ff; border:1px solid #1f6feb; }
/* Regime banner */
.regime-bull { background:#0d2e1a; border-left:4px solid #3fb950;
               padding:10px 16px; border-radius:4px; color:#3fb950; }
.regime-bear { background:#2e0d0d; border-left:4px solid #f85149;
               padding:10px 16px; border-radius:4px; color:#f85149; }
.regime-vol  { background:#2e2200; border-left:4px solid #d29922;
               padding:10px 16px; border-radius:4px; color:#d29922; }
.regime-neut { background:#161b22; border-left:4px solid #8b949e;
               padding:10px 16px; border-radius:4px; color:#8b949e; }
/* Action badges */
.act-buy   { color:#3fb950; font-weight:700; }
.act-close { color:#f85149; font-weight:700; }
.act-hold  { color:#8b949e; }
.act-stop  { color:#f0883e; font-weight:700; }
.act-tp    { color:#7ee787; font-weight:700; }
.act-flat  { color:#d29922; font-weight:700; }
/* Sidebar nav */
div[data-testid="stSidebar"] .stRadio > div { gap: 2px; }
/* Gate pills in table */
.gate-pass { background:#0d2e1a; color:#3fb950; border:1px solid #238636; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:600; }
.gate-block { background:#2e0d0d; color:#f85149; border:1px solid #da3633; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:600; }
.gate-na    { background:#161b22; color:#8b949e; border:1px solid #30363d; padding:2px 7px; border-radius:4px; font-size:11px; }
/* Run history card */
.run-card { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:12px 16px; margin-bottom:8px; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------
cfg = load_config()
MODE = cfg["broker"]["mode"]
DATA = Path(cfg["paths"]["data_dir"])
ARCHIVE = DATA / "archive"
STATE_FILE     = Path(cfg["paths"]["state_file"])
SHORTLIST_FILE = Path(cfg["paths"]["shortlist_file"])
LESSONS_FILE   = Path(cfg["paths"]["lessons_file"])

# Archive files
DECISIONS_MASTER    = ARCHIVE / "decisions_master.jsonl"
BT_DECISIONS_MASTER = ARCHIVE / "backtest_decisions_master.jsonl"
OUTCOMES_MASTER     = ARCHIVE / "outcomes_master.jsonl"
IND_OUTCOMES_MASTER = ARCHIVE / "indicator_outcomes_master.jsonl"
IND_STATS_HISTORY   = ARCHIVE / "indicator_stats_history.jsonl"
LESSONS_MASTER      = ARCHIVE / "lessons_master.md"

# Backtest files
BT_RESULTS_FILE   = DATA / "backtest_results.json"
BT_HISTORY_FILE   = DATA / "backtest_history.json"
BT_LESSONS_FILE   = DATA / "backtest_lessons.md"
BT_POSTMORTEMS    = DATA / "backtest_postmortems.jsonl"
BT_DECISIONS_FILE = DATA / "backtest_decisions.jsonl"

# Other
TRADE_SCORES_FILE = DATA / "trade_scores.json"
INDICATOR_STATS   = DATA / "indicator_stats.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_GRADE_CSS = {"A+": "g-ap", "A": "g-a", "B": "g-b", "C": "g-c", "D": "g-d", "F": "g-f"}

def grade_badge(g: str) -> str:
    css = _GRADE_CSS.get(str(g).upper(), "g-c")
    return f'<span class="badge {css}">{g}</span>'

def signal_badge(s: str) -> str:
    s = str(s).lower()
    css = ("p-green" if "buy" in s else
           "p-red" if "avoid" in s else
           "p-yellow" if "caution" in s else "p-gray")
    return f'<span class="pill {css}">{s}</span>'

def action_html(a: str) -> str:
    a = str(a).upper()
    if a == "BUY":   return '<span class="act-buy">&#9650; BUY</span>'
    if a == "CLOSE": return '<span class="act-close">&#9660; CLOSE</span>'
    if a == "STOP_LOSS": return '<span class="act-stop">&#9632; STOP</span>'
    if a == "TAKE_PROFIT": return '<span class="act-tp">&#9650; PROFIT</span>'
    if a == "FLATTEN_ALL": return '<span class="act-flat">&#9644; FLATTEN</span>'
    return f'<span class="act-hold">&#8212; {a}</span>'

def fmt_pnl(v: float) -> str:
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

def _gate_pill(status: str) -> str:
    if status == "pass":  return '<span class="gate-pass">PASS</span>'
    if status == "block": return '<span class="gate-block">BLOCK</span>'
    return '<span class="gate-na">N/A</span>'

def _pct_color(v: float) -> str:
    return C_BUY if v >= 0 else C_SELL

def _plotly_dark_layout(**kwargs) -> dict:
    base = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c9d1d9", size=12),
        xaxis=dict(showgrid=False, zeroline=False, color="#8b949e"),
        yaxis=dict(showgrid=True, gridcolor="#21262d", zeroline=False, color="#8b949e"),
        hovermode="x unified",
        margin=dict(l=0, r=0, t=10, b=0),
    )
    base.update(kwargs)
    return base

def _plotly_line(df: pd.DataFrame, x_col: str, y_col: str,
                 color: str = "#58a6ff", label: str = "") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df[x_col], y=df[y_col],
        mode="lines", name=label,
        line=dict(color=color, width=2),
        fill="tozeroy",
        fillcolor="rgba(88,166,255,0.06)",
    ))
    fig.update_layout(**_plotly_dark_layout(showlegend=False))
    return fig

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30)
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"cash": 0, "positions": {}, "orders": []}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cash": 0, "positions": {}, "orders": []}

@st.cache_data(ttl=60)
def load_shortlist() -> dict:
    if not SHORTLIST_FILE.exists():
        return {"date": "-", "symbols": []}
    try:
        with open(SHORTLIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "-", "symbols": []}

@st.cache_data(ttl=60)
def load_deep_scores() -> dict:
    if not TRADE_SCORES_FILE.exists():
        return {}
    try:
        return json.loads(TRADE_SCORES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

@st.cache_data(ttl=60)
def load_indicator_stats() -> dict:
    if not INDICATOR_STATS.exists():
        return {}
    try:
        return json.loads(INDICATOR_STATS.read_text(encoding="utf-8"))
    except Exception:
        return {}

@st.cache_data(ttl=15)
def check_llm() -> dict:
    try:
        return llm_ping(timeout=3.0)
    except Exception as e:
        return {"ok": False, "provider": "unknown", "model": None,
                "latency_ms": None, "error": str(e)[:200]}

@st.cache_data(ttl=60)
def get_current_price(symbol: str) -> float | None:
    try:
        h = yf.Ticker(symbol).history(period="1d", interval="1m", auto_adjust=False)
        if h.empty:
            h = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
        return float(h["Close"].iloc[-1]) if not h.empty else None
    except Exception:
        return None

@st.cache_data(ttl=300)
def load_decisions_master(source: str = "live") -> list:
    path = DECISIONS_MASTER if source == "live" else BT_DECISIONS_MASTER
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    rows.sort(key=lambda r: r.get("date", "") + r.get("timestamp", ""), reverse=True)
    return rows

@st.cache_data(ttl=300)
def load_backtest_history() -> list:
    if not BT_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(BT_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return sorted(data, key=lambda r: r.get("date", ""), reverse=True)
        return []
    except Exception:
        return []

@st.cache_data(ttl=60)
def load_backtest_results(results_file: str) -> dict:
    if not results_file:
        return {}
    p = Path(results_file)
    if not p.exists():
        p = DATA / results_file
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

@st.cache_data(ttl=300)
def load_lessons_master() -> str:
    if not LESSONS_MASTER.exists():
        return ""
    try:
        return LESSONS_MASTER.read_text(encoding="utf-8")
    except Exception:
        return ""

@st.cache_data(ttl=300)
def load_backtest_lessons() -> str:
    if not BT_LESSONS_FILE.exists():
        return ""
    try:
        return BT_LESSONS_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""

@st.cache_data(ttl=300)
def load_backtest_postmortems() -> list:
    if not BT_POSTMORTEMS.exists():
        return []
    rows = []
    try:
        with open(BT_POSTMORTEMS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows

@st.cache_data(ttl=300)
def load_ind_stats_history() -> list:
    if not IND_STATS_HISTORY.exists():
        return []
    rows = []
    try:
        with open(IND_STATS_HISTORY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows

@st.cache_data(ttl=300)
def load_rules_cached() -> list:
    try:
        return load_rules()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# INDICATOR STATS HELPERS
# ---------------------------------------------------------------------------

_IND_SCORE_KEYS = {
    "RSI":   "rsi_score",
    "MACD":  "macd_score",
    "Trend": "trend_score",
    "BB":    "bb_score",
    "OBV":   "obv_score",
    "VWAP":  "vwap_score",
    "Fib":   "fib_score",
}

def _pearson_simple(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    dx  = sum((x-mx)**2 for x in xs) ** 0.5
    dy  = sum((y-my)**2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx*dy), 4)


def compute_bt_indicator_stats(rows: list) -> dict:
    """Compute per-indicator effectiveness stats from backtest decision rows.

    Uses fwd_5d_return as the outcome. Covers BUY, HOLD, and SELL decisions.
    Returns a dict keyed by indicator display name.
    """
    stats = {}
    for ind_name, score_key in _IND_SCORE_KEYS.items():
        valid = [
            r for r in rows
            if r.get(score_key) is not None and r.get("fwd_5d_return") is not None
        ]
        if not valid:
            stats[ind_name] = None
            continue

        scores  = [float(r[score_key]) for r in valid]
        returns = [float(r["fwd_5d_return"]) for r in valid]
        n = len(valid)

        # Overall win = fwd_5d_return > 0
        wins = sum(1 for ret in returns if ret > 0)
        win_rate = wins / n
        avg_ret  = sum(returns) / n

        # High vs low signal: 0.5 is neutral on the [0, 1] normalized scale
        high = [(s, r) for s, r in zip(scores, returns) if s >= 0.5]
        low  = [(s, r) for s, r in zip(scores, returns) if s < 0.5]

        def _sub(pairs):
            if not pairs:
                return {"n": 0, "win_rate": None, "avg_ret": None}
            rets = [r for _, r in pairs]
            return {
                "n":        len(pairs),
                "win_rate": sum(1 for r in rets if r > 0) / len(pairs),
                "avg_ret":  sum(rets) / len(pairs),
            }

        # By action
        by_action = {}
        for action in ("BUY", "SELL", "HOLD"):
            a_rows = [r for r in valid if r.get("action") == action]
            if a_rows:
                a_rets = [float(r["fwd_5d_return"]) for r in a_rows]
                a_wins = sum(1 for r in a_rets if r > 0)
                by_action[action] = {
                    "n":        len(a_rows),
                    "win_rate": a_wins / len(a_rows),
                    "avg_ret":  sum(a_rets) / len(a_rows),
                }

        corr = _pearson_simple(scores, returns)
        stats[ind_name] = {
            "n":        n,
            "win_rate": win_rate,
            "avg_ret":  avg_ret,
            "wins":     wins,
            "losses":   n - wins,
            "corr":     corr,
            "high":     _sub(high),
            "low":      _sub(low),
            "by_action": by_action,
            "scores":   scores,
            "returns":  returns,
        }
    return stats


# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## Trading Bot")
    st.divider()

    mode = st.radio(
        "", ["Live Bot", "Backtester"],
        key="mode_radio", horizontal=True,
        label_visibility="collapsed",
    )
    is_live = mode == "Live Bot"

    st.divider()

    if is_live:
        live_pages = [
            "Home", "Positions & Orders", "Signals & Gates",
            "Decisions Log", "Deep Scores", "Indicators",
            "Rules & Learning", "Lessons & Reflections",
        ]
        page = st.radio("Navigate", live_pages, key="live_page",
                        label_visibility="collapsed")
    else:
        bt_pages = [
            "Run History", "Equity Curve", "Trade Journal",
            "Signals & Gates", "Decisions Log", "Deep Scores",
            "Indicators", "Lessons",
        ]
        page = st.radio("Navigate", bt_pages, key="bt_page",
                        label_visibility="collapsed")

    st.divider()

    # Backtest run selector (only in backtest mode)
    _sel_run = None
    _bt_data: dict = {}
    _sel_run_id = ""
    if not is_live:
        _bt_history = load_backtest_history()
        if _bt_history:
            _run_ids = [r.get("run_id", f"run_{i}") for i, r in enumerate(_bt_history)]
            _sel_run_id = st.selectbox("Run", _run_ids, index=0, key="sel_run")
            _sel_run = next((r for r in _bt_history if r.get("run_id") == _sel_run_id),
                            _bt_history[0])
            _bt_data = load_backtest_results(_sel_run.get("results_file", ""))
        else:
            # Fall back to default results file
            _bt_data = load_backtest_results(str(BT_RESULTS_FILE))

    st.divider()

    # LLM status
    _llm = check_llm()
    if _llm.get("ok"):
        st.markdown(
            f'<span class="pill p-green">LLM ONLINE</span> <code>{(_llm.get("model") or "")[:20]}</code>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<span class="pill p-red">LLM OFFLINE</span>', unsafe_allow_html=True)

    st.write("")
    if st.button("Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")

    st.divider()
    _mode_info = {
        "sim":          ("SIM",   "p-gray"),
        "alpaca_paper": ("PAPER", "p-blue"),
        "alpaca_live":  ("LIVE",  "p-red"),
    }
    _ml, _mc = _mode_info.get(MODE, ("?", "p-gray"))
    st.markdown(f'<span class="pill {_mc}">{_ml}</span>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Backtest auto-reload watchdog (fragment)
# ---------------------------------------------------------------------------
@st.fragment(run_every=10)
def _bt_watchdog():
    if BT_RESULTS_FILE.exists():
        mtime = BT_RESULTS_FILE.stat().st_mtime
        last = st.session_state.get("_bt_mtime", 0)
        if mtime != last:
            st.session_state["_bt_mtime"] = mtime
            if last != 0:
                st.cache_data.clear()
                st.rerun()

if not is_live:
    _bt_watchdog()

# ---------------------------------------------------------------------------
# Live positions fragment (auto-refresh every 5 min)
# ---------------------------------------------------------------------------
@st.fragment(run_every=300)
def _live_positions_panel(mini: bool = False) -> None:
    _state = load_state()
    cash      = float(_state.get("cash", 0))
    positions = _state.get("positions", {})

    pos_rows, total_mv, total_unrl = [], 0.0, 0.0
    for sym, p in positions.items():
        qty = float(p.get("qty", 0))
        if qty == 0:
            continue
        entry   = float(p.get("avg_entry", 0))
        current = get_current_price(sym)
        if current is None:
            continue
        mv   = qty * current
        unrl = (current - entry) * qty
        pct  = (current / entry - 1) if entry else 0
        stop   = p.get("stop_loss")
        target = p.get("take_profit")
        total_mv   += mv
        total_unrl += unrl
        tags = p.get("tags") or {}
        pos_rows.append({
            "Symbol":    sym,
            "Qty":       qty,
            "Avg Entry": entry,
            "Current":   current,
            "Return":    pct,
            "Unrl P&L":  unrl,
            "Mkt Value": mv,
            "Stop":      stop,
            "to Stop":   ((stop / current - 1) if (stop and current) else None),
            "Target":    target,
            "to Tgt":    ((target / current - 1) if (target and current) else None),
            "Quality":   tags.get("quality", ""),
            "Trend":     tags.get("trend", ""),
        })

    equity    = cash + total_mv
    starting  = 100_000.0 if MODE == "sim" else equity
    total_ret = equity - starting
    total_ret_pct = (equity / starting - 1) if starting else 0

    if mini:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Equity",    f"${equity:,.2f}")
        m2.metric("Cash",      f"${cash:,.2f}")
        m3.metric("Open P&L",  fmt_pnl(total_unrl))
        m4.metric("Positions", len(pos_rows))
        return

    st.markdown('<div class="section-label">Account</div>', unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Equity",         f"${equity:,.2f}",
              f"{total_ret_pct:+.2%}  (${total_ret:+,.0f})" if MODE == "sim" else None)
    m2.metric("Cash",           f"${cash:,.2f}")
    m3.metric("Open Positions", len(pos_rows),
              f"${total_mv:,.0f} market value" if pos_rows else "none")
    m4.metric("Unrealized P&L", fmt_pnl(total_unrl))

    st.write("")
    hdr_c, ts_c = st.columns([5, 3])
    hdr_c.markdown('<div class="section-label">Open Positions</div>', unsafe_allow_html=True)
    ts_c.caption(f"Auto-refreshes every 5 min  |  {datetime.now().strftime('%H:%M:%S')}")

    if pos_rows:
        df_pos = pd.DataFrame(pos_rows)
        st.dataframe(
            df_pos, use_container_width=True, hide_index=True,
            column_config={
                "Avg Entry": st.column_config.NumberColumn(format="$%.2f"),
                "Current":   st.column_config.NumberColumn(format="$%.2f"),
                "Return":    st.column_config.NumberColumn(format="+%.2f%%"),
                "Unrl P&L":  st.column_config.NumberColumn(format="$+%.2f"),
                "Mkt Value": st.column_config.NumberColumn(format="$%.2f"),
                "Stop":      st.column_config.NumberColumn(format="$%.2f"),
                "to Stop":   st.column_config.NumberColumn(format="%.2f%%"),
                "Target":    st.column_config.NumberColumn(format="$%.2f"),
                "to Tgt":    st.column_config.NumberColumn(format="+%.2f%%"),
            },
        )
    else:
        st.caption("No open positions.")

# ===========================================================================
# Helper: compute backtest stats from _bt_data
# ===========================================================================
def _bt_stats(bt: dict) -> dict:
    trades = bt.get("trades", [])
    curve  = bt.get("equity_curve", [])
    start  = float(bt.get("starting_cash", 100_000))
    final  = float(curve[-1]["equity"]) if curve else start
    pnl    = final - start
    ret    = pnl / start if start else 0

    wins   = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    wr     = len(wins) / len(trades) if trades else 0

    # Sharpe
    rets, prev = [], start
    for s in curve:
        e = float(s["equity"])
        if prev > 0: rets.append((e - prev) / prev)
        prev = e
    sharpe = 0.0
    if len(rets) > 1:
        sd = _stats.stdev(rets)
        sharpe = (_stats.mean(rets) / sd * 252 ** 0.5) if sd > 0 else 0.0

    # Max drawdown
    peak, mdd = start, 0.0
    for s in curve:
        e = float(s["equity"])
        if e > peak: peak = e
        dd = (e - peak) / peak if peak > 0 else 0
        if dd < mdd: mdd = dd

    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    period = ""
    if curve:
        period = f"{curve[0]['date']}  to  {curve[-1]['date']} ({len(curve)} trading days)"

    return dict(
        trades=trades, curve=curve, start=start, final=final,
        pnl=pnl, ret=ret, wins=wins, losses=losses, wr=wr,
        sharpe=sharpe, mdd=mdd, pf=pf, period=period,
    )

# ===========================================================================
# LIVE BOT PAGES
# ===========================================================================

def page_home():
    st.markdown("## Home")
    if MODE == "alpaca_live":
        st.error("LIVE TRADING MODE - real money at risk.")

    # Circuit breaker
    _daily_state: dict = {}
    _ds_path = DATA / "daily_state.json"
    if _ds_path.exists():
        try:
            _daily_state = json.loads(_ds_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    cb = _daily_state.get("circuit_breaker", False)

    # Regime from today's decisions
    _today_str = datetime.now().strftime("%Y-%m-%d")
    _decisions_today = [d for d in load_decisions_master("live")
                        if d.get("date", "") == _today_str]
    _regime: dict = {}
    if _decisions_today:
        for _d in reversed(_decisions_today):
            r = _d.get("regime") or {}
            if r:
                _regime = r
                break

    if _regime.get("label"):
        _rl = str(_regime["label"]).lower()
        _css_r = {"bullish": "regime-bull", "bearish": "regime-bear",
                  "volatile": "regime-vol"}.get(_rl, "regime-neut")
        _icon_r = {"bullish": "GREEN", "bearish": "RED", "volatile": "YELLOW"}.get(_rl, "")
        _sc = _regime.get("score")
        _sc_str = f"  score: {_sc:+.2f}" if isinstance(_sc, (int, float)) else ""
        st.markdown(
            f'<div class="{_css_r}"><strong>MARKET REGIME: {_rl.upper()}</strong>{_sc_str}</div>',
            unsafe_allow_html=True,
        )
        st.write("")

    # Row 1 mini metrics
    st.markdown('<div class="section-label">Account Overview</div>', unsafe_allow_html=True)
    _live_positions_panel(mini=True)

    # Circuit breaker pill
    st.write("")
    cb_html = ('<span class="pill p-red">CB: TRIGGERED</span>' if cb
               else '<span class="pill p-green">CB: OK</span>')
    st.markdown(cb_html, unsafe_allow_html=True)

    st.write("")

    # Today's shortlist
    st.markdown('<div class="section-label">Today\'s Shortlist</div>', unsafe_allow_html=True)
    sl = load_shortlist()
    st.caption(f"Screened {sl.get('date', '-')} — {len(sl.get('symbols', []))} tickers")
    if sl.get("symbols"):
        trends = sl.get("trends", {}) or {}
        _ds = load_deep_scores()
        sl_rows = []
        for sym in sl["symbols"]:
            t   = trends.get(sym, {}) or {}
            c30 = t.get("change_30d")
            g   = _ds.get(sym, {}).get("grade", "-") if sym in _ds else "-"
            sl_rows.append({
                "Symbol": sym,
                "Trend":  t.get("label", "-"),
                "Grade":  g,
                "30d %":  (c30 * 100) if isinstance(c30, (int, float)) else None,
            })
        _sl_html_rows = []
        for r in sl_rows:
            pct_str = f"{r['30d %']:+.1f}%" if r["30d %"] is not None else "-"
            pct_col = C_BUY if (r["30d %"] or 0) >= 0 else C_SELL
            _sl_html_rows.append(
                f"<tr>"
                f"<td style='padding:4px 10px;font-weight:600'>{r['Symbol']}</td>"
                f"<td style='padding:4px 10px;color:{C_MUTED}'>{r['Trend']}</td>"
                f"<td style='padding:4px 10px;text-align:center'>{grade_badge(r['Grade'])}</td>"
                f"<td style='padding:4px 10px;color:{pct_col};text-align:right'>{pct_str}</td>"
                f"</tr>"
            )
        _sl_table = (
            "<table style='width:100%;border-collapse:collapse;font-family:monospace'>"
            "<thead><tr style='border-bottom:1px solid #30363d'>"
            "<th style='padding:4px 10px;text-align:left;color:#8b949e;font-size:11px'>Symbol</th>"
            "<th style='padding:4px 10px;text-align:left;color:#8b949e;font-size:11px'>Trend</th>"
            "<th style='padding:4px 10px;text-align:center;color:#8b949e;font-size:11px'>Grade</th>"
            "<th style='padding:4px 10px;text-align:right;color:#8b949e;font-size:11px'>30d %</th>"
            "</tr></thead><tbody>"
            + "".join(_sl_html_rows)
            + "</tbody></table>"
        )
        st.markdown(_sl_table, unsafe_allow_html=True)

    # Mini positions
    st.write("")
    st.markdown('<div class="section-label">Open Positions</div>', unsafe_allow_html=True)
    _live_positions_panel(mini=False)


def page_positions_orders():
    st.markdown("## Positions & Orders")
    _live_positions_panel(mini=False)

    st.write("")
    st.markdown('<div class="section-label">Order History</div>', unsafe_allow_html=True)
    _state = load_state()
    orders = _state.get("orders", [])
    if orders:
        _df_o = pd.DataFrame(orders)
        _df_o["at"]       = pd.to_datetime(_df_o.get("at", pd.Series(dtype=str)), errors="coerce")
        _df_o["notional"] = _df_o["qty"] * _df_o["price"]
        _df_o["cash_chg"] = _df_o.apply(
            lambda r: -r["notional"] if str(r.get("side", "")).upper() == "BUY" else r["notional"],
            axis=1,
        )
        _df_o = _df_o.sort_values("at")
        _df_o["cum_cash"] = 100_000.0 + _df_o["cash_chg"].cumsum()

        _ot1, _ot2, _ot3 = st.tabs(["Orders Table", "Cash Over Time", "P&L by Symbol"])
        with _ot1:
            _cols = [c for c in ["at","symbol","side","qty","price","notional","notes"] if c in _df_o.columns]
            st.dataframe(
                _df_o[_cols].sort_values("at", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "price":    st.column_config.NumberColumn("Fill Price", format="$%.2f"),
                    "notional": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
        with _ot2:
            _cash_df = _df_o[["at","cum_cash"]].rename(columns={"at":"date","cum_cash":"cash"})
            st.plotly_chart(_plotly_line(_cash_df, "date", "cash", color=C_BUY, label="Cash"),
                            use_container_width=True)
        with _ot3:
            # P&L by symbol: pair buys and sells
            _sym_pnl: dict = {}
            for _, row in _df_o.iterrows():
                sym = row.get("symbol", "")
                side = str(row.get("side", "")).upper()
                notional = float(row.get("notional", 0))
                if sym not in _sym_pnl:
                    _sym_pnl[sym] = 0.0
                if side == "SELL":
                    _sym_pnl[sym] += notional
                elif side == "BUY":
                    _sym_pnl[sym] -= notional
            if _sym_pnl:
                _syms = list(_sym_pnl.keys())
                _vals = [_sym_pnl[s] for s in _syms]
                _colors = [C_BUY if v >= 0 else C_SELL for v in _vals]
                _fig_pnl = go.Figure(go.Bar(x=_syms, y=_vals, marker_color=_colors))
                _fig_pnl.update_layout(**_plotly_dark_layout(height=280))
                st.plotly_chart(_fig_pnl, use_container_width=True)
    else:
        st.caption("No orders yet.")


def _parse_gate_notes(gate_notes_str: str) -> dict:
    """Parse gate_notes field into per-gate status dict."""
    gates = {}
    if not gate_notes_str:
        return gates
    for part in str(gate_notes_str).split(","):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            gates[k.strip()] = v.strip()
        elif part:
            gates[part.strip()] = "block"
    return gates


def page_signals_gates(source: str = "live", run_id: str = "") -> None:
    st.markdown("## Signals & Gates")

    _today_str = datetime.now().strftime("%Y-%m-%d")
    _all_decisions = load_decisions_master(source)

    if source == "live":
        _decisions = [d for d in _all_decisions if d.get("date", "") == _today_str]
        _period_label = f"Today ({_today_str})"
    else:
        _decisions = [d for d in _all_decisions
                      if not run_id or d.get("run_id", "") == run_id]
        _period_label = f"Run: {run_id}" if run_id else "All runs"

    if not _decisions:
        st.info(f"No decision data for {_period_label}.")
        return

    # Gate breakdown table
    GATE_TYPES = ["vol_low", "deep_score", "earnings", "gap_up", "tech_veto",
                  "min_price", "breadth", "regime"]
    st.markdown(f'<div class="section-label">Gate Breakdown — {_period_label}</div>',
                unsafe_allow_html=True)

    _gate_rows_html = []
    _gate_block_counts: dict = {g: 0 for g in GATE_TYPES}
    for dec in _decisions:
        sym    = dec.get("symbol", "")
        action = str(dec.get("action", "")).upper()
        gnotes = _parse_gate_notes(dec.get("gate_notes", ""))
        cells = f"<td style='padding:3px 8px;font-weight:600'>{sym}</td>"
        cells += f"<td style='padding:3px 8px'>{action_html(action)}</td>"
        for g in GATE_TYPES:
            if g in gnotes:
                status = gnotes[g]
                if status == "block":
                    _gate_block_counts[g] += 1
                cells += f"<td style='padding:3px 8px;text-align:center'>{_gate_pill(status)}</td>"
            else:
                cells += f"<td style='padding:3px 8px;text-align:center'>{_gate_pill('na')}</td>"
        _gate_rows_html.append(f"<tr>{cells}</tr>")

    _th = "".join(
        f"<th style='padding:3px 8px;text-align:center;color:#8b949e;font-size:11px'>{g}</th>"
        for g in GATE_TYPES
    )
    _gate_table = (
        "<table style='width:100%;border-collapse:collapse;font-family:monospace;font-size:12px'>"
        "<thead><tr style='border-bottom:1px solid #30363d'>"
        "<th style='padding:3px 8px;text-align:left;color:#8b949e;font-size:11px'>Symbol</th>"
        "<th style='padding:3px 8px;text-align:left;color:#8b949e;font-size:11px'>Action</th>"
        + _th +
        "</tr></thead><tbody>"
        + "".join(_gate_rows_html)
        + "</tbody></table>"
    )
    st.markdown(_gate_table, unsafe_allow_html=True)

    st.write("")
    _gc1, _gc2 = st.columns(2)

    with _gc1:
        st.markdown('<div class="section-label">Block Counts by Gate</div>', unsafe_allow_html=True)
        _active_gates = {g: c for g, c in _gate_block_counts.items() if c > 0}
        if _active_gates:
            _fig_donut = go.Figure(go.Pie(
                labels=list(_active_gates.keys()),
                values=list(_active_gates.values()),
                hole=0.55,
                marker_colors=["#f85149","#e3b341","#58a6ff","#3fb950",
                                "#f0883e","#bc8cff","#d29922","#8b949e"],
            ))
            _fig_donut.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#c9d1d9", size=11),
                margin=dict(l=0, r=0, t=10, b=0),
                height=220,
                showlegend=True,
                legend=dict(bgcolor="rgba(0,0,0,0)"),
            )
            st.plotly_chart(_fig_donut, use_container_width=True)
        else:
            st.caption("No gate blocks recorded.")

    with _gc2:
        st.markdown('<div class="section-label">Signal Values</div>', unsafe_allow_html=True)
        _sig_rows = []
        for dec in _decisions:
            sym    = dec.get("symbol", "")
            action = str(dec.get("action", "")).upper()
            _sig_rows.append({
                "Symbol":   sym,
                "Action":   action,
                "Tech":     dec.get("tech_score") or dec.get("entry_tech"),
                "News":     dec.get("news_score") or dec.get("entry_news"),
                "Breadth":  dec.get("breadth_score") or dec.get("entry_breadth"),
                "LLM":      dec.get("llm_score") or dec.get("entry_llm"),
                "Combined": dec.get("combined_score") or dec.get("entry_combined"),
            })
        if _sig_rows:
            st.dataframe(
                pd.DataFrame(_sig_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Tech":     st.column_config.NumberColumn(format="%.3f"),
                    "News":     st.column_config.NumberColumn(format="%.3f"),
                    "Breadth":  st.column_config.NumberColumn(format="%.3f"),
                    "LLM":      st.column_config.NumberColumn(format="%.3f"),
                    "Combined": st.column_config.NumberColumn(format="%.3f"),
                },
            )

    # Rolling 30-day gate bar (live only)
    if source == "live":
        st.write("")
        st.markdown('<div class="section-label">30-Day Gate Rejection Trend</div>',
                    unsafe_allow_html=True)
        _cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _recent = [d for d in _all_decisions if d.get("date", "") >= _cutoff]
        _rolling_counts: dict = {g: 0 for g in GATE_TYPES}
        for dec in _recent:
            gnotes = _parse_gate_notes(dec.get("gate_notes", ""))
            for g in GATE_TYPES:
                if gnotes.get(g) == "block":
                    _rolling_counts[g] += 1
        _rc_active = {g: c for g, c in _rolling_counts.items() if c > 0}
        if _rc_active:
            _fig_bar = go.Figure(go.Bar(
                x=list(_rc_active.keys()),
                y=list(_rc_active.values()),
                marker_color=C_SELL,
            ))
            _fig_bar.update_layout(**_plotly_dark_layout(height=200))
            st.plotly_chart(_fig_bar, use_container_width=True)
        else:
            st.caption("No gate blocks in the last 30 days.")


def page_decisions_log(source: str = "live", run_id: str = "",
                       run_start: str = "", run_end: str = "") -> None:
    st.markdown("## Decisions Log")

    _all_decisions = load_decisions_master(source)
    _today = datetime.now().date()

    if source == "live":
        _sel_date = st.date_input("Date", value=_today, key="dec_date_live")
        _sel_date_str = str(_sel_date)
        _decisions = [d for d in _all_decisions if d.get("date", "") == _sel_date_str]
    else:
        _start_d = date.fromisoformat(run_start) if run_start else _today - timedelta(days=30)
        _end_d   = date.fromisoformat(run_end)   if run_end   else _today
        _col1, _col2 = st.columns(2)
        with _col1:
            _sel_date = st.date_input("Date", value=_end_d,
                                      min_value=_start_d, max_value=_end_d,
                                      key="dec_date_bt")
        _sel_date_str = str(_sel_date)
        _decisions = [d for d in _all_decisions
                      if d.get("date", "") == _sel_date_str
                      and (not run_id or d.get("run_id", "") == run_id)]

    _action_filter = st.selectbox("Filter", ["All", "BUY", "CLOSE/SELL", "HOLD"],
                                  key=f"dec_filter_{source}")

    if _action_filter == "BUY":
        _decisions = [d for d in _decisions if str(d.get("action","")).upper() == "BUY"]
    elif _action_filter == "CLOSE/SELL":
        _decisions = [d for d in _decisions
                      if str(d.get("action","")).upper() in ("CLOSE","SELL","STOP_LOSS","TAKE_PROFIT")]
    elif _action_filter == "HOLD":
        _decisions = [d for d in _decisions if str(d.get("action","")).upper() == "HOLD"]

    _n_buy  = sum(1 for d in _decisions if str(d.get("action","")).upper() == "BUY")
    _n_sell = sum(1 for d in _decisions
                  if str(d.get("action","")).upper() in ("CLOSE","SELL","STOP_LOSS","TAKE_PROFIT"))
    _n_hold = sum(1 for d in _decisions if str(d.get("action","")).upper() == "HOLD")
    st.caption(f"{len(_decisions)} entries  |  {_n_buy} buys  |  {_n_sell} sells  |  {_n_hold} holds")

    if not _decisions:
        st.info("No decisions for selected filters.")
        return

    def _score_color(v) -> str:
        if not isinstance(v, (int, float)):
            return "#8b949e"
        return C_BUY if v > 0.2 else C_SELL if v < -0.2 else "#8b949e"

    _html_rows = []
    for dec in _decisions:
        action = str(dec.get("action", "")).upper()
        sym    = dec.get("symbol", "")
        combined = dec.get("combined_score") or dec.get("entry_combined") or 0
        tech     = dec.get("tech_score")    or dec.get("entry_tech")
        news     = dec.get("news_score")    or dec.get("entry_news")
        breadth  = dec.get("breadth_score") or dec.get("entry_breadth")
        llm      = dec.get("llm_score")     or dec.get("entry_llm")
        gnotes   = dec.get("gate_notes", "") or ""
        fill     = dec.get("fill_price")
        qty      = dec.get("qty")

        def _sv(v) -> str:
            return f'<span style="color:{_score_color(v)}">{v:.3f}</span>' if isinstance(v, (int, float)) else "-"

        if action == "HOLD":
            regime  = dec.get("regime", {}) if isinstance(dec.get("regime"), dict) else {}
            rlabel  = regime.get("label", "-") if regime else "-"
            trend   = dec.get("trend", "-")
            p_dec   = dec.get("price_at_decision")
            p_eod   = dec.get("price_at_eod")
            hit_str = ""
            pct_str = "-"
            if isinstance(p_dec, (int, float)) and isinstance(p_eod, (int, float)) and p_dec > 0:
                pct = (p_eod / p_dec - 1) * 100
                pct_col = C_BUY if pct >= 0 else C_SELL
                pct_str = f'<span style="color:{pct_col}">{pct:+.2f}%</span>'
                hit = dec.get("hit")
                hit_str = "OK" if hit else "X"
            _html_rows.append(
                f"<tr style='opacity:0.7'>"
                f"<td style='padding:3px 8px'>{action_html(action)}</td>"
                f"<td style='padding:3px 8px;font-weight:600'>{sym}</td>"
                f"<td style='padding:3px 8px;color:{C_MUTED}'>{rlabel}</td>"
                f"<td style='padding:3px 8px;color:{C_MUTED}'>{trend}</td>"
                f"<td style='padding:3px 8px'>{pct_str}</td>"
                f"<td style='padding:3px 8px;color:{C_MUTED}'>{hit_str}</td>"
                f"<td colspan='4'></td>"
                f"</tr>"
            )
        else:
            fill_str = f"${fill:,.2f}" if isinstance(fill, (int, float)) else "-"
            qty_str  = str(int(qty)) if isinstance(qty, (int, float)) else "-"
            _html_rows.append(
                f"<tr>"
                f"<td style='padding:3px 8px'>{action_html(action)}</td>"
                f"<td style='padding:3px 8px;font-weight:600'>{sym}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_sv(combined)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_sv(tech)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_sv(news)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_sv(breadth)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_sv(llm)}</td>"
                f"<td style='padding:3px 8px;color:{C_MUTED};font-size:11px'>{gnotes[:60]}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{fill_str}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{qty_str}</td>"
                f"</tr>"
            )

    _dec_table = (
        "<table style='width:100%;border-collapse:collapse;font-family:monospace;font-size:12px'>"
        "<thead><tr style='border-bottom:1px solid #30363d'>"
        "<th style='padding:3px 8px;text-align:left;color:#8b949e;font-size:11px'>Action</th>"
        "<th style='padding:3px 8px;text-align:left;color:#8b949e;font-size:11px'>Symbol</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Combined</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Tech</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>News</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Breadth</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>LLM</th>"
        "<th style='padding:3px 8px;text-align:left;color:#8b949e;font-size:11px'>Gates</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Fill</th>"
        "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Qty</th>"
        "</tr></thead><tbody>"
        + "".join(_html_rows)
        + "</tbody></table>"
    )
    st.markdown(_dec_table, unsafe_allow_html=True)


def page_deep_scores(note: str = "") -> None:
    st.markdown("## Deep Scores")
    if note:
        st.info(note)

    _ds = load_deep_scores()
    if not _ds:
        st.info("No deep scores yet. Run run-deep-scorer.bat or wait for the Sunday scheduled task.")
        return

    _ds_rows = []
    for _sym, _entry in _ds.items():
        if not _entry or (_entry.get("error") and not _entry.get("breakdown")):
            continue
        _bd      = _entry.get("breakdown") or {}
        _updated = (_entry.get("updated") or "")[:10]
        _age_d   = 0
        _stale   = False
        if _updated:
            try:
                _age_d = (datetime.now().date() -
                          datetime.fromisoformat(_updated).date()).days
                _stale = _age_d > 7
            except Exception:
                pass
        _sc = float(_entry.get("score", 0))
        _ds_rows.append({
            "Symbol":      _sym,
            "Score":       _sc,
            "Grade":       _entry.get("grade", "?"),
            "Signal":      _entry.get("signal", "?"),
            "Technical":   (_bd.get("technical")   or {}).get("score"),
            "Fundamental": (_bd.get("fundamental") or {}).get("score"),
            "Sentiment":   (_bd.get("sentiment")   or {}).get("score"),
            "Risk":        (_bd.get("risk")        or {}).get("score"),
            "Thesis":      (_bd.get("thesis")      or {}).get("score"),
            "Updated":     _updated,
            "Age (days)":  _age_d,
            "Stale":       "yes" if _stale else "",
        })
    _ds_rows.sort(key=lambda r: -r["Score"])

    st.markdown(f'<div class="section-label">{len(_ds_rows)} tickers scored</div>',
                unsafe_allow_html=True)

    # Grade distribution donut
    _grade_counts: dict = {}
    for r in _ds_rows:
        g = r["Grade"]
        _grade_counts[g] = _grade_counts.get(g, 0) + 1
    _grade_order = ["A+", "A", "B", "C", "D", "F"]
    _grade_colors_list = [GRADE_COLORS.get(g, C_MUTED) for g in _grade_order if g in _grade_counts]
    _fig_donut = go.Figure(go.Pie(
        labels=[g for g in _grade_order if g in _grade_counts],
        values=[_grade_counts[g] for g in _grade_order if g in _grade_counts],
        hole=0.55,
        marker_colors=_grade_colors_list,
    ))
    _fig_donut.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c9d1d9", size=11),
        margin=dict(l=0, r=0, t=10, b=0),
        height=220,
        legend=dict(bgcolor="rgba(0,0,0,0)"),
    )
    st.plotly_chart(_fig_donut, use_container_width=True)

    # Grade filter
    _grade_opts = ["All"] + [g for g in _grade_order if g in _grade_counts]
    _grade_sel = st.selectbox("Filter by Grade", _grade_opts, key="ds_grade_filter")
    _filtered_rows = _ds_rows if _grade_sel == "All" else [r for r in _ds_rows if r["Grade"] == _grade_sel]

    st.dataframe(
        pd.DataFrame(_filtered_rows), use_container_width=True, hide_index=True,
        column_config={
            "Score":       st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
            "Technical":   st.column_config.NumberColumn(format="%.0f"),
            "Fundamental": st.column_config.NumberColumn(format="%.0f"),
            "Sentiment":   st.column_config.NumberColumn(format="%.0f"),
            "Risk":        st.column_config.NumberColumn(format="%.0f"),
            "Thesis":      st.column_config.NumberColumn(format="%.0f"),
            "Age (days)":  st.column_config.NumberColumn(format="%d d"),
        },
    )

    # Ticker deep dive
    st.write("")
    st.markdown('<div class="section-label">Ticker Deep Dive</div>', unsafe_allow_html=True)
    _pick = st.selectbox(
        "Select ticker for full breakdown:",
        ["-"] + [r["Symbol"] for r in _filtered_rows],
        key="ds_picker",
    )
    if _pick and _pick != "-":
        _e  = _ds.get(_pick, {})
        _bd = _e.get("breakdown") or {}
        _ks = _e.get("key_stats")  or {}
        _sc = _e.get("score", 0)
        _gr = _e.get("grade", "?")
        _si = _e.get("signal", "?")

        st.markdown(
            f'### {_pick} &nbsp; {grade_badge(_gr)} &nbsp; '
            f'{signal_badge(_si)} &nbsp; '
            f'<span style="color:#8b949e;font-size:14px">{_sc:.1f} / 100</span>',
            unsafe_allow_html=True,
        )

        _dims     = ["Technical", "Fundamental", "Sentiment", "Risk", "Thesis"]
        _dim_keys = ["technical", "fundamental", "sentiment", "risk", "thesis"]
        _dim_scores = [(_bd.get(k) or {}).get("score", 50) for k in _dim_keys]
        _radar = go.Figure(go.Scatterpolar(
            r=_dim_scores + [_dim_scores[0]],
            theta=_dims + [_dims[0]],
            fill="toself", fillcolor="rgba(88,166,255,0.15)",
            line=dict(color="#58a6ff", width=2),
        ))
        _radar.update_layout(
            polar=dict(
                bgcolor="rgba(0,0,0,0)",
                radialaxis=dict(visible=True, range=[0, 100],
                                color="#8b949e", gridcolor="#21262d"),
                angularaxis=dict(color="#c9d1d9"),
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=40, r=40, t=40, b=40),
            height=320, showlegend=False,
        )
        st.plotly_chart(_radar, use_container_width=True)

        for _dim_label, _dim_key in zip(_dims, _dim_keys):
            _d = _bd.get(_dim_key) or {}
            if not _d:
                continue
            with st.expander(f"{_dim_label}  -  {_d.get('score','?')} / 100"):
                st.markdown(f"**{_d.get('rationale', '')}**")
                c_bull, c_bear = st.columns(2)
                c_bull.success(f"Bull: {_d.get('bull', '-')}")
                c_bear.error(f"Bear: {_d.get('bear', '-')}")

        if _ks:
            with st.expander("Key Stats"):
                _ks_clean = {k: v for k, v in _ks.items() if v is not None}
                _ks_cols = st.columns(3)
                for i, (k, v) in enumerate(_ks_clean.items()):
                    with _ks_cols[i % 3]:
                        if isinstance(v, float):
                            st.metric(k.replace("_", " ").title(), f"{v:.2f}")
                        else:
                            st.metric(k.replace("_", " ").title(), str(v))


def _render_indicator_tab(ind_name: str, iv: dict) -> None:
    """Render a single indicator tab for both live and backtest modes."""
    n       = iv.get("n") or iv.get("samples", 0)
    wr      = iv.get("win_rate") or iv.get("hit_rate")
    avg_ret = iv.get("avg_ret") or iv.get("avg_edge")
    corr    = iv.get("corr") or iv.get("correlation")
    wins_n  = iv.get("wins") or (int((wr or 0) * n) if wr is not None else 0)
    losses_n = n - wins_n

    # Top metrics row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Win %",      f"{(wr or 0)*100:.1f}%"    if wr  is not None else "-")
    c2.metric("Avg Return", f"{(avg_ret or 0)*100:+.2f}%" if avg_ret is not None else "-")
    c3.metric("Samples",    str(n))
    corr_col = C_BUY if (corr or 0) >= 0 else C_SELL
    c4.markdown(
        f"<div style='padding-top:1.1rem;font-size:0.8rem;color:{C_MUTED}'>Correlation</div>"
        f"<div style='font-size:1.3rem;font-weight:700;color:{corr_col}'>"
        f"{corr:+.3f}</div>" if corr is not None else
        f"<div style='padding-top:1.1rem;font-size:0.8rem;color:{C_MUTED}'>Correlation</div>"
        f"<div style='font-size:1.3rem;font-weight:700;color:{C_MUTED}'>-</div>",
        unsafe_allow_html=True,
    )

    col_left, col_right = st.columns(2)

    # Win/Loss bar
    with col_left:
        _fig_wl = go.Figure(go.Bar(
            x=["Wins", "Losses"],
            y=[wins_n, losses_n],
            marker_color=[C_BUY, C_SELL],
            text=[str(wins_n), str(losses_n)],
            textposition="outside",
        ))
        _fig_wl.update_layout(**_plotly_dark_layout(height=200,
            yaxis=dict(showgrid=False, color=C_MUTED),
            xaxis=dict(color=C_MUTED)))
        st.caption("Win / Loss count")
        st.plotly_chart(_fig_wl, use_container_width=True, key=f"ind_wl_{ind_name}")

    # High-signal vs Low-signal breakdown (backtest only)
    high = iv.get("high") or {}
    low  = iv.get("low")  or {}
    if high.get("n") and low.get("n"):
        with col_right:
            _fig_hl = go.Figure()
            _fig_hl.add_trace(go.Bar(
                name="Bullish (>=0.5)",
                x=["Win %", "Avg Ret %"],
                y=[(high.get("win_rate") or 0)*100, (high.get("avg_ret") or 0)*100],
                marker_color=C_BUY,
            ))
            _fig_hl.add_trace(go.Bar(
                name="Bearish (<0.5)",
                x=["Win %", "Avg Ret %"],
                y=[(low.get("win_rate") or 0)*100, (low.get("avg_ret") or 0)*100],
                marker_color=C_SELL,
            ))
            _fig_hl.update_layout(**_plotly_dark_layout(height=200, barmode="group"))
            st.caption(f"Bullish signal >=0.5 (n={high['n']}) vs Bearish <0.5 (n={low['n']})")
            st.plotly_chart(_fig_hl, use_container_width=True, key=f"ind_hl_{ind_name}")

    # By-action breakdown
    by_action = iv.get("by_action") or {}
    if by_action:
        st.markdown('<div class="section-label" style="margin-top:0.5rem">By Decision Type</div>',
                    unsafe_allow_html=True)
        _ac_cols = st.columns(len(by_action))
        for i, (act, adat) in enumerate(by_action.items()):
            act_color = C_BUY if act == "BUY" else C_SELL if act == "SELL" else C_INFO
            with _ac_cols[i]:
                st.markdown(
                    f"<div style='border:1px solid {act_color}22;border-radius:6px;padding:0.5rem;text-align:center'>"
                    f"<div style='color:{act_color};font-weight:700;font-size:0.9rem'>{act}</div>"
                    f"<div style='font-size:1.1rem;font-weight:600'>{adat['win_rate']*100:.1f}%</div>"
                    f"<div style='color:{C_MUTED};font-size:0.75rem'>Win % / {adat['n']} samples</div>"
                    f"<div style='font-size:0.9rem;color:{'#3fb950' if adat['avg_ret']>=0 else '#f85149'}'>"
                    f"{adat['avg_ret']*100:+.2f}%</div>"
                    f"<div style='color:{C_MUTED};font-size:0.75rem'>Avg Return</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    # Scatter: score vs fwd_5d_return (backtest only)
    scores  = iv.get("scores")
    returns = iv.get("returns")
    if scores and returns and len(scores) > 3:
        _colors_sc = [C_BUY if r > 0 else C_SELL for r in returns]
        _fig_sc = go.Figure(go.Scatter(
            x=scores, y=[r * 100 for r in returns],
            mode="markers",
            marker=dict(color=_colors_sc, size=5, opacity=0.65),
            hovertemplate=f"Score: %{{x:.3f}}<br>Fwd 5d Ret: %{{y:+.2f}}%<extra></extra>",
        ))
        _fig_sc.add_hline(y=0, line_dash="dot", line_color=C_MUTED, opacity=0.5)
        _fig_sc.add_vline(x=0.5, line_dash="dot", line_color=C_WARN, opacity=0.4)
        _fig_sc.update_layout(**_plotly_dark_layout(height=220,
            xaxis=dict(title=f"{ind_name} Score", showgrid=False, color=C_MUTED),
            yaxis=dict(title="Fwd 5d Return %", showgrid=True, gridcolor="#21262d", color=C_MUTED)))
        st.caption("Score vs Forward 5-day Return")
        st.plotly_chart(_fig_sc, use_container_width=True, key=f"ind_sc_{ind_name}")


def page_indicators(
    source: str = "live",
    trades: list | None = None,
    run_id: str | None = None,
) -> None:
    st.markdown("## Indicators")

    # ------------------------------------------------------------------
    # Backtest mode: compute stats from backtest_decisions_master.jsonl
    # ------------------------------------------------------------------
    if source == "backtest":
        _all_bt_rows = load_decisions_master("backtest")

        scope_opts = ["All Runs"] + sorted({r.get("run_id","") for r in _all_bt_rows if r.get("run_id")})
        _default_scope = run_id if (run_id and run_id in scope_opts) else "All Runs"
        _scope = st.selectbox("Scope", scope_opts,
                              index=scope_opts.index(_default_scope),
                              key="ind_scope")

        _rows = _all_bt_rows if _scope == "All Runs" else [r for r in _all_bt_rows if r.get("run_id") == _scope]

        if not _rows:
            st.caption("No backtest decision data yet. Run a backtest to populate.")
            return

        _bt_stats_computed = compute_bt_indicator_stats(_rows)

        # Summary table across all indicators
        _summary_data = []
        for ind_name in _IND_SCORE_KEYS:
            iv = _bt_stats_computed.get(ind_name)
            if iv is None:
                continue
            wr = iv.get("win_rate")
            _summary_data.append({
                "Indicator": ind_name,
                "Samples":   iv["n"],
                "Win %":     round((wr or 0)*100, 1),
                "Avg Return %": round((iv.get("avg_ret") or 0)*100, 2),
                "Correlation":  iv.get("corr"),
                "Wins":      iv.get("wins", 0),
                "Losses":    iv.get("losses", 0),
            })

        if _summary_data:
            st.markdown('<div class="section-label">Summary — All Indicators</div>',
                        unsafe_allow_html=True)
            _df_sum = pd.DataFrame(_summary_data)
            st.dataframe(
                _df_sum, use_container_width=True, hide_index=True,
                column_config={
                    "Win %":        st.column_config.NumberColumn(format="%.1f%%"),
                    "Avg Return %": st.column_config.NumberColumn(format="%+.2f%%"),
                    "Correlation":  st.column_config.NumberColumn(format="%.3f"),
                },
            )
            st.divider()

        # Per-indicator tabs
        _ind_names_avail = [k for k in _IND_SCORE_KEYS if _bt_stats_computed.get(k) is not None]
        if not _ind_names_avail:
            st.caption("No indicator score data found in decisions.")
            return

        _tabs = st.tabs(_ind_names_avail)
        for tab, ind_name in zip(_tabs, _ind_names_avail):
            with tab:
                iv = _bt_stats_computed[ind_name]
                _render_indicator_tab(ind_name, iv)

        return

    # ------------------------------------------------------------------
    # Live mode: use indicator_stats.json (populated by EOD reflection)
    # ------------------------------------------------------------------
    _ind_stats_raw = load_indicator_stats()
    _ind_data = _ind_stats_raw.get("indicators") or {}
    _ind_history = load_ind_stats_history()

    LIVE_INDICATOR_NAMES = ["rsi", "macd", "trend", "bb", "obv", "vwap", "fib", "roc", "rs_etf"]
    _display_names = {
        "rsi": "RSI", "macd": "MACD", "trend": "Trend/ADX",
        "bb": "BB", "obv": "OBV", "vwap": "VWAP", "fib": "Fib",
        "roc": "ROC", "rs_etf": "RS/ETF",
    }

    if not _ind_data or all(not (_ind_data.get(k) or {}).get("samples") for k in LIVE_INDICATOR_NAMES):
        st.info("No indicator data yet — accumulates after each EOD reflection cycle. "
                "Run the live bot through a trading day to populate.")
        return

    _updated = _ind_stats_raw.get("updated", "")
    if _updated:
        st.caption(f"Last updated: {_updated}  |  Window: {_ind_stats_raw.get('window_days', 30)} days")

    _avail = [k for k in LIVE_INDICATOR_NAMES if (_ind_data.get(k) or {}).get("samples")]
    _live_tabs = st.tabs([_display_names.get(k, k.upper()) for k in _avail])
    for tab, key in zip(_live_tabs, _avail):
        with tab:
            _iv_raw = _ind_data.get(key) or {}
            samples  = _iv_raw.get("samples", 0)
            hr       = _iv_raw.get("hit_rate")
            avg_edge = _iv_raw.get("avg_edge")
            corr     = _iv_raw.get("correlation")
            wins_n   = int((hr or 0) * samples) if isinstance(hr, float) else 0
            losses_n = samples - wins_n

            # Build a unified iv dict for _render_indicator_tab
            _iv_unified = {
                "n":        samples,
                "win_rate": hr,
                "avg_ret":  avg_edge,
                "corr":     corr,
                "wins":     wins_n,
                "losses":   losses_n,
            }
            _render_indicator_tab(_display_names.get(key, key.upper()), _iv_unified)

            # Correlation over time from archive history
            if _ind_history:
                _time_corr = []
                for h in _ind_history:
                    h_inds = (h.get("indicators") or {}).get(key) or {}
                    if h_inds.get("correlation") is not None:
                        _time_corr.append({
                            "date":        h.get("updated") or h.get("snapshot_date", ""),
                            "correlation": h_inds["correlation"],
                            "hit_rate":    h_inds.get("hit_rate"),
                        })
                if _time_corr:
                    _df_corr = pd.DataFrame(_time_corr)
                    _df_corr["date"] = pd.to_datetime(_df_corr["date"], errors="coerce")
                    _df_corr = _df_corr.sort_values("date")
                    _fig_trend = go.Figure()
                    _fig_trend.add_trace(go.Scatter(
                        x=_df_corr["date"], y=_df_corr["correlation"],
                        mode="lines+markers", name="Correlation",
                        line=dict(color=C_INFO, width=2),
                    ))
                    if "hit_rate" in _df_corr.columns:
                        _fig_trend.add_trace(go.Scatter(
                            x=_df_corr["date"],
                            y=[v * 100 if v is not None else None for v in _df_corr["hit_rate"]],
                            mode="lines+markers", name="Win % (scaled /100)",
                            line=dict(color=C_WARN, width=1, dash="dot"),
                            yaxis="y2",
                        ))
                    _fig_trend.add_hline(y=0, line_dash="dot", line_color=C_MUTED, opacity=0.5)
                    _fig_trend.update_layout(**_plotly_dark_layout(height=180))
                    st.caption("Correlation & Win % over time (archive history)")
                    st.plotly_chart(_fig_trend, use_container_width=True, key=f"ind_live_trend_{key}")


def page_rules_and_learning() -> None:
    st.markdown("## Rules & Learning")

    _sec1, _sec2, _sec3 = st.tabs(["Rules", "Track Records", "Signal Weights"])

    with _sec1:
        st.caption(
            "LLM-proposed rules with live hit rate and edge. "
            "Rules are never auto-removed - use the controls below to manage them."
        )
        _rules = load_rules_cached()
        if _rules:
            _rule_rows = []
            for r in _rules:
                s = r.get("stats") or {}
                fires = int(s.get("fire_count", 0))
                hits  = int(s.get("hit_count", 0))
                folls = int(s.get("follow_count", 0))
                fhits = int(s.get("follow_hit_count", 0))
                _rule_rows.append({
                    "ID":       r.get("id", ""),
                    "Active":   bool(r.get("active", True)),
                    "Rule":     r.get("text", ""),
                    "Action":   r.get("action", ""),
                    "Regime":   r.get("regime_when_proposed", ""),
                    "Proposed": r.get("proposed_on", ""),
                    "Fires":    fires,
                    "Hit Rate": hits / fires if fires else None,
                    "Avg Edge": s.get("avg_edge", 0.0),
                    "Follows":  folls,
                    "Flw Hit%": fhits / folls if folls else None,
                    "Last Fired": s.get("last_fired") or "",
                })
            st.dataframe(
                pd.DataFrame(_rule_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Active":   st.column_config.CheckboxColumn(),
                    "Hit Rate": st.column_config.NumberColumn(format="%.0f%%"),
                    "Flw Hit%": st.column_config.NumberColumn(format="%.0f%%"),
                    "Avg Edge": st.column_config.NumberColumn(format="%.2f%%"),
                },
            )
            _min_f = int(((cfg.get("learning") or {}).get("rules") or {}).get(
                "min_fires_for_confidence", 5))
            _lc = [r["ID"] for r in _rule_rows if r["Fires"] < _min_f]
            if _lc:
                st.caption(f"Low-confidence (< {_min_f} fires): {', '.join(_lc[:8])}")

            st.write("")
            st.markdown('<div class="section-label">Manage a Rule</div>', unsafe_allow_html=True)
            _ca, _cb, _cc = st.columns([3, 1, 1])
            _ids = [r["ID"] for r in _rule_rows]
            with _ca:
                _picked = st.selectbox("Select rule", _ids, key="rule_picker")
            with _cb:
                _picked_row = next((r for r in _rule_rows if r["ID"] == _picked), None)
                _active_now = bool(_picked_row and _picked_row["Active"])
                if st.button("Deactivate" if _active_now else "Reactivate", key="toggle_rule"):
                    set_rule_active(_picked, not _active_now)
                    st.cache_data.clear()
                    st.rerun()
            with _cc:
                if st.button("Delete", key="delete_rule"):
                    delete_rule(_picked)
                    st.cache_data.clear()
                    st.rerun()
        else:
            st.caption("No rules yet - EOD reflection will propose them after the first graded day.")

    with _sec2:
        st.caption(
            "Rolling per-ticker win rate and edge. "
            "Hit = directionally correct. Edge = signed % move in the bet direction."
        )
        _tracks = all_ticker_track_records(min_samples=1)
        if _tracks:
            _df_tr = pd.DataFrame([{
                "Symbol":       t["symbol"],
                "Samples":      t["samples"],
                "Hit Rate":     t["hit_rate"],
                "Avg Edge":     t["avg_edge"],
                "BUYs":         t["buys"],
                "Executed":     t["executed"],
                "Avg Realized": t.get("avg_realized"),
                "Best":         t.get("best"),
                "Worst":        t.get("worst"),
                "Stops Hit":    t["stop_hits"],
                "TPs Hit":      t["tp_hits"],
            } for t in _tracks])
            st.dataframe(
                _df_tr, use_container_width=True, hide_index=True,
                column_config={
                    "Hit Rate":     st.column_config.NumberColumn(format="%.0f%%"),
                    "Avg Edge":     st.column_config.NumberColumn(format="%.2f%%"),
                    "Avg Realized": st.column_config.NumberColumn(format="%.2f%%"),
                    "Best":         st.column_config.NumberColumn(format="%.2f%%"),
                    "Worst":        st.column_config.NumberColumn(format="%.2f%%"),
                },
            )
        else:
            st.caption("No graded outcomes yet - they accumulate after each EOD reflection.")

    with _sec3:
        st.caption(
            "Current signal weights for the combined score. "
            "Auto-tuned weekly based on which signal correlates best with realized edge."
        )
        try:
            _weights = effective_weights()
        except Exception:
            _weights = {}
        if _weights:
            _wc = st.columns(len(_weights))
            for _col, (k, v) in zip(_wc, _weights.items()):
                _col.metric(k.capitalize(), f"{v:.0%}")

        _hist = load_weight_history()
        if _hist:
            _wrows = [{"time": h.get("at"), **h.get("weights", {})} for h in _hist]
            _df_wh = pd.DataFrame(_wrows)
            _df_wh["time"] = pd.to_datetime(_df_wh["time"], errors="coerce")
            _df_wh = _df_wh.dropna(subset=["time"]).set_index("time")
            _sig_cols = [c for c in _df_wh.columns]
            _colors_w = ["#58a6ff","#3fb950","#d29922","#f0883e","#bc8cff"]
            _wfig = go.Figure()
            for i, col in enumerate(_sig_cols):
                _wfig.add_trace(go.Scatter(
                    x=_df_wh.index, y=_df_wh[col],
                    name=col.capitalize(), mode="lines+markers",
                    line=dict(color=_colors_w[i % len(_colors_w)], width=2),
                ))
            _wfig.update_layout(
                **_plotly_dark_layout(height=250,
                                      yaxis=dict(showgrid=True, gridcolor="#21262d",
                                                 color=C_MUTED, tickformat=".0%")),
                legend=dict(bgcolor="rgba(0,0,0,0)"),
            )
            st.plotly_chart(_wfig, use_container_width=True)
            _last_h = _hist[-1]
            st.caption(
                f"Last tuned: {_last_h.get('at','?')}  |  "
                f"Samples: {_last_h.get('samples','?')}  |  "
                "Correlations: "
                + ", ".join(f"{k}={v:+.2f}" for k, v in
                            (_last_h.get("correlations") or {}).items())
            )
        else:
            st.caption("No tuning history yet - needs ~40 graded decisions (about 1-2 weeks).")


def page_lessons_reflections() -> None:
    st.markdown("## Lessons & Reflections")

    lessons_text = load_lessons_master()
    if not lessons_text:
        # Fall back to original lessons file
        try:
            lessons_text = LESSONS_FILE.read_text(encoding="utf-8") if LESSONS_FILE.exists() else ""
        except Exception:
            lessons_text = ""

    if lessons_text:
        # Parse into dated sections
        _sections: list[tuple[str, str]] = []
        _current_title = "General"
        _current_lines: list[str] = []
        for line in lessons_text.splitlines():
            if line.startswith("## ") and len(line) > 12:
                if _current_lines:
                    _sections.append((_current_title, "\n".join(_current_lines)))
                _current_title = line[3:].strip()
                _current_lines = []
            else:
                _current_lines.append(line)
        if _current_lines:
            _sections.append((_current_title, "\n".join(_current_lines)))

        # Most recent first
        for title, content in reversed(_sections):
            if content.strip():
                with st.expander(title, expanded=(title == _sections[-1][0] if _sections else False)):
                    st.markdown(content)
    else:
        st.caption("No lessons yet - written after the 4:30 PM EOD reflection.")


# ===========================================================================
# BACKTEST MODE PAGES
# ===========================================================================

def page_bt_run_history() -> None:
    st.markdown("## Run History")

    _bt_history = load_backtest_history()
    if not _bt_history:
        st.info("No backtest history yet. Run run-backtest.bat to generate results.")
        # Try default results file
        _default_bt = load_backtest_results(str(BT_RESULTS_FILE))
        if _default_bt:
            st.caption("Found default backtest_results.json - showing that run.")
            _s = _bt_stats(_default_bt)
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Return",       f"{_s['ret']:+.2%}")
            c2.metric("Win Rate",     f"{_s['wr']:.0%}")
            c3.metric("Sharpe",       f"{_s['sharpe']:.2f}")
            c4.metric("Max Drawdown", f"{_s['mdd']:.2%}")
            c5.metric("Trades",       len(_s["trades"]))
        return

    # Selected run headline
    if _sel_run:
        _s = _bt_stats(_bt_data)
        st.markdown(f'<div class="section-label">Selected Run: {_sel_run_id}</div>',
                    unsafe_allow_html=True)
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Return",       f"{_s['ret']:+.2%}",  fmt_pnl(_s["pnl"]))
        c2.metric("Win Rate",     f"{_s['wr']:.0%}",    f"{len(_s['wins'])}W / {len(_s['losses'])}L")
        c3.metric("Sharpe",       f"{_s['sharpe']:.2f}")
        c4.metric("Max Drawdown", f"{_s['mdd']:.2%}")
        c5.metric("Trades",       len(_s["trades"]))
        if _s["period"]:
            st.caption(f"Period: {_s['period']}")

    st.write("")

    # Summary table of all runs
    st.markdown('<div class="section-label">All Runs</div>', unsafe_allow_html=True)
    _summary_rows = []
    for run in _bt_history:
        _rd = load_backtest_results(run.get("results_file", ""))
        if _rd:
            _rs = _bt_stats(_rd)
            pf_str = f"{_rs['pf']:.2f}x" if _rs["pf"] != float("inf") else "--"
            _summary_rows.append({
                "Run ID":       run.get("run_id", ""),
                "Date":         run.get("date", ""),
                "Days":         len(_rs["curve"]),
                "Return %":     _rs["ret"] * 100,
                "Win Rate":     _rs["wr"],
                "Sharpe":       _rs["sharpe"],
                "Max DD":       _rs["mdd"],
                "Trades":       len(_rs["trades"]),
                "Profit Factor":pf_str,
                "Flags":        run.get("flags", ""),
            })
        else:
            _summary_rows.append({
                "Run ID":  run.get("run_id", ""),
                "Date":    run.get("date", ""),
                "Days":    0, "Return %": None, "Win Rate": None,
                "Sharpe":  None, "Max DD": None, "Trades": 0,
                "Profit Factor": "-", "Flags": run.get("flags", ""),
            })

    if _summary_rows:
        _df_runs = pd.DataFrame(_summary_rows)
        st.dataframe(
            _df_runs, use_container_width=True, hide_index=True,
            column_config={
                "Return %": st.column_config.NumberColumn(format="+.2f%%"),
                "Win Rate": st.column_config.NumberColumn(format=".0f%%"),
                "Sharpe":   st.column_config.NumberColumn(format=".2f"),
                "Max DD":   st.column_config.NumberColumn(format=".2f%%"),
            },
        )

    # Equity curves overlay (up to 10 most recent)
    st.write("")
    st.markdown('<div class="section-label">Equity Curves Comparison</div>', unsafe_allow_html=True)
    _overlay_colors = ["#58a6ff","#3fb950","#d29922","#f0883e","#bc8cff",
                       "#f85149","#56d364","#e3b341","#79c0ff","#ff7b72"]
    _fig_overlay = go.Figure()
    for idx, run in enumerate(_bt_history[:10]):
        _rd = load_backtest_results(run.get("results_file", ""))
        _curve = _rd.get("equity_curve", []) if _rd else []
        if _curve:
            _df_c = pd.DataFrame(_curve)
            _df_c["date"] = pd.to_datetime(_df_c["date"], errors="coerce")
            _fig_overlay.add_trace(go.Scatter(
                x=_df_c["date"], y=_df_c["equity"],
                mode="lines",
                name=run.get("run_id", f"run_{idx}"),
                line=dict(color=_overlay_colors[idx % len(_overlay_colors)], width=1.5),
            ))
    if _fig_overlay.data:
        _fig_overlay.update_layout(
            **_plotly_dark_layout(height=300),
            legend=dict(bgcolor="rgba(0,0,0,0)"),
        )
        st.plotly_chart(_fig_overlay, use_container_width=True)
    else:
        st.caption("No equity curve data available for overlay.")


def page_bt_equity_curve() -> None:
    st.markdown("## Equity Curve")

    if not _bt_data:
        st.info("No backtest data. Select a run in the sidebar.")
        return

    _s = _bt_stats(_bt_data)
    curve = _s["curve"]
    start = _s["start"]

    if not curve:
        st.caption("No equity curve data in this run.")
        return

    _df_c = pd.DataFrame(curve)
    _df_c["date"]   = pd.to_datetime(_df_c["date"], errors="coerce")
    _df_c["return"] = (_df_c["equity"] / start - 1) * 100

    # Equity chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=_df_c["date"], y=_df_c["equity"],
        name="Equity", mode="lines",
        line=dict(color=C_INFO, width=2.5),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
    ))
    fig.add_hline(y=start, line_dash="dot", line_color=C_MUTED, opacity=0.5)
    if "positions" in _df_c.columns:
        fig.add_trace(go.Bar(
            x=_df_c["date"], y=_df_c["positions"],
            name="Open Positions", yaxis="y2",
            marker_color="rgba(63,185,80,0.25)",
        ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=20, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c9d1d9", size=12),
        legend=dict(bgcolor="rgba(0,0,0,0)", x=0, y=1),
        xaxis=dict(showgrid=False, color=C_MUTED),
        yaxis=dict(title="Equity ($)", showgrid=True, gridcolor="#21262d", color=C_MUTED),
        yaxis2=dict(title="Positions", overlaying="y", side="right",
                    showgrid=False, color=C_MUTED),
        hovermode="x unified",
        height=350,
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"Start: ${start:,.0f}  |  Final: ${_s['final']:,.0f}  |  "
        f"P&L: {fmt_pnl(_s['pnl'])}  |  Bars = positions held each day"
    )

    # Drawdown chart
    st.write("")
    st.markdown('<div class="section-label">Drawdown</div>', unsafe_allow_html=True)
    _peak_val = start
    _dd_vals = []
    for row in curve:
        e = float(row["equity"])
        if e > _peak_val:
            _peak_val = e
        dd = (e - _peak_val) / _peak_val if _peak_val > 0 else 0
        _dd_vals.append(dd * 100)
    _df_dd = _df_c.copy()
    _df_dd["drawdown"] = _dd_vals
    _fig_dd = go.Figure(go.Scatter(
        x=_df_dd["date"], y=_df_dd["drawdown"],
        mode="lines", fill="tozeroy",
        fillcolor="rgba(248,81,73,0.15)",
        line=dict(color=C_SELL, width=1.5),
    ))
    _fig_dd.add_hline(y=0, line_dash="dot", line_color=C_MUTED, opacity=0.3)
    _fig_dd.update_layout(**_plotly_dark_layout(height=200,
                                                 yaxis=dict(title="Drawdown %", showgrid=True,
                                                            gridcolor="#21262d", color=C_MUTED,
                                                            tickformat=".1f")))
    st.plotly_chart(_fig_dd, use_container_width=True)

    # Summary metrics
    st.write("")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    pf_str = f"{_s['pf']:.2f}x" if _s["pf"] != float("inf") else "--"
    c1.metric("Total Return",  f"{_s['ret']:+.2%}", fmt_pnl(_s["pnl"]))
    c2.metric("Sharpe Ratio",  f"{_s['sharpe']:.2f}")
    c3.metric("Max Drawdown",  f"{_s['mdd']:.2%}")
    c4.metric("Win Rate",      f"{_s['wr']:.0%}",  f"{len(_s['wins'])}W / {len(_s['losses'])}L")
    c5.metric("Profit Factor", pf_str)
    c6.metric("Total Trades",  len(_s["trades"]))


def page_bt_trade_journal() -> None:
    st.markdown("## Trade Journal")

    if not _bt_data:
        st.info("No backtest data. Select a run in the sidebar.")
        return

    _s = _bt_stats(_bt_data)
    trades = _s["trades"]
    _ds = load_deep_scores()

    if not trades:
        st.caption("No completed trades in this run.")
        return

    # Summary
    best  = max(trades, key=lambda t: t.get("pnl", 0))
    worst = min(trades, key=lambda t: t.get("pnl", 0))
    pf_str = f"{_s['pf']:.2f}x" if _s["pf"] != float("inf") else "--"
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades",       len(trades))
    c2.metric("Win Rate",     f"{_s['wr']:.0%}")
    c3.metric("Profit Factor",pf_str)
    c4.metric("Best Trade",   fmt_pnl(best.get("pnl", 0)))
    c5.metric("Worst Trade",  fmt_pnl(worst.get("pnl", 0)))

    st.write("")

    _j1, _j2, _j3, _j4, _j5, _j6 = st.tabs([
        "Trade Table", "P&L Distribution", "By Symbol",
        "Best / Worst", "Signal Analysis", "Cycle Log",
    ])

    with _j1:
        # Cumulative P&L line
        _df_t = pd.DataFrame(sorted(trades, key=lambda t: t.get("closed_at", "")))
        _df_t["cum_pnl"] = _df_t["pnl"].cumsum()
        _df_t["trade_n"] = range(1, len(_df_t) + 1)
        _fig_cum = go.Figure()
        _fig_cum.add_trace(go.Scatter(
            x=_df_t["trade_n"], y=_df_t["cum_pnl"],
            mode="lines",
            line=dict(color=C_INFO, width=2),
            fill="tozeroy", fillcolor="rgba(88,166,255,0.06)",
            hovertemplate="Trade #%{x}<br>Cumulative P&L: $%{y:+,.2f}<extra></extra>",
        ))
        _fig_cum.add_hline(y=0, line_dash="dot", line_color=C_MUTED, opacity=0.5)
        _fig_cum.update_layout(**_plotly_dark_layout(height=170,
                                                      xaxis=dict(title="Trade #", showgrid=False, color=C_MUTED),
                                                      yaxis=dict(showgrid=True, gridcolor="#21262d", color=C_MUTED, tickprefix="$")),
                               showlegend=False)
        st.plotly_chart(_fig_cum, use_container_width=True)

        _df_t2 = pd.DataFrame(trades).sort_values("closed_at", ascending=False).copy()
        _df_t2["Grade"] = _df_t2["symbol"].map(lambda s: _ds.get(s, {}).get("grade", "--"))
        _display_cols = [c for c in ["closed_at","symbol","Grade","side","qty","entry","exit","pnl","pnl_pct","reason"] if c in _df_t2.columns]
        _rename = {"closed_at":"Date","symbol":"Symbol","side":"Side","qty":"Qty",
                   "entry":"Entry","exit":"Exit","pnl":"P&L","pnl_pct":"P&L %","reason":"Reason"}
        st.dataframe(
            _df_t2[_display_cols].rename(columns=_rename),
            use_container_width=True, hide_index=True,
            column_config={
                "Entry": st.column_config.NumberColumn(format="$%.2f"),
                "Exit":  st.column_config.NumberColumn(format="$%.2f"),
                "P&L":   st.column_config.NumberColumn(format="$+%.2f"),
                "P&L %": st.column_config.NumberColumn(format="+%.1f%%"),
            },
        )

    with _j2:
        _pnl_pcts = [t.get("pnl_pct", 0) * 100 for t in trades]
        _fig_hist = go.Figure(go.Histogram(
            x=_pnl_pcts,
            nbinsx=30,
            marker_color=C_INFO,
            opacity=0.8,
        ))
        _fig_hist.add_vline(x=0, line_dash="dot", line_color=C_MUTED, opacity=0.7)
        _fig_hist.update_layout(**_plotly_dark_layout(height=280,
                                                       xaxis=dict(title="P&L %", showgrid=False, color=C_MUTED),
                                                       yaxis=dict(title="Count", showgrid=True, gridcolor="#21262d", color=C_MUTED)))
        st.plotly_chart(_fig_hist, use_container_width=True)

    with _j3:
        _by_s: dict = {}
        for t in trades:
            s = t["symbol"]
            if s not in _by_s:
                _by_s[s] = {"n": 0, "pnl": 0.0, "wins": 0}
            _by_s[s]["n"]   += 1
            _by_s[s]["pnl"] += t.get("pnl", 0.0)
            if t.get("pnl", 0.0) > 0:
                _by_s[s]["wins"] += 1
        _sym_rows = sorted([
            {"Symbol":    s,
             "Grade":     _ds.get(s, {}).get("grade", "--"),
             "Trades":    v["n"],
             "Win Rate":  v["wins"] / v["n"] if v["n"] else 0,
             "Total P&L": v["pnl"]}
            for s, v in _by_s.items()
        ], key=lambda r: -r["Total P&L"])

        _fig_sym = go.Figure(go.Bar(
            x=[r["Symbol"] for r in _sym_rows],
            y=[r["Total P&L"] for r in _sym_rows],
            marker_color=[C_BUY if r["Total P&L"] >= 0 else C_SELL for r in _sym_rows],
        ))
        _fig_sym.update_layout(**_plotly_dark_layout(height=240))
        st.plotly_chart(_fig_sym, use_container_width=True)
        st.dataframe(
            pd.DataFrame(_sym_rows), use_container_width=True, hide_index=True,
            column_config={
                "Win Rate":  st.column_config.NumberColumn(format="%.0f%%"),
                "Total P&L": st.column_config.NumberColumn(format="$+%.2f"),
            },
        )

    with _j4:
        _sorted_t = sorted(trades, key=lambda t: t.get("pnl", 0), reverse=True)
        _bc, _wc = st.columns(2)
        for _col, _subset, _title in [(_bc, _sorted_t[:5], "Best 5"), (_wc, _sorted_t[-5:], "Worst 5")]:
            with _col:
                st.markdown(f"**{_title}**")
                _sub_cols = [c for c in ["symbol","closed_at","pnl","pnl_pct","reason"] if c in pd.DataFrame(_subset).columns]
                _df_sub = pd.DataFrame(_subset)[_sub_cols].rename(columns={
                    "symbol":"Symbol","closed_at":"Date",
                    "pnl":"P&L","pnl_pct":"P&L %","reason":"Reason",
                })
                st.dataframe(_df_sub, use_container_width=True, hide_index=True,
                    column_config={
                        "P&L":   st.column_config.NumberColumn(format="$+%.2f"),
                        "P&L %": st.column_config.NumberColumn(format="+%.1f%%"),
                    })

    with _j5:
        # Entry signal performance (same as existing bt8 logic)
        _skip_sig = {"stop_loss","locked_profit_stop","end_of_backtest",
                     "weak_close_trim_50pct","reduce_half","flatten_all"}
        _sig_exits = [t for t in trades
                      if t.get("reason","") not in _skip_sig and "close_verdict" in t]
        if _sig_exits:
            _sx_correct = [t for t in _sig_exits if t["close_verdict"] == "correct"]
            _sx_early   = [t for t in _sig_exits if t["close_verdict"] == "early"]
            _sx_neutral = [t for t in _sig_exits if t["close_verdict"] == "neutral"]
            _sx_n = len(_sig_exits)
            _sxa, _sxb, _sxc = st.columns(3)
            _sxa.metric("Correct - stock dropped",  f"{len(_sx_correct)/_sx_n:.0%}", f"{len(_sx_correct)} of {_sx_n}")
            _sxb.metric("Early - stock kept rising",f"{len(_sx_early)/_sx_n:.0%}",   f"{len(_sx_early)} sold too soon")
            _sxc.metric("Neutral - within 1%",      f"{len(_sx_neutral)/_sx_n:.0%}", f"{len(_sx_neutral)} inconclusive")

            _sx_rows = [{
                "Symbol":  t["symbol"],
                "Date":    t.get("closed_at",""),
                "Exit $":  t.get("exit",0),
                "Day End": t.get("close_day_end",0),
                "Move %":  t.get("post_close_move_pct",0),
                "Verdict": t.get("close_verdict",""),
                "P&L":     t.get("pnl",0),
            } for t in sorted(_sig_exits, key=lambda x: x.get("post_close_move_pct",0), reverse=True)]
            st.dataframe(
                pd.DataFrame(_sx_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Exit $":  st.column_config.NumberColumn(format="$%.2f"),
                    "Day End": st.column_config.NumberColumn(format="$%.2f"),
                    "Move %":  st.column_config.NumberColumn(format="+%.1f%%"),
                    "P&L":     st.column_config.NumberColumn(format="$+%.2f"),
                },
            )
        else:
            st.caption("No signal exit verdict data available.")

        st.divider()

        _entry_trades = [t for t in trades if "entry_tech" in t]
        if _entry_trades:
            def _sp(subset: list) -> dict:
                if not subset:
                    return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0}
                wins = sum(1 for t in subset if t.get("pnl", 0) > 0)
                return {"n": len(subset), "win_rate": wins/len(subset),
                        "avg_pnl": sum(t.get("pnl",0) for t in subset)/len(subset)}

            _perf_rows = []
            for _sk, _sl in [("entry_tech","Tech"),("entry_news","News"),("entry_llm","LLM")]:
                for _bkt, _lo, _hi in [("Positive",0.20,99),("Neutral",-0.20,0.20),("Negative",-99,-0.20)]:
                    if _bkt == "Positive":
                        _sub = [t for t in _entry_trades if t.get(_sk, 0) > _lo]
                    elif _bkt == "Negative":
                        _sub = [t for t in _entry_trades if t.get(_sk, 0) < _hi]
                    else:
                        _sub = [t for t in _entry_trades if _lo <= t.get(_sk, 0) <= _hi]
                    _p = _sp(_sub)
                    if _p["n"] > 0:
                        _perf_rows.append({"Signal": f"{_sl} {_bkt}",
                                           "Win Rate": _p["win_rate"],
                                           "Avg P&L":  _p["avg_pnl"],
                                           "Trades":   _p["n"]})
            if _perf_rows:
                _df_perf = pd.DataFrame(_perf_rows)
                _fig_sp = go.Figure(go.Bar(
                    x=_df_perf["Signal"], y=_df_perf["Win Rate"],
                    marker_color=[C_BUY if v >= 0.5 else C_SELL for v in _df_perf["Win Rate"]],
                    text=[f"{v:.0%}" for v in _df_perf["Win Rate"]],
                    textposition="outside",
                    customdata=_df_perf["Trades"],
                    hovertemplate="%{x}<br>Win Rate: %{y:.0%}<br>Trades: %{customdata}<extra></extra>",
                ))
                _fig_sp.add_hline(y=0.5, line_dash="dot", line_color=C_MUTED, opacity=0.5)
                _fig_sp.update_layout(**_plotly_dark_layout(height=280,
                                                             yaxis=dict(showgrid=True, gridcolor="#21262d",
                                                                        color=C_MUTED, tickformat=".0%", range=[0,1.15])))
                st.plotly_chart(_fig_sp, use_container_width=True)

            _grp_a, _grp_b, _grp_c = st.tabs(["By Quality", "By Regime", "By Trend"])
            for _tab_grp, _key_grp, _vals_grp, _lbl_grp in [
                (_grp_a, "entry_quality", ["strong","good","weak","unknown"], "Quality"),
                (_grp_b, "entry_regime",  ["bullish","neutral","bearish","volatile","uncertain"], "Regime"),
                (_grp_c, "entry_trend",   ["up","sideways","down","neutral","unknown"], "Trend"),
            ]:
                with _tab_grp:
                    _gr_rows = []
                    for _v in _vals_grp:
                        _sub = [t for t in _entry_trades if t.get(_key_grp) == _v]
                        if _sub:
                            _p = _sp(_sub)
                            _gr_rows.append({_lbl_grp: _v.capitalize(),
                                             "Trades": _p["n"], "Win Rate": _p["win_rate"],
                                             "Avg P&L": _p["avg_pnl"]})
                    if _gr_rows:
                        st.dataframe(pd.DataFrame(_gr_rows), use_container_width=True, hide_index=True,
                            column_config={"Win Rate": st.column_config.NumberColumn(format="%.0f%%"),
                                           "Avg P&L":  st.column_config.NumberColumn(format="$+%.0f")})
        else:
            st.caption("No entry signal data in this run.")

    with _j6:
        _cycle_log = _bt_data.get("cycle_log", [])
        if not _cycle_log:
            st.caption("No cycle log available. Re-run the backtest to generate it.")
        else:
            _cl_dates = sorted(set(e.get("date","") for e in _cycle_log))
            _date_opts = ["All dates"] + _cl_dates
            _sel_cl_date = st.selectbox("Filter by date", _date_opts, key="bt_cl_date")
            _cl_filtered = (_cycle_log if _sel_cl_date == "All dates"
                            else [e for e in _cycle_log if e.get("date","") == _sel_cl_date])

            _close_pnl: dict = {}
            for t in trades:
                _close_pnl[(t["symbol"], t["closed_at"])] = t.get("pnl")

            _jl_rows = []
            for ev in _cl_filtered:
                sym = ev.get("symbol","")
                act = ev.get("action","")
                pnl_val = None
                if act in ("close","CLOSE","stop_loss","take_profit"):
                    pnl_val = _close_pnl.get((sym, ev.get("date","")))
                g = _ds.get(sym,{}).get("grade","--") if sym else "--"
                _jl_rows.append({
                    "Date": ev.get("date",""),
                    "Cycle": ev.get("cycle",""),
                    "Symbol": sym,
                    "Action": action_html(act),
                    "Qty":   ev.get("qty"),
                    "Price": ev.get("price"),
                    "P&L":   f"${pnl_val:+,.2f}" if pnl_val is not None else "--",
                    "Grade": grade_badge(g) if g not in ("--","?") else "--",
                    "Reason": (ev.get("reason") or "")[:120],
                })

            if _jl_rows:
                _jl_html = []
                for r in _jl_rows:
                    px = f"${r['Price']:,.2f}" if r["Price"] is not None else "--"
                    qx = str(int(r["Qty"])) if r["Qty"] is not None else "--"
                    pc = ""
                    if r["P&L"] != "--":
                        pc = f"color:{C_BUY};" if "$-" not in r["P&L"] else f"color:{C_SELL};"
                    _jl_html.append(
                        f"<tr>"
                        f"<td style='padding:3px 8px;color:{C_MUTED};font-size:11px'>{r['Date']}</td>"
                        f"<td style='padding:3px 8px;color:{C_MUTED}'>{r['Cycle']}</td>"
                        f"<td style='padding:3px 8px;font-weight:600'>{r['Symbol']}</td>"
                        f"<td style='padding:3px 8px'>{r['Action']}</td>"
                        f"<td style='padding:3px 8px;text-align:right'>{qx}</td>"
                        f"<td style='padding:3px 8px;text-align:right'>{px}</td>"
                        f"<td style='padding:3px 8px;text-align:right;{pc}'>{r['P&L']}</td>"
                        f"<td style='padding:3px 8px;text-align:center'>{r['Grade']}</td>"
                        f"<td style='padding:3px 8px;color:{C_MUTED};font-size:11px'>{r['Reason']}</td>"
                        f"</tr>"
                    )
                _jl_table = (
                    "<table style='width:100%;border-collapse:collapse;font-family:monospace;font-size:12px'>"
                    "<thead><tr style='border-bottom:1px solid #30363d'>"
                    "<th style='padding:3px 8px;color:#8b949e;font-size:11px'>Date</th>"
                    "<th style='padding:3px 8px;color:#8b949e;font-size:11px'>Cycle</th>"
                    "<th style='padding:3px 8px;color:#8b949e;font-size:11px'>Symbol</th>"
                    "<th style='padding:3px 8px;color:#8b949e;font-size:11px'>Action</th>"
                    "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Qty</th>"
                    "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>Price</th>"
                    "<th style='padding:3px 8px;text-align:right;color:#8b949e;font-size:11px'>P&amp;L</th>"
                    "<th style='padding:3px 8px;text-align:center;color:#8b949e;font-size:11px'>Grade</th>"
                    "<th style='padding:3px 8px;color:#8b949e;font-size:11px'>Reason</th>"
                    "</tr></thead><tbody>"
                    + "".join(_jl_html)
                    + "</tbody></table>"
                )
                st.markdown(_jl_table, unsafe_allow_html=True)
            else:
                st.caption("No events for the selected date.")


def page_bt_lessons() -> None:
    st.markdown("## Lessons")

    _bt_lessons = load_backtest_lessons()
    if _bt_lessons:
        st.markdown(_bt_lessons)
    else:
        st.caption("No backtest lessons yet.")

    _postmortems = load_backtest_postmortems()
    if _postmortems:
        st.write("")
        st.markdown('<div class="section-label">Postmortems</div>', unsafe_allow_html=True)
        _pm_rows = []
        for pm in _postmortems:
            # Filter by selected run if possible
            if _sel_run_id and pm.get("run_id") and pm.get("run_id") != _sel_run_id:
                continue
            _pm_rows.append({
                "Date":         pm.get("date", ""),
                "Symbol":       pm.get("symbol", ""),
                "P&L %":        pm.get("pnl_pct"),
                "Close Reason": pm.get("close_reason", ""),
                "Stop Verdict": pm.get("stop_verdict", ""),
                "Lesson":       (pm.get("lesson") or "")[:120],
            })
        if _pm_rows:
            st.dataframe(
                pd.DataFrame(_pm_rows), use_container_width=True, hide_index=True,
                column_config={
                    "P&L %": st.column_config.NumberColumn(format="+.2f%%"),
                },
            )
        else:
            st.caption("No postmortems for this run.")


# ===========================================================================
# PAGE ROUTING
# ===========================================================================
if is_live:
    if page == "Home":
        page_home()
    elif page == "Positions & Orders":
        page_positions_orders()
    elif page == "Signals & Gates":
        page_signals_gates(source="live")
    elif page == "Decisions Log":
        page_decisions_log(source="live")
    elif page == "Deep Scores":
        page_deep_scores()
    elif page == "Indicators":
        page_indicators(source="live")
    elif page == "Rules & Learning":
        page_rules_and_learning()
    elif page == "Lessons & Reflections":
        page_lessons_reflections()
else:
    # Backtest mode
    _run_start = _sel_run.get("start_date", "") if _sel_run else ""
    _run_end   = _sel_run.get("end_date", "")   if _sel_run else ""

    if page == "Run History":
        page_bt_run_history()
    elif page == "Equity Curve":
        page_bt_equity_curve()
    elif page == "Trade Journal":
        page_bt_trade_journal()
    elif page == "Signals & Gates":
        page_signals_gates(source="backtest", run_id=_sel_run_id)
    elif page == "Decisions Log":
        page_decisions_log(source="backtest", run_id=_sel_run_id,
                           run_start=_run_start, run_end=_run_end)
    elif page == "Deep Scores":
        page_deep_scores(note="Scores reflect the last deep scorer run.")
    elif page == "Indicators":
        page_indicators(source="backtest", run_id=_sel_run_id)
    elif page == "Lessons":
        page_bt_lessons()

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.divider()
st.caption(f"Dashboard is read-only. Data: {DATA}")
