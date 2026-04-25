"""Per-ticker track record built from data/outcomes.jsonl.

Lets the advisor see "here's how the last N decisions on AAPL actually played
out" — hit rate, avg edge, how often the stop hit, biggest winner/loser.
Injected into the advisor system prompt so each ticker gets its own history
rather than only the market-wide lessons.

The dashboard also uses this for the per-ticker panel.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from .outcomes import load_outcomes


def ticker_track_record(
    symbol: str,
    *,
    window_days: int | None = None,
    min_samples: int | None = None,
) -> dict[str, Any]:
    """Compute a compact track record for one ticker.

    Returns a dict with enough fields to render a one-liner into the advisor
    prompt and to fill a dashboard row. Returns an empty-ish dict when there
    aren't enough samples — the advisor then knows "no history, use signals".
    """
    cfg = load_config()
    lcfg = (cfg.get("learning") or {})
    if window_days is None:
        window_days = int(lcfg.get("ticker_track_window_days", 60))
    if min_samples is None:
        min_samples = int(lcfg.get("ticker_track_min_samples", 3))

    rows = load_outcomes(since_days=window_days, symbol=symbol)
    # Only keep rows that were actually graded
    graded = [r for r in rows if r.get("outcome")]
    n = len(graded)
    empty = {
        "symbol": symbol,
        "samples": n,
        "window_days": window_days,
        "has_history": n >= min_samples,
        "summary_line": "",
    }
    if n < min_samples:
        if n == 0:
            empty["summary_line"] = "no prior decisions in window"
        else:
            empty["summary_line"] = f"only {n} prior decision(s) — not enough history"
        return empty

    hits = sum(1 for r in graded if (r.get("outcome") or {}).get("hit") is True)
    buys = [r for r in graded if r.get("action") == "BUY"]
    closes = [r for r in graded if r.get("action") in ("CLOSE", "SELL")]
    holds = [r for r in graded if r.get("action") == "HOLD"]

    def _avg_edge(subset: list[dict]) -> float:
        if not subset:
            return 0.0
        return sum((r.get("outcome") or {}).get("edge", 0) or 0 for r in subset) / len(subset)

    buy_hits = sum(1 for r in buys if (r.get("outcome") or {}).get("hit") is True)
    buy_stop_hits = sum(1 for r in buys if (r.get("outcome") or {}).get("stop_hit"))
    buy_tp_hits = sum(1 for r in buys if (r.get("outcome") or {}).get("tp_hit"))

    # Find best and worst realized trades (executed only)
    executed = [r for r in graded if r.get("executed")]
    best = max(
        executed,
        key=lambda r: (r.get("outcome") or {}).get("realized_pct") or -999,
        default=None,
    )
    worst = min(
        executed,
        key=lambda r: (r.get("outcome") or {}).get("realized_pct") or 999,
        default=None,
    )

    hit_rate = hits / n if n else 0.0
    avg_edge = _avg_edge(graded)
    summary_line = (
        f"{n} decisions over {window_days}d: "
        f"hit rate {hit_rate:.0%}, avg edge {avg_edge:+.2%} "
        f"({len(buys)} BUY / {len(closes)} CLOSE / {len(holds)} HOLD"
        + (f"; stops hit {buy_stop_hits}, TPs hit {buy_tp_hits}" if buys else "")
        + ")"
    )

    return {
        "symbol": symbol,
        "samples": n,
        "window_days": window_days,
        "has_history": True,
        "hit_rate": hit_rate,
        "avg_edge": avg_edge,
        "buys": len(buys),
        "closes": len(closes),
        "holds": len(holds),
        "buy_hits": buy_hits,
        "buy_hit_rate": (buy_hits / len(buys)) if buys else None,
        "buy_avg_edge": _avg_edge(buys),
        "stop_hits": buy_stop_hits,
        "tp_hits": buy_tp_hits,
        "best_realized": (best.get("outcome") or {}).get("realized_pct") if best else None,
        "worst_realized": (worst.get("outcome") or {}).get("realized_pct") if worst else None,
        "summary_line": summary_line,
    }


def symbol_on_cooldown(symbol: str, *, window_days: int = 15, max_stops: int = 3) -> bool:
    cutoff = date.today() - timedelta(days=window_days)
    cfg = load_config()
    path = Path(cfg["paths"]["outcomes_file"])
    if not path.exists():
        return False
    stop_count = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("symbol", "").upper() != symbol.upper():
                continue
            try:
                rec_date = date.fromisoformat(rec.get("date", "2000-01-01"))
            except Exception:
                continue
            if rec_date < cutoff:
                continue
            if rec.get("stop_hit"):
                stop_count += 1
    return stop_count >= max_stops


def all_ticker_track_records(
    *,
    window_days: int | None = None,
    min_samples: int = 1,
) -> list[dict]:
    """Bulk variant for the dashboard: one row per ticker that appears in
    outcomes.jsonl within the window. Cheaper than calling `ticker_track_record`
    per symbol because we scan the file once.
    """
    cfg = load_config()
    lcfg = cfg.get("learning") or {}
    if window_days is None:
        window_days = int(lcfg.get("ticker_track_window_days", 60))

    rows = load_outcomes(since_days=window_days)
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym or not r.get("outcome"):
            continue
        by_symbol.setdefault(sym, []).append(r)

    out: list[dict] = []
    for sym, sym_rows in by_symbol.items():
        if len(sym_rows) < min_samples:
            continue
        hits = sum(1 for r in sym_rows if (r.get("outcome") or {}).get("hit") is True)
        edges = [(r.get("outcome") or {}).get("edge", 0) or 0 for r in sym_rows]
        buys = [r for r in sym_rows if r.get("action") == "BUY"]
        executed = [r for r in sym_rows if r.get("executed")]
        realized = [
            (r.get("outcome") or {}).get("realized_pct")
            for r in executed
            if isinstance((r.get("outcome") or {}).get("realized_pct"), (int, float))
        ]
        out.append({
            "symbol": sym,
            "samples": len(sym_rows),
            "hit_rate": hits / len(sym_rows),
            "avg_edge": sum(edges) / len(edges) if edges else 0.0,
            "buys": len(buys),
            "executed": len(executed),
            "avg_realized": (sum(realized) / len(realized)) if realized else None,
            "best": max(realized) if realized else None,
            "worst": min(realized) if realized else None,
            "stop_hits": sum(1 for r in buys if (r.get("outcome") or {}).get("stop_hit")),
            "tp_hits": sum(1 for r in buys if (r.get("outcome") or {}).get("tp_hit")),
        })
    # Sort by total samples descending so tickers the bot touches most come first
    out.sort(key=lambda r: -r["samples"])
    return out
