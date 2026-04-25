"""Setup fingerprint database.

At every BUY entry, a structured "fingerprint" is recorded — a compact
description of the market conditions at the moment of entry. When the position
closes, the fingerprint is updated with the realized outcome.

At decision time the LLM advisor receives a similarity query: "The N most
similar past setups to what we're seeing now had an avg edge of X% with Y%
hit rate." This turns vague prose memory into evidence-based pattern recall.

Fingerprint schema (appended to data/setup_fingerprints.jsonl):
  {
    "id": "AAPL_20260422_0930",
    "date": "2026-04-22",
    "cycle": "09:30",
    "symbol": "AAPL",
    "regime": "bullish",
    "trend": "uptrend",
    "quality": "strong",
    "rsi_zone": "neutral",       # overbought / oversold / neutral
    "macd_state": "bullish",     # bullish / bearish / neutral
    "breadth_zone": "positive",  # positive / negative / neutral
    "price_range": "large_cap",  # small_cap / mid_cap / large_cap
    "gap_up": false,
    "tech_score": 0.72,
    "news_score": 0.15,
    "combined_score": 0.55,
    "entry_price": 185.20,
    # Filled at close:
    "closed": true,
    "close_date": "2026-04-22",
    "close_reason": "take_profit",
    "pnl_pct": 0.021,
    "hold_duration": "3h30m"
  }
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)

_FINGERPRINTS_FILE = "data/setup_fingerprints.jsonl"


# ------------------------------------------------------------------ public API

def record_entry_fingerprint(
    symbol: str,
    cycle: str,
    decision: dict,
    entry_price: float,
    as_of_date=None,
    db_file: str | None = None,
) -> str:
    """Record a fingerprint at BUY entry. Returns the fingerprint id.

    Pass as_of_date when calling from backtest (stamps with simulated date).
    Pass db_file to write to a different store (e.g. backtest-specific file).
    """
    date_str = as_of_date.isoformat() if as_of_date is not None else today_str()
    fp_id = f"{symbol}_{date_str.replace('-','')}_{cycle.replace(':','')}"
    fp = {
        "id": fp_id,
        "date": date_str,
        "cycle": cycle,
        "symbol": symbol,
        "regime": _get_nested(decision, "regime", "label") or "unknown",
        "trend": _get_nested(decision, "trend", "label") or "unknown",
        "quality": _get_nested(decision, "quality", "label") or "unknown",
        "rsi_zone": _rsi_zone(_get_signal_detail(decision, "technicals", "rsi_score")),
        "macd_state": _macd_state(_get_signal_detail(decision, "technicals", "macd_score")),
        "breadth_zone": _breadth_zone(_get_signal(decision, "breadth")),
        "price_range": _price_range(entry_price),
        "gap_up": bool(decision.get("gap_up", False)),
        "tech_score": _round(_get_signal(decision, "technicals")),
        "news_score": _round(_get_signal(decision, "news")),
        "combined_score": _round(decision.get("combined_score")),
        "entry_price": round(float(entry_price), 4),
        "closed": False,
        "close_date": None,
        "close_reason": None,
        "pnl_pct": None,
        "hold_duration": None,
    }
    _append(fp, dest=db_file)
    log.debug(f"[setup-memory] fingerprint recorded: {fp_id}")
    return fp_id


def record_close_outcome(
    symbol: str,
    close_price: float,
    close_reason: str,
    entry_price: float,
    entry_datetime_str: str,
    as_of_date=None,
    db_file: str | None = None,
) -> bool:
    """Update the most recent open fingerprint for symbol with close data.

    Returns True if a matching fingerprint was found and updated.
    Pass as_of_date when calling from backtest.
    Pass db_file to read/write a different store (e.g. backtest-specific file).
    """
    path = Path(db_file if db_file else _FINGERPRINTS_FILE)
    if not path.exists():
        return False

    lines = path.read_text(encoding="utf-8").splitlines()
    pnl_pct = (close_price - entry_price) / entry_price if entry_price else 0.0
    hold_dur = _hold_duration(entry_datetime_str)
    close_date = as_of_date.isoformat() if as_of_date is not None else today_str()

    updated = False
    new_lines: list[str] = []
    # Walk in reverse to find the most recent open fingerprint for this symbol
    target_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
            if row.get("symbol") == symbol and not row.get("closed"):
                target_idx = i
                break
        except json.JSONDecodeError:
            continue

    if target_idx is None:
        return False

    for i, line in enumerate(lines):
        if i == target_idx:
            try:
                row = json.loads(line)
                row["closed"] = True
                row["close_date"] = close_date
                row["close_reason"] = close_reason[:100]
                row["pnl_pct"] = round(pnl_pct, 4)
                row["hold_duration"] = hold_dur
                new_lines.append(json.dumps(row, default=str))
                updated = True
            except json.JSONDecodeError:
                new_lines.append(line)
        else:
            new_lines.append(line)

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    log.debug(f"[setup-memory] fingerprint closed: {symbol} pnl={pnl_pct:+.2%}")
    return updated


def find_similar_setups(
    decision: dict,
    entry_price: float,
    window_days: int = 60,
    min_matches: int = 3,
    max_results: int = 5,
    db_file: str | None = None,
) -> dict:
    """Find past closed setups most similar to the current decision.

    Similarity is scored by counting matching categorical fields. Returns a
    dict with stats if enough matches are found, otherwise empty dict.
    Pass db_file to search a different store (e.g. backtest-specific file).
    """
    path = Path(db_file if db_file else _FINGERPRINTS_FILE)
    if not path.exists():
        return {}

    query = {
        "regime": _get_nested(decision, "regime", "label") or "unknown",
        "trend": _get_nested(decision, "trend", "label") or "unknown",
        "quality": _get_nested(decision, "quality", "label") or None,
        "rsi_zone": _rsi_zone(_get_signal_detail(decision, "technicals", "rsi_score")),
        "macd_state": _macd_state(_get_signal_detail(decision, "technicals", "macd_score")),
        "breadth_zone": _breadth_zone(_get_signal(decision, "breadth")),
        "price_range": _price_range(entry_price) if entry_price > 0 else None,
        "gap_up": bool(decision.get("gap_up", False)),
    }

    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()

    candidates: list[tuple[int, dict]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("closed") or (row.get("date") or "") < cutoff:
                continue
            score = _similarity_score(query, row)
            candidates.append((score, row))

    if not candidates:
        return {}

    candidates.sort(key=lambda x: -x[0])
    top = [row for _, row in candidates[:max(min_matches * 2, max_results * 3)]]

    # Only use matches with at least half the fields matching (4 of 8)
    threshold = 4
    close_matches = [(s, r) for s, r in candidates if s >= threshold]
    if len(close_matches) < min_matches:
        return {}

    pnls = [r["pnl_pct"] for _, r in close_matches if r.get("pnl_pct") is not None]
    if not pnls:
        return {}

    hits = sum(1 for p in pnls if p > 0)
    avg_edge = sum(pnls) / len(pnls)
    return {
        "match_count": len(close_matches),
        "hit_rate": round(hits / len(pnls), 3),
        "avg_edge": round(avg_edge, 4),
        "sample_symbols": [r["symbol"] for _, r in close_matches[:3]],
        "query": query,
    }


def format_similarity_block(match: dict) -> str:
    """Format similar-setup results as a one-liner for the advisor prompt."""
    if not match:
        return ""
    n = match["match_count"]
    hr = match["hit_rate"]
    ae = match["avg_edge"]
    syms = ", ".join(match.get("sample_symbols", []))
    return (
        f"Similar past setups ({n} matches, 60d): "
        f"hit rate {hr:.0%} | avg edge {ae:+.2%} "
        f"(e.g. {syms})"
    )


# ------------------------------------------------------------------ internals

def _append(fp: dict, dest: str | None = None) -> None:
    path = Path(dest if dest else _FINGERPRINTS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(fp, default=str) + "\n")


def _similarity_score(query: dict, row: dict) -> int:
    fields = ["regime", "trend", "quality", "rsi_zone",
              "macd_state", "breadth_zone", "price_range", "gap_up"]
    # Skip fields where query value is None (unknown/unavailable)
    return sum(1 for f in fields if query.get(f) is not None and query.get(f) == row.get(f))


def _rsi_zone(score) -> str:
    if score is None:
        return "unknown"
    s = float(score)
    if s >= 0.5:
        return "overbought"
    if s <= -0.5:
        return "oversold"
    return "neutral"


def _macd_state(score) -> str:
    if score is None:
        return "unknown"
    s = float(score)
    if s > 0.1:
        return "bullish"
    if s < -0.1:
        return "bearish"
    return "neutral"


def _breadth_zone(score) -> str:
    if score is None:
        return "unknown"
    s = float(score)
    if s > 0.2:
        return "positive"
    if s < -0.2:
        return "negative"
    return "neutral"


def _price_range(price: float) -> str:
    if price <= 15:
        return "small_cap"
    if price <= 100:
        return "mid_cap"
    return "large_cap"


def _round(v) -> float | None:
    try:
        return round(float(v), 3) if v is not None else None
    except Exception:
        return None


def _get_signal(decision: dict, key: str) -> float | None:
    sigs = decision.get("signals") or {}
    v = sigs.get(key) or {}
    s = v.get("score") if isinstance(v, dict) else None
    return float(s) if s is not None else None


def _get_signal_detail(decision: dict, signal_key: str, detail_key: str) -> float | None:
    sigs = decision.get("signals") or {}
    sig = sigs.get(signal_key) or {}
    details = sig.get("details") if isinstance(sig, dict) else {}
    if not details:
        return None
    v = details.get(detail_key)
    return float(v) if v is not None else None


def _get_nested(d: dict, *keys) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _hold_duration(entry_dt_str: str) -> str:
    if not entry_dt_str:
        return ""
    try:
        entry_dt = datetime.fromisoformat(entry_dt_str)
        mins = max(0, int((datetime.utcnow() - entry_dt).total_seconds() // 60))
        if mins >= 60:
            return f"{mins // 60}h{mins % 60}m"
        return f"{mins}m"
    except Exception:
        return ""
