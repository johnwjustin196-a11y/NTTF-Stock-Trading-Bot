"""Intra-day live session context.

After each decision cycle completes (and orders are executed), this module
writes a brief session summary to data/today_context.md. The LLM advisor
reads that file at the top of the NEXT cycle so every cycle's decisions are
informed by how the current session is going.

Call order (decision_engine.py enforces this):
  1. Primary decision logic + order execution
  2. Post-mortems for any closes (postmortem.py)
  3. update_session_context()   <-- this file, always last

The advisor reads load_session_context() from llm_advisor.py. If the file
is from a prior day it is treated as stale and ignored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)

_CONTEXT_FILE = "data/today_context.md"


# ------------------------------------------------------------------ public API

def update_session_context(broker, cycle_label: str) -> str:
    """Build and persist today's session summary. Always safe to call.

    Returns the context string written (or "" on failure).
    """
    try:
        return _build_and_write(broker, cycle_label)
    except Exception as e:
        log.debug(f"[session-context] update failed after {cycle_label}: {e}")
        return ""


def load_session_context() -> str:
    """Return today's session context string, or '' if stale/missing."""
    path = Path(_CONTEXT_FILE)
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
        # Stale check: first line must contain today's date
        first_line = content.split("\n", 1)[0]
        if today_str() not in first_line:
            return ""
        return content
    except Exception:
        return ""


# ------------------------------------------------------------------ internals

def _build_and_write(broker, cycle_label: str) -> str:
    from .postmortem import load_today_postmortems

    today = today_str()
    positions = broker.get_positions()
    today_entries: list[dict] = []
    all_open: list[dict] = []

    for p in positions:
        tags = getattr(p, "tags", {}) or {}
        entry_dt_str = tags.get("entry_datetime", "")
        entry_date = entry_dt_str[:10] if entry_dt_str else ""
        entry_price = float(tags.get("entry_price") or getattr(p, "avg_entry", 0) or 0)
        qty = float(getattr(p, "quantity", 1) or 1)
        mv = float(getattr(p, "market_value", 0) or 0)
        current_price = mv / qty if qty else entry_price
        pnl_pct = (current_price - entry_price) / entry_price if entry_price else 0.0
        rec = {
            "symbol": p.symbol,
            "entry_price": entry_price,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "today": entry_date == today,
        }
        all_open.append(rec)
        if entry_date == today:
            today_entries.append(rec)

    postmortems = load_today_postmortems()

    lines: list[str] = [f"[Session as of {cycle_label} - {today}]"]

    if all_open:
        avg_pnl = sum(r["pnl_pct"] for r in all_open) / len(all_open)
        up = sum(1 for r in all_open if r["pnl_pct"] >= 0)
        down = len(all_open) - up
        lines.append(
            f"Open positions: {len(all_open)} | Avg unrealized P&L: {avg_pnl:+.2%} | "
            f"{up} up / {down} down"
        )
        best = max(all_open, key=lambda r: r["pnl_pct"])
        worst = min(all_open, key=lambda r: r["pnl_pct"])
        if len(all_open) > 1:
            lines.append(
                f"Best open: {best['symbol']} {best['pnl_pct']:+.2%} | "
                f"Worst: {worst['symbol']} {worst['pnl_pct']:+.2%}"
            )
    else:
        lines.append("Open positions: 0 (fully in cash)")

    if today_entries:
        avg_today = sum(r["pnl_pct"] for r in today_entries) / len(today_entries)
        lines.append(
            f"Today's new entries: {len(today_entries)} | Avg P&L since entry: {avg_today:+.2%}"
        )
    else:
        lines.append("No new entries made today yet.")

    if postmortems:
        wins = [p for p in postmortems if p["pnl_pct"] >= 0]
        losses = [p for p in postmortems if p["pnl_pct"] < 0]
        avg_close = sum(p["pnl_pct"] for p in postmortems) / len(postmortems)
        lines.append(
            f"Today's closed trades: {len(postmortems)} | "
            f"{len(wins)} wins / {len(losses)} losses | Avg: {avg_close:+.2%}"
        )
        for pm in postmortems[-3:]:
            lines.append(
                f"  - {pm['symbol']}: {pm['pnl_pct']:+.2%} ({pm['close_reason'][:60]})"
            )

    ctx = "\n".join(lines)

    path = Path(_CONTEXT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ctx, encoding="utf-8")
    log.debug(f"[session-context] written after {cycle_label} ({len(all_open)} open, {len(postmortems)} closed today)")
    return ctx
