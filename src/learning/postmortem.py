"""Per-trade post-mortem learning.

Fires immediately after a position closes (signal CLOSE, stop hit, or
take-profit). Asks the local LLM for a targeted lesson about THIS specific
trade. Writes to data/trade_postmortems.jsonl and appends to data/lessons.md.

Design rules:
  - Always fires AFTER primary execution -- never delays a decision or order.
  - Wrapped in try/except so a post-mortem failure never surfaces to the caller.
  - Works without an LLM (skips the LLM call, still persists the raw P&L row).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.llm_client import chat, llm_available, extract_json_object
from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)

_POSTMORTEMS_FILE = "data/trade_postmortems.jsonl"


# ------------------------------------------------------------------ public API

def run_trade_postmortem(
    symbol: str,
    close_reason: str,
    close_price: float,
    position,
    entry_journal_entry: dict | None = None,
    as_of_date=None,
    postmortems_file: str | None = None,
    lessons_file: str | None = None,
    use_llm: bool = True,
) -> str:
    """Fire a post-mortem for a just-closed trade. Always safe to call.

    Returns the lesson text written (or "" if LLM skipped / failed).
    The row is persisted to the postmortems file regardless of LLM status.

    Pass as_of_date when calling from backtest (stamps records with sim date).
    Pass postmortems_file / lessons_file to redirect writes to separate files
    (e.g. data/backtest_postmortems.jsonl and data/backtest_lessons.md) so
    backtest artifacts stay separate from live data until the user reviews them.
    """
    try:
        return _do_postmortem(
            symbol, close_reason, close_price, position, entry_journal_entry,
            as_of_date=as_of_date,
            postmortems_file=postmortems_file,
            lessons_file=lessons_file,
            use_llm=use_llm,
        )
    except Exception as e:
        log.warning(f"[postmortem] {symbol}: {e}")
        return ""


def load_today_postmortems() -> list[dict]:
    """Return today's completed post-mortem rows (used by session_context)."""
    path = Path(_POSTMORTEMS_FILE)
    if not path.exists():
        return []
    today = today_str()
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("date") == today:
                    out.append(row)
            except json.JSONDecodeError:
                continue
    return out


# ------------------------------------------------------------------ internals

def _do_postmortem(
    symbol: str,
    close_reason: str,
    close_price: float,
    position,
    entry_journal_entry: dict | None,
    as_of_date=None,
    postmortems_file: str | None = None,
    lessons_file: str | None = None,
    use_llm: bool = True,
) -> str:
    cfg = load_config()
    tags = getattr(position, "tags", {}) or {}
    entry_price = float(tags.get("entry_price") or getattr(position, "avg_entry", 0) or close_price)
    qty = float(getattr(position, "quantity", 1) or 1)
    pnl_pct = (close_price - entry_price) / entry_price if entry_price else 0.0
    pnl_dollar = (close_price - entry_price) * qty
    date_str = as_of_date.isoformat() if as_of_date is not None else today_str()

    hold_duration = _hold_duration(tags.get("entry_datetime", ""))

    entry_context_parts = [
        f"quality={tags.get('quality', '?')}",
        f"trend={tags.get('trend', '?')}",
        f"regime={tags.get('regime', '?')}",
    ]
    if entry_journal_entry:
        d = entry_journal_entry.get("decision") or {}
        sigs = d.get("signals") or {}
        for k in ("technicals", "news", "breadth", "llm"):
            v = sigs.get(k) or {}
            s = v.get("score") if isinstance(v, dict) else None
            if s is not None:
                entry_context_parts.append(f"{k}={float(s):+.2f}")
        reason_snip = (d.get("reason") or "")[:150]
        if reason_snip:
            entry_context_parts.append(f"entry_reason={reason_snip}")

    stop_loss = getattr(position, "stop_loss", None) or tags.get("stop_loss")
    take_profit = getattr(position, "take_profit", None) or tags.get("take_profit")
    stop_str = f"${float(stop_loss):.2f}" if stop_loss else "none"
    tp_str = f"${float(take_profit):.2f}" if take_profit else "none"

    # LLM lesson generation (optional)
    lesson = ""
    tags_list: list[str] = []
    sentiment = "neutral"
    ok, _why = llm_available()
    if use_llm and ok:
        prompt = (
            f"A trade on {symbol} just closed. Write a specific post-mortem lesson.\n\n"
            f"Trade:\n"
            f"  symbol: {symbol}\n"
            f"  entry: ${entry_price:.2f}\n"
            f"  exit: ${close_price:.2f}\n"
            f"  P&L: {pnl_pct:+.2%} (${pnl_dollar:+.0f})\n"
            f"  hold: {hold_duration or 'unknown'}\n"
            f"  close reason: {close_reason}\n"
            f"  stop was: {stop_str}\n"
            f"  take_profit was: {tp_str}\n"
            f"  entry context: {', '.join(entry_context_parts)}\n\n"
            f"Return JSON with:\n"
            f'  "lesson": 1-3 sentences specific to THIS trade (name the ticker, cite P&L and reason)\n'
            f'  "tags": 1-3 short tag strings like ["stop_placement","momentum","overbought"]\n'
            f'  "sentiment": "positive" | "negative" | "neutral"\n\n'
            f"ONE JSON object only. Start with {{ end with }}."
        )
        try:
            text = chat(prompt=prompt, system=None, max_tokens=300, temperature=0.3, tag="postmortem")
            data = extract_json_object(text)
            lesson = str(data.get("lesson") or "").strip()
            tags_list = [str(t) for t in (data.get("tags") or [])][:5]
            sentiment = str(data.get("sentiment") or "neutral").lower()
        except Exception as e:
            log.debug(f"[postmortem] {symbol}: LLM call failed: {e}")

    # Append lesson to the lessons file
    if lesson:
        lessons_path = Path(lessons_file if lessons_file else cfg["paths"]["lessons_file"])
        lessons_path.parent.mkdir(parents=True, exist_ok=True)
        section = (
            f"\n### [{date_str}] {symbol} post-mortem ({pnl_pct:+.1%})\n"
            f"{lesson}\n"
        )
        with open(lessons_path, "a", encoding="utf-8") as f:
            f.write(section)

    # Persist row to postmortems file
    pm_path = Path(postmortems_file if postmortems_file else _POSTMORTEMS_FILE)
    pm_path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "date": date_str,
        "symbol": symbol,
        "entry": round(entry_price, 4),
        "exit": round(close_price, 4),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_dollar": round(pnl_dollar, 2),
        "hold_duration": hold_duration,
        "close_reason": close_reason[:200],
        "lesson": lesson,
        "tags": tags_list,
        "sentiment": sentiment,
    }
    with open(pm_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")

    log.info(f"[postmortem] {symbol}: {pnl_pct:+.2%} ({close_reason[:60]}) | lesson={'yes' if lesson else 'no-llm'}")
    return lesson


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
