"""Per-sub-indicator performance tracking.

After each EOD reflection this module:
  1. Extracts individual indicator sub-scores from the day's journal entries
     (details stored in decision.signals.technicals.details).
  2. Joins them with the graded outcome (edge, hit) for that decision.
  3. Appends one row per (decision, indicator) to indicator_outcomes.jsonl.
  4. Recomputes rolling stats (hit rate, avg edge, Pearson correlation) for
     each indicator and writes indicator_stats.json for the dashboard.

Tracked indicators: rsi, macd, trend, bb, obv, vwap, fib
  - rsi / macd / trend scores are now stored in technicals.details directly.
  - bb / obv / vwap / fib were already stored (may be None when data is missing).
  - ADX is folded into the trend score via adx_factor and is not tracked
    separately -- trend_score already reflects ADX-weighted confidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)

INDICATORS = ["rsi", "macd", "trend", "bb", "obv", "vwap", "fib", "roc", "rs_etf"]

_DETAIL_KEY = {
    "rsi":    "rsi_score",
    "macd":   "macd_score",
    "trend":  "trend_score",
    "bb":     "bb_score",
    "obv":    "obv_score",
    "vwap":   "vwap_score",
    "fib":    "fib_score",
    "roc":    "roc_score",
    "rs_etf": "rs_etf_score",
}


def extract_indicator_outcomes(
    entries: list[dict[str, Any]],
    graded_outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join journal entries with graded outcomes, emitting one row per (entry, indicator).

    Only emits rows where the indicator score is not None.
    Returns an empty list when there are no technicals details to extract.
    """
    edge_lookup: dict[tuple, dict] = {}
    for o in graded_outcomes:
        key = (o.get("symbol"), o.get("cycle"), o.get("date"))
        out = o.get("outcome") or {}
        edge_lookup[key] = {
            "edge": out.get("edge"),
            "hit":  out.get("hit"),
        }

    rows: list[dict[str, Any]] = []
    for entry in entries:
        decision = entry.get("decision") or {}
        sym = decision.get("symbol") or entry.get("symbol")
        if not sym:
            continue

        signals = decision.get("signals") or {}
        tech = signals.get("technicals") or {}
        details = tech.get("details") if isinstance(tech, dict) else {}
        if not details:
            continue

        ts = entry.get("timestamp") or ""
        date_part = ts[:10] if ts else ""
        cycle = entry.get("cycle") or ""
        action = decision.get("action") or "HOLD"
        regime_obj = decision.get("regime") or entry.get("regime") or {}
        regime = regime_obj.get("label") if isinstance(regime_obj, dict) else str(regime_obj)

        outcome_data = edge_lookup.get((sym, cycle, date_part)) or {}
        edge = outcome_data.get("edge")
        hit  = outcome_data.get("hit")

        for ind in INDICATORS:
            score = details.get(_DETAIL_KEY[ind])
            if score is None:
                continue
            rows.append({
                "date":      date_part,
                "symbol":    sym,
                "cycle":     cycle,
                "action":    action,
                "regime":    regime,
                "indicator": ind,
                "score":     float(score),
                "edge":      float(edge) if edge is not None else None,
                "hit":       hit,
            })

    return rows


def append_indicator_outcomes(rows: list[dict[str, Any]]) -> None:
    """Append rows to indicator_outcomes.jsonl (creates file if missing)."""
    if not rows:
        return
    cfg = load_config()
    path = Path(cfg["paths"].get("indicator_outcomes_file", "data/indicator_outcomes.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


def compute_indicator_stats(window_days: int = 30) -> dict[str, Any]:
    """Read indicator_outcomes.jsonl and compute rolling per-indicator stats.

    Returns a dict ready for save_indicator_stats().
    """
    cfg = load_config()
    path = Path(cfg["paths"].get("indicator_outcomes_file", "data/indicator_outcomes.jsonl"))

    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    buckets: dict[str, list[dict]] = {ind: [] for ind in INDICATORS}

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("date") or "") < cutoff:
                    continue
                ind = row.get("indicator")
                if ind in buckets:
                    buckets[ind].append(row)

    indicators: dict[str, dict] = {}
    for ind, bucket in buckets.items():
        if not bucket:
            indicators[ind] = {"samples": 0, "hit_rate": None, "avg_edge": None, "correlation": None}
            continue

        scores = [r["score"] for r in bucket if r.get("score") is not None]
        edges  = [r["edge"]  for r in bucket if r.get("edge")  is not None]
        hits   = [r["hit"]   for r in bucket if r.get("hit")   is not None]

        samples  = len(scores)
        hit_rate = (sum(1 for h in hits if h is True) / len(hits)) if hits else None
        avg_edge = (sum(edges) / len(edges)) if edges else None

        correlation: float | None = None
        paired = [(s, e) for r in bucket
                  if r.get("score") is not None and r.get("edge") is not None
                  for s, e in [(r["score"], r["edge"])]]
        if len(paired) >= 5:
            xs = [p[0] for p in paired]
            ys = [p[1] for p in paired]
            correlation = _pearson(xs, ys)

        indicators[ind] = {
            "samples":     samples,
            "hit_rate":    round(hit_rate, 4) if hit_rate is not None else None,
            "avg_edge":    round(avg_edge, 6) if avg_edge is not None else None,
            "correlation": round(correlation, 4) if correlation is not None else None,
        }

    return {
        "updated":     datetime.utcnow().strftime("%Y-%m-%d"),
        "window_days": window_days,
        "indicators":  indicators,
    }


def save_indicator_stats(stats: dict[str, Any]) -> None:
    """Write indicator_stats.json."""
    cfg = load_config()
    path = Path(cfg["paths"].get("indicator_stats_file", "data/indicator_stats.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")


def load_indicator_stats() -> dict[str, Any]:
    """Read indicator_stats.json. Returns {} on missing or corrupt file."""
    cfg = load_config()
    path = Path(cfg["paths"].get("indicator_stats_file", "data/indicator_stats.json"))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Simple Pearson correlation coefficient. Returns None on degenerate input."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = sum((x - mx) ** 2 for x in xs) ** 0.5
    den_y = sum((y - my) ** 2 for y in ys) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)
