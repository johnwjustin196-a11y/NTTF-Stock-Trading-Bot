"""Intraday outcome grading.

For each journal entry (decision made at a specific timestamp during the
session), this module computes what actually happened in the hours AFTER that
decision — not the whole-day close-to-close move, which is what the old crude
grader used.

The grading uses 5-minute bars from yfinance. For an entry made at 12:00 PM:
  - price_at_decision: first 5m bar at or after the decision timestamp
  - price_at_eod:      last bar of the session
  - pct_to_eod:        the signed % move between those two
  - max_favorable_pct: biggest move in the direction the decision bet on
  - max_adverse_pct:   biggest move against the decision
  - For executed BUYs: stop_hit / tp_hit — did the price ever trade through
    the stop or take-profit between entry and close?

"Edge" is the magnitude-and-direction score used as the single-number grade,
always expressed as "% move in the direction the bot bet on." So a BUY that
ended +1.2% has edge=+0.012; a BUY that ended -0.5% has edge=-0.005; a CLOSE
(short-direction bet for grading purposes) of a stock that then rose 2% has
edge=-0.02. This is what downstream analytics (rules scoring, per-ticker
track record, signal-weight auto-tuner) consume.

Outputs are appended to data/outcomes.jsonl — one line per decision. That
file is the source of truth for the dashboard's learning panels.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)


# ----------------------------------------------------------- public entrypoints

def grade_journal_entries(
    entries: list[dict[str, Any]],
    *,
    date_str: str | None = None,
) -> list[dict[str, Any]]:
    """Grade every decision in `entries`, returning the enriched outcome rows.

    Skips entries that aren't proper decisions (e.g. "flatten" event rows,
    "trailing_stop_update" rows). Groups by symbol so we only fetch each
    ticker's intraday bars once per EOD run, then walks each decision for
    that ticker against the fetched bars.
    """
    # Group decisions by symbol for efficient bar fetching
    by_symbol: dict[str, list[dict]] = {}
    for e in entries:
        d = e.get("decision") or {}
        sym = d.get("symbol") or e.get("symbol")
        # Only grade rows that carry a decision; skip 'flatten' / 'trailing_*' events
        if not sym or not d:
            continue
        by_symbol.setdefault(sym, []).append(e)

    outcomes: list[dict[str, Any]] = []
    for sym, sym_entries in by_symbol.items():
        bars = _fetch_intraday_bars(sym, date_str=date_str)
        if bars is None or bars.empty:
            log.debug(f"outcomes: no intraday bars for {sym} — skipping {len(sym_entries)} entries")
            # We can still emit an outcome row with null grading so the dashboard
            # knows the decision existed; downstream code tolerates nulls.
            for e in sym_entries:
                outcomes.append(_outcome_row(e, bars=None))
            continue
        for e in sym_entries:
            outcomes.append(_outcome_row(e, bars=bars))
    return outcomes


def append_outcomes(outcomes: list[dict[str, Any]]) -> Path:
    """Append graded outcomes to data/outcomes.jsonl (one JSON row per line).

    Returns the path written to. Safe to call with an empty list.
    """
    cfg = load_config()
    path = Path(cfg["paths"]["outcomes_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if not outcomes:
        return path
    with open(path, "a", encoding="utf-8") as f:
        for row in outcomes:
            f.write(json.dumps(row, default=str) + "\n")
    return path


def load_outcomes(
    *,
    since_days: int | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Read outcomes.jsonl. Optional filters for window and ticker.

    The file is append-only; we scan linearly and filter. For the sizes this
    bot produces (~250 decisions/day), a full scan is fine and avoids having
    to maintain an index.
    """
    cfg = load_config()
    path = Path(cfg["paths"]["outcomes_file"])
    if not path.exists():
        return []
    cutoff_date: str | None = None
    if since_days is not None:
        cutoff_date = (datetime.utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if symbol and row.get("symbol") != symbol:
                continue
            if cutoff_date and (row.get("date") or "") < cutoff_date:
                continue
            out.append(row)
    return out


def regime_win_rates(window_days: int = 60) -> dict:
    """Return BUY win rates grouped by regime over the last window_days.

    Schema:
      {
        "bullish": {"total": 45, "hits": 30, "hit_rate": 0.67, "avg_edge": 0.018},
        "bearish": {"total": 18, "hits": 6, "hit_rate": 0.33, "avg_edge": -0.009},
        ...
      }
    Only includes BUY decisions with graded outcomes.
    """
    rows = load_outcomes(since_days=window_days)
    by_regime: dict[str, dict] = {}
    for r in rows:
        if r.get("action") != "BUY":
            continue
        out = r.get("outcome") or {}
        edge = out.get("edge")
        hit = out.get("hit")
        if edge is None or hit is None:
            continue
        regime = (r.get("regime") or "unknown").lower()
        if regime not in by_regime:
            by_regime[regime] = {"total": 0, "hits": 0, "edge_sum": 0.0}
        by_regime[regime]["total"] += 1
        if hit is True:
            by_regime[regime]["hits"] += 1
        by_regime[regime]["edge_sum"] += float(edge)

    return {
        k: {
            "total": v["total"],
            "hits": v["hits"],
            "hit_rate": round(v["hits"] / v["total"], 3) if v["total"] else None,
            "avg_edge": round(v["edge_sum"] / v["total"], 4) if v["total"] else None,
        }
        for k, v in by_regime.items()
    }


def hold_counterfactuals(window_days: int = 30, min_missed_edge: float = 0.02) -> list[dict]:
    """Return HOLD decisions where the market moved significantly afterward.

    A HOLD on a stock that then moved >min_missed_edge (default 2%) is a
    "missed opportunity" if it went up, or a "good pass" if it went down.
    Returns the top 20 by absolute missed edge, most recent first.

    Used in EOD reflection and advisor context to show the bot where it was
    too conservative or correctly cautious.
    """
    rows = load_outcomes(since_days=window_days)
    out: list[dict] = []
    for r in rows:
        if r.get("action") != "HOLD":
            continue
        out_data = r.get("outcome") or {}
        edge = out_data.get("edge")
        pct = out_data.get("pct_to_eod")
        if edge is None or pct is None:
            continue
        abs_move = abs(float(pct))
        if abs_move < min_missed_edge:
            continue
        out.append({
            "date": r.get("date", ""),
            "symbol": r.get("symbol", ""),
            "cycle": r.get("cycle", ""),
            "combined_score": r.get("combined_score"),
            "regime": r.get("regime"),
            "pct_to_eod": float(pct),
            "abs_move": abs_move,
            "direction": "up" if float(pct) > 0 else "down",
            "missed_gain": float(pct) > 0,
            "reason": (r.get("reason") or "")[:120],
        })
    out.sort(key=lambda x: (-abs(x["pct_to_eod"]), x["date"]), reverse=False)
    # Sort by date desc, then by abs move desc within same date
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:20]


def cycle_win_rates(window_days: int = 30) -> dict:
    """Return win rates and avg edge grouped by (cycle, regime) over the last window_days.

    Schema:
      {
        "by_cycle": {
          "09:30": {"total": 12, "hits": 8, "hit_rate": 0.67, "avg_edge": 0.012},
          ...
        },
        "by_cycle_regime": {
          "09:30|bullish": {"total": 7, "hits": 5, "hit_rate": 0.71, "avg_edge": 0.018},
          ...
        }
      }
    Only includes BUY decisions with graded outcomes.
    """
    rows = load_outcomes(since_days=window_days)
    by_cycle: dict[str, dict] = {}
    by_cycle_regime: dict[str, dict] = {}

    for r in rows:
        if r.get("action") != "BUY":
            continue
        out = r.get("outcome") or {}
        edge = out.get("edge")
        hit = out.get("hit")
        if edge is None or hit is None:
            continue
        cycle = r.get("cycle") or "?"
        regime = r.get("regime") or "unknown"
        cr_key = f"{cycle}|{regime}"

        for bucket, key in [(by_cycle, cycle), (by_cycle_regime, cr_key)]:
            if key not in bucket:
                bucket[key] = {"total": 0, "hits": 0, "edge_sum": 0.0}
            bucket[key]["total"] += 1
            if hit is True:
                bucket[key]["hits"] += 1
            bucket[key]["edge_sum"] += float(edge)

    def _finalize(raw: dict) -> dict:
        out: dict = {}
        for k, v in raw.items():
            total = v["total"]
            hits = v["hits"]
            out[k] = {
                "total": total,
                "hits": hits,
                "hit_rate": round(hits / total, 3) if total else None,
                "avg_edge": round(v["edge_sum"] / total, 4) if total else None,
            }
        return out

    return {
        "by_cycle": _finalize(by_cycle),
        "by_cycle_regime": _finalize(by_cycle_regime),
        "window_days": window_days,
    }


# ------------------------------------------------------------- internal helpers

def _outcome_row(entry: dict[str, Any], bars: pd.DataFrame | None) -> dict[str, Any]:
    decision = entry.get("decision") or {}
    executed = entry.get("executed") or {}
    regime = decision.get("regime") or entry.get("regime") or {}
    quality = decision.get("quality") or {}
    trend = decision.get("trend") or {}

    sym = decision.get("symbol") or entry.get("symbol")
    action = decision.get("action") or "HOLD"
    decision_ts = entry.get("timestamp") or ""
    date_part = decision_ts[:10] if decision_ts else ""
    cycle = entry.get("cycle") or ""

    base: dict[str, Any] = {
        "date": date_part,
        "symbol": sym,
        "cycle": cycle,
        "decision_time": decision_ts,
        "action": action,
        "combined_score": decision.get("combined_score", 0.0),
        "quality": quality.get("label") if isinstance(quality, dict) else quality,
        "trend": trend.get("label") if isinstance(trend, dict) else trend,
        "regime": regime.get("label") if isinstance(regime, dict) else regime,
        "regime_score": regime.get("score") if isinstance(regime, dict) else None,
        "reason": (decision.get("reason") or "")[:400],
        "signals": _signals_snapshot(decision.get("signals") or {}),
        "executed": bool(executed),
        "entry_price": executed.get("filled_price") if executed else None,
        "stop": executed.get("stop_loss") if executed else None,
        "tp": executed.get("take_profit") if executed else None,
    }

    if bars is None or bars.empty:
        base["outcome"] = None
        return base

    grading = _grade_against_bars(
        bars=bars,
        decision_ts=decision_ts,
        action=action,
        entry_price=base.get("entry_price"),
        stop=base.get("stop"),
        tp=base.get("tp"),
    )
    base["outcome"] = grading
    return base


def _signals_snapshot(signals: dict) -> dict:
    """Pull just the scalar score from each signal so outcomes.jsonl stays small
    and is usable for correlation work in the signal-weight tuner."""
    out: dict[str, Any] = {}
    for k in ("technicals", "news", "breadth", "llm"):
        v = signals.get(k) or {}
        if isinstance(v, dict):
            score = v.get("score")
            if isinstance(score, (int, float)):
                out[k] = float(score)
    return out


def _fetch_intraday_bars(symbol: str, *, date_str: str | None) -> pd.DataFrame | None:
    """Fetch 5-minute bars covering the decision day. yfinance only serves
    intraday history for the last ~60 days; older grading falls back to null.
    """
    try:
        # period='5d' with interval='5m' reliably covers today + a few prior days,
        # which is what we need for the EOD job at 4:30pm.
        bars = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=False)
    except Exception as e:
        log.debug(f"intraday bar fetch for {symbol} failed: {e}")
        return None
    if bars is None or bars.empty:
        return None
    # Filter to the target date if provided. yfinance returns tz-aware index.
    if date_str:
        try:
            idx = bars.index
            if getattr(idx, "tz", None) is not None:
                # Convert to Eastern for date match since decisions are stamped in ET
                bars = bars.tz_convert("America/New_York")
            bars = bars[bars.index.strftime("%Y-%m-%d") == date_str]
        except Exception:
            pass
    return bars


def _grade_against_bars(
    *,
    bars: pd.DataFrame,
    decision_ts: str,
    action: str,
    entry_price: float | None,
    stop: float | None,
    tp: float | None,
) -> dict[str, Any] | None:
    """Compute the grading payload for one decision against the session's bars.

    Uses the first bar at-or-after the decision timestamp as the grading
    baseline. If the decision was made before the session opened, the baseline
    is the session's first bar.
    """
    if bars.empty:
        return None
    try:
        dt = _parse_decision_ts(decision_ts)
    except Exception:
        dt = None

    idx = bars.index
    if dt is not None and getattr(idx, "tz", None) is not None:
        # Make sure both sides are tz-aware in the same tz. yfinance bars are
        # already tz-aware; our decision timestamp is ISO-8601 with offset.
        try:
            if dt.tzinfo is None:
                dt = idx.tz.localize(dt)
            after = bars[bars.index >= dt]
        except Exception:
            after = bars
    else:
        after = bars

    if after.empty:
        after = bars  # fall back to full session if timestamp parsing failed

    baseline_bar = after.iloc[0]
    baseline_px = float(baseline_bar["Close"])
    last_px = float(after.iloc[-1]["Close"])
    highs = after["High"].astype(float)
    lows = after["Low"].astype(float)

    # Favorable/adverse depends on which direction the bot bet on
    sign = _direction_sign(action)  # +1 for bullish bet, -1 for bearish, 0 for HOLD
    pct_to_eod = (last_px - baseline_px) / baseline_px if baseline_px else 0.0
    # Max favorable excursion: biggest move in the bet direction
    if sign >= 0:
        mfe = float((highs.max() - baseline_px) / baseline_px) if baseline_px else 0.0
        mae = float((lows.min() - baseline_px) / baseline_px) if baseline_px else 0.0
    else:
        mfe = float((baseline_px - lows.min()) / baseline_px) if baseline_px else 0.0
        mae = float((baseline_px - highs.max()) / baseline_px) if baseline_px else 0.0

    # Stop/TP detection for executed BUYs
    stop_hit = False
    tp_hit = False
    realized_pct: float | None = None
    if entry_price and stop and stop > 0:
        if (lows <= stop).any():
            stop_hit = True
            realized_pct = float((stop - entry_price) / entry_price)
    if entry_price and tp and tp > 0:
        if (highs >= tp).any():
            tp_hit = True
            # If both hit on the same day, assume the earlier one wins. Walk bars
            # in order and pick whichever triggers first.
            if stop_hit:
                first = _first_trigger_time(highs, lows, tp=tp, stop=stop)
                if first == "tp":
                    realized_pct = float((tp - entry_price) / entry_price)
                    stop_hit = False  # tp fired first this session
                else:
                    tp_hit = False
            else:
                realized_pct = float((tp - entry_price) / entry_price)

    # If executed but neither stop nor tp hit, realized = mark-to-close from entry
    if entry_price and realized_pct is None and (stop_hit or tp_hit) is False:
        realized_pct = float((last_px - entry_price) / entry_price) if entry_price else None

    # Directional hit (yes/no) — for HOLD, we say "hit" when the day was quiet
    if action == "HOLD":
        hit: bool | None = abs(pct_to_eod) < 0.01
    elif sign == 0:
        hit = None
    else:
        hit = (pct_to_eod * sign) > 0

    edge = float(pct_to_eod * sign) if sign != 0 else 0.0

    # Stop verdict: did the stop fire at the right time or was it too tight?
    stop_verdict: str | None = None
    if stop_hit and entry_price:
        post_move = pct_to_eod  # move from baseline to EOD
        if post_move > 0.01:
            stop_verdict = "too_tight"   # stock recovered — stop fired too early
        elif post_move < -0.01:
            stop_verdict = "correct"     # stock kept falling — stop was right
        else:
            stop_verdict = "ambiguous"

    # Close verdict (signal-driven exits): did the stock keep falling after we closed?
    close_verdict: str | None = None
    if action in ("CLOSE", "SELL") and not stop_hit and entry_price:
        post_move = pct_to_eod  # move after the close decision
        if post_move < -0.01:
            close_verdict = "correct"   # stock fell — good call
        elif post_move > 0.01:
            close_verdict = "early"     # stock rose — closed too soon
        else:
            close_verdict = "neutral"

    return {
        "bars_used": int(len(after)),
        "price_at_decision": baseline_px,
        "price_at_eod": last_px,
        "pct_to_eod": float(pct_to_eod),
        "max_favorable_pct": mfe,
        "max_adverse_pct": mae,
        "stop_hit": bool(stop_hit),
        "tp_hit": bool(tp_hit),
        "realized_pct": realized_pct,
        "hit": hit,
        "edge": edge,
        "stop_verdict": stop_verdict,
        "close_verdict": close_verdict,
    }


def _first_trigger_time(highs, lows, *, tp: float, stop: float) -> str:
    """Given aligned high/low series, return which trigger fires first: 'tp' or 'stop'.

    Ties (both trigger on the same bar) resolve to 'stop' because that's the
    more conservative assumption — we credit the worse outcome.
    """
    for i in range(len(highs)):
        h = float(highs.iloc[i])
        l = float(lows.iloc[i])
        stop_this_bar = l <= stop
        tp_this_bar = h >= tp
        if stop_this_bar and tp_this_bar:
            return "stop"
        if stop_this_bar:
            return "stop"
        if tp_this_bar:
            return "tp"
    return "tp"  # shouldn't reach — caller only invokes when both fired overall


def _direction_sign(action: str) -> int:
    if action == "BUY":
        return +1
    if action in ("SELL", "CLOSE"):
        return -1
    return 0


def _parse_decision_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        try:
            # Sometimes the JSON write strips tzinfo — try naive parse
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
