"""End-of-day reflection.

At 4:30 PM ET:
  1. Load today's journal entries.
  2. Grade each decision against INTRADAY (5-minute) bars from the decision
     timestamp forward — not the old close-to-close hack. For executed trades,
     we also check whether the stop or take-profit actually triggered during
     the session.
  3. Append those per-decision outcomes to data/outcomes.jsonl so the
     dashboard, rules scorer, and signal-weight tuner can read them later.
  4. Ask the LLM to produce:
        - a dated markdown "lesson" section (what worked / what failed /
          rules for tomorrow), written into data/lessons.md
        - 0-3 STRUCTURED proposed rules (with a condition + action + regime
          tag), recorded in data/rules.json alongside the free-form prose.
  5. Score every rule in data/rules.json against the decisions that actually
     fired it today — updating fire_count / hit_count / avg_edge / last_fired.
     Rules are NEVER auto-removed; the dashboard shows hit rate and fire count
     so the user can retire low-performing rules by hand.

If no LLM is available the prose + rules steps are skipped, but outcomes are
still computed + persisted so the metrics pipeline keeps working.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..broker import Broker
from ..utils.config import load_config
from ..utils.llm_client import chat, llm_available, extract_json_object
from ..utils.logger import get_logger
from ..utils.market_time import today_str
from .journal import load_today_journal
from .outcomes import append_outcomes, grade_journal_entries
from .rules import add_proposed_rules, score_rules_against_outcomes

log = get_logger(__name__)


def run_eod_reflection(broker: Broker | None = None) -> str:
    entries = load_today_journal()
    if not entries:
        log.info("EOD reflection: no journal entries today")
        return ""

    # 1. Intraday grading + persistence ---------------------------------------
    outcomes = grade_journal_entries(entries, date_str=today_str())
    append_outcomes(outcomes)
    log.info(f"EOD reflection: graded {len(outcomes)} decisions with intraday bars")

    # 1a. Write flat decisions_log.jsonl with EOD outcome data merged in
    try:
        _write_flat_decisions_log(entries, today_str(), outcomes=outcomes)
    except Exception as _fdle:
        log.warning(f"[decisions-log] write failed: {_fdle}")

    # 1b. Per-indicator outcome logging (non-critical — wrapped in try/except)
    try:
        from .indicator_tracker import (
            extract_indicator_outcomes, append_indicator_outcomes,
            compute_indicator_stats, save_indicator_stats,
        )
        ind_rows = extract_indicator_outcomes(entries, outcomes)
        append_indicator_outcomes(ind_rows)
        save_indicator_stats(compute_indicator_stats())
        log.info(f"[indicator-tracker] logged {len(ind_rows)} indicator-outcome rows")
    except Exception as _e:
        log.warning(f"[indicator-tracker] failed: {_e}")

    # 2. Rules scoring pass — cheap, always runs even when LLM offline --------
    try:
        updated = score_rules_against_outcomes(outcomes)
        if updated:
            log.info(f"Rules ledger updated: {updated} rule(s) had new fires today")
    except Exception as e:
        log.warning(f"Rules scoring failed: {e}")

    # 3. Prose + structured proposed rules from the LLM ----------------------
    cfg = load_config()
    summary_block = _render_summary(outcomes)
    section = f"## {today_str()}\n\n{summary_block}\n"

    today_regime = _most_common_regime(outcomes)

    # Collect analyst notes; fetch intraday performance for flagged ones
    analyst_notes = _collect_analyst_notes(entries, date_str=today_str())

    # Load queue history — entries that were queued today but never triggered
    queue_history = _load_today_queue_history()

    ok, _why = llm_available()
    if ok:
        _max_attempts = 2
        for _attempt in range(1, _max_attempts + 1):
            try:
                llm_out = _llm_reflect(outcomes, cfg, today_regime=today_regime,
                                       analyst_notes=analyst_notes,
                                       queue_history=queue_history)
                if llm_out.get("markdown"):
                    section = (
                        f"## {today_str()} "
                        f"[regime: {today_regime or 'unknown'}]\n\n"
                        + llm_out["markdown"]
                    )
                proposed = llm_out.get("rules") or []
                if proposed:
                    try:
                        added = add_proposed_rules(proposed, regime=today_regime)
                        log.info(f"Rules ledger: added {added} new proposed rule(s)")
                    except Exception as e:
                        log.warning(f"Failed to persist proposed rules: {e}")
                break  # success — exit retry loop
            except Exception as e:
                if _attempt < _max_attempts:
                    log.warning(
                        f"LLM reflection attempt {_attempt} failed ({e}) — retrying in 30s"
                    )
                    time.sleep(30)
                else:
                    log.warning(
                        f"LLM reflection failed after {_max_attempts} attempts, "
                        f"using summary-only block: {e}"
                    )

    # 4. Append the prose section to lessons.md -----------------------------
    lessons_path = Path(cfg["paths"]["lessons_file"])
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lessons_path, "a", encoding="utf-8") as f:
        if lessons_path.stat().st_size == 0:
            f.write("# Lessons Learned\n\nDated notes the bot writes to itself at end of day.\n\n")
        f.write(section.rstrip() + "\n\n")

    log.info(f"EOD reflection written for {today_str()}")
    # Archive today's live data to permanent master files (365-day retention)
    try:
        from src.dashboard.archiver import archive_live_eod
        _data_dir = Path(cfg["paths"]["data_dir"])
        archive_live_eod(_data_dir)
        log.info("EOD archive complete")
    except Exception as _arc_e:
        log.warning(f"EOD archive failed (non-critical): {_arc_e}")
    return section


# ---------------------------------------------------------- flat decisions log

def _write_flat_decisions_log(
    entries: list[dict],
    date_str: str,
    outcomes: list[dict] | None = None,
) -> None:
    """Write data/decisions_log.jsonl with all technical subscores flattened
    and EOD outcome data merged in.

    Mirrors the backtester's backtest_decisions.jsonl format so the same
    post-hoc analysis tools work on live data.  One row per decision entry
    (skips event rows like flatten/ratchet_stop/profit_locked).

    outcome fields merged per row (keyed by symbol+cycle):
      price_at_decision, price_at_eod, pct_to_eod,
      max_favorable_pct, max_adverse_pct,
      stop_hit, tp_hit, realized_pct,
      stop_verdict, close_verdict, hit, edge
    """
    cfg = load_config()
    log_path = Path(cfg.get("paths", {}).get("data_dir", "data")) / "decisions_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Build outcome lookup: (symbol, cycle) -> outcome sub-dict
    _outcome_lookup: dict[tuple[str, str], dict] = {}
    for o in (outcomes or []):
        sym_o = o.get("symbol") or ""
        cyc_o = o.get("cycle") or ""
        if sym_o and cyc_o:
            _outcome_lookup[(sym_o.upper(), cyc_o)] = o

    rows: list[dict] = []
    for e in entries:
        dec = e.get("decision") or {}
        if not dec or not dec.get("action"):
            continue  # skip event rows (flatten, ratchet_stop, etc.)
        sym = dec.get("symbol") or e.get("symbol")
        if not sym:
            continue

        tech_sig = (dec.get("signals") or {}).get("technicals") or {}
        d = tech_sig.get("details") or {}
        news_sig = (dec.get("signals") or {}).get("news") or {}
        llm_sig = (dec.get("signals") or {}).get("llm") or {}
        brd_sig = (dec.get("signals") or {}).get("breadth") or {}
        exe = e.get("executed") or {}
        regime = dec.get("regime") or {}
        trend = dec.get("trend") or {}

        # Merge EOD outcome data
        oc = _outcome_lookup.get((sym.upper(), e.get("cycle", ""))) or {}
        oc_data = oc.get("outcome") or {}

        rows.append({
            "date": date_str,
            "cycle": e.get("cycle", ""),
            "symbol": sym,
            "action": dec.get("action"),
            "combined": dec.get("combined_score"),
            "tech_score": round(float(tech_sig.get("score", 0)), 4),
            "news_score": round(float(news_sig.get("score", 0)), 4),
            "breadth_score": round(float(brd_sig.get("score", 0)), 4),
            "llm_score": round(float(llm_sig.get("score", 0)), 4),
            # Technical sub-scores
            "rsi": d.get("rsi"),
            "rsi_score": d.get("rsi_score"),
            "macd_hist": d.get("macd_hist"),
            "macd_score": d.get("macd_score"),
            "adx": d.get("adx"),
            "trend_score": d.get("trend_score"),
            "bb_pct_b": d.get("bb_pct_b"),
            "bb_score": d.get("bb_score"),
            "bb_squeeze": d.get("bb_squeeze"),
            "obv_score": d.get("obv_score"),
            "vwap_score": d.get("vwap_score"),
            "vwap_distance_pct": d.get("vwap_distance_pct"),
            "fib_score": d.get("fib_score"),
            "fib_ratio": d.get("fib_nearest_ratio"),
            "fib_proximity_pct": d.get("fib_proximity_pct"),
            "fib_direction": d.get("fib_direction"),
            # Context
            "regime": regime.get("label") if isinstance(regime, dict) else regime,
            "regime_score": regime.get("score") if isinstance(regime, dict) else None,
            "trend": trend.get("label") if isinstance(trend, dict) else trend,
            "breadth_reason": str(brd_sig.get("reason", ""))[:150],
            # LLM
            "llm_action": llm_sig.get("action"),
            "llm_confidence": llm_sig.get("confidence"),
            "llm_reason": str(llm_sig.get("reason", ""))[:250],
            # Quality / deep score
            "quality": (dec.get("quality") or {}).get("label"),
            "deep_score": dec.get("deep_score"),
            "deep_grade": dec.get("deep_grade"),
            "gate_notes": dec.get("gate_notes", ""),
            # Position context
            "had_position": dec.get("had_position", False),
            "fill_price": exe.get("filled_price") if exe else None,
            "qty": exe.get("quantity") if exe else None,
            # EOD outcome — what actually happened after this decision
            "price_at_decision": oc_data.get("price_at_decision"),
            "price_at_eod": oc_data.get("price_at_eod"),
            "pct_to_eod": oc_data.get("pct_to_eod"),
            "max_favorable_pct": oc_data.get("max_favorable_pct"),
            "max_adverse_pct": oc_data.get("max_adverse_pct"),
            "stop_hit": oc_data.get("stop_hit"),
            "tp_hit": oc_data.get("tp_hit"),
            "realized_pct": oc_data.get("realized_pct"),
            "stop_verdict": oc_data.get("stop_verdict"),
            "close_verdict": oc_data.get("close_verdict"),
            "hit": oc_data.get("hit"),
            "edge": oc_data.get("edge"),
        })

    if not rows:
        return
    # Append to today's decisions; start new file each day
    with open(log_path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    log.info(f"[decisions-log] wrote {len(rows)} rows to {log_path}")


# ---------------------------------------------------------- summaries

def _render_summary(outcomes: list[dict]) -> str:
    """Fallback markdown when the LLM isn't available. Works off the new
    outcomes schema (with an 'outcome' sub-dict carrying hit/edge/etc.)."""
    if not outcomes:
        return "_no decisions today_"
    graded = [o for o in outcomes if o.get("outcome")]
    total = len(outcomes)
    hits = sum(1 for o in graded if (o.get("outcome") or {}).get("hit") is True)
    misses = sum(1 for o in graded if (o.get("outcome") or {}).get("hit") is False)
    buys = [o for o in outcomes if o["action"] == "BUY"]
    closes = [o for o in outcomes if o["action"] in ("CLOSE", "SELL")]

    executed = [o for o in outcomes if o.get("executed") and (o.get("outcome") or {}).get("realized_pct") is not None]
    avg_realized = (
        sum((o["outcome"]["realized_pct"] or 0) for o in executed) / len(executed)
        if executed else 0.0
    )

    lines = [
        f"**Decisions:** {total}  |  hits: {hits}  |  misses: {misses}",
        f"**Buys:** {len(buys)}  |  **Closes:** {len(closes)}  |  "
        f"**Executed trades:** {len(executed)} (avg realized: {avg_realized:+.2%})",
        "",
        "### Notable",
    ]
    # Sort by absolute edge (biggest moves we participated in / missed)
    def _abs_edge(o: dict) -> float:
        return abs((o.get("outcome") or {}).get("edge") or 0.0)

    for o in sorted(outcomes, key=_abs_edge, reverse=True)[:6]:
        out = o.get("outcome") or {}
        hit = out.get("hit")
        tag = "[hit]" if hit is True else ("[miss]" if hit is False else "[-]")
        edge = out.get("edge") or 0.0
        lines.append(
            f"- {tag} **{o['symbol']}** {o['action']} @ {o.get('cycle','?')} — "
            f"edge {edge:+.2%} (MFE {out.get('max_favorable_pct',0):+.2%} / "
            f"MAE {out.get('max_adverse_pct',0):+.2%}) — {o['reason'][:120]}"
        )
    return "\n".join(lines)


def _most_common_regime(outcomes: list[dict]) -> str:
    """Pick the regime label that appeared most often in today's decisions.
    Used as the 'regime tag' for any proposed rules written today."""
    counts: dict[str, int] = {}
    for o in outcomes:
        r = o.get("regime")
        if isinstance(r, str) and r:
            counts[r] = counts.get(r, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda x: x[1])[0]


# ---------------------------------------------------------- LLM prompt

def _collect_analyst_notes(entries: list[dict], date_str: str) -> list[dict]:
    """Extract analyst_note entries and fetch intraday performance for flagged ones."""
    notes = []
    for e in entries:
        if e.get("type") != "analyst_note":
            continue
        note = dict(e)
        if e.get("fetch_intraday_performance") and e.get("symbol"):
            try:
                note["performance"] = _fetch_intraday_perf(
                    e["symbol"],
                    date_str,
                    e.get("perf_start_time", "09:35"),
                    e.get("perf_end_time", "16:00"),
                )
            except Exception as ex:
                log.debug(f"[analyst_note] perf fetch {e['symbol']}: {ex}")
        notes.append(note)
    return notes


def _fetch_intraday_perf(symbol: str, date_str: str, start_time: str, end_time: str) -> dict:
    """Fetch intraday 5-min bars for symbol on date_str and compute performance metrics."""
    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")

    tkr = yf.Ticker(symbol)
    df = tkr.history(period="1d", interval="5m")
    if df is None or df.empty:
        return {}

    import pandas as pd
    df.index = pd.to_datetime(df.index)
    intraday = df.between_time(start_time, end_time)
    if intraday.empty:
        return {}

    price_open  = float(intraday.iloc[0]["Open"])
    price_close = float(intraday.iloc[-1]["Close"])
    price_high  = float(intraday["High"].max())
    price_low   = float(intraday["Low"].min())
    high_time   = intraday["High"].idxmax().strftime("%H:%M")
    low_time    = intraday["Low"].idxmin().strftime("%H:%M")

    return {
        "price_at_start":  round(price_open,  2),
        "price_at_close":  round(price_close, 2),
        "session_high":    round(price_high,  2),
        "session_low":     round(price_low,   2),
        "high_time":       high_time,
        "low_time":        low_time,
        "pct_change":      round((price_close - price_open) / price_open * 100, 2),
        "max_drawdown_pct": round((price_low  - price_open) / price_open * 100, 2),
        "max_gain_pct":    round((price_high  - price_open) / price_open * 100, 2),
    }


def _load_today_queue_history() -> list[dict]:
    """Load today's queue_history.jsonl rows (entries that expired without triggering)."""
    cfg = load_config()
    path = Path(cfg.get("paths", {}).get("data_dir", "data")) / "queue_cache" / "queue_history.jsonl"
    if not path.exists():
        return []
    today = today_str()
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("date") == today:
                    rows.append(row)
            except Exception:
                continue
    except Exception:
        pass
    return rows


def _llm_reflect(outcomes: list[dict], cfg: dict, *, today_regime: str,
                 analyst_notes: list[dict] | None = None,
                 queue_history: list[dict] | None = None) -> dict:
    """Ask the LLM for BOTH a prose lessons section AND 0-3 structured rules.

    Structured rules let the rest of the system (scoring, dashboard display,
    regime filtering at advisor-time) treat them as first-class objects
    instead of parsing free-form markdown every time.
    """
    # Shrink each outcome to the minimum the LLM needs, so we can afford more
    # rows in the payload without blowing the token budget.
    compact = []
    for o in outcomes[:120]:
        out = o.get("outcome") or {}
        compact.append({
            "symbol": o["symbol"],
            "cycle": o.get("cycle"),
            "action": o["action"],
            "regime": o.get("regime"),
            "quality": o.get("quality"),
            "trend": o.get("trend"),
            "score": round(float(o.get("combined_score", 0)), 3),
            "reason": (o.get("reason") or "")[:200],
            "executed": o.get("executed"),
            "edge": round(out.get("edge", 0) or 0, 4),
            "mfe": round(out.get("max_favorable_pct", 0) or 0, 4),
            "mae": round(out.get("max_adverse_pct", 0) or 0, 4),
            "realized": out.get("realized_pct"),
            "stop_hit": out.get("stop_hit"),
            "tp_hit": out.get("tp_hit"),
            "hit": out.get("hit"),
        })
    payload = json.dumps(compact, indent=2, default=str)[:90000]
    max_new = int(((cfg.get("learning") or {}).get("rules") or {}).get("max_new_per_day", 3))

    notes_block = ""
    if analyst_notes:
        notes_lines = ["### Analyst notes (human-flagged events)\n"]
        for n in analyst_notes:
            sym = n.get("symbol", "")
            note_text = n.get("note", "")
            perf = n.get("performance")
            line = f"- **{sym}** ({n.get('cycle','?')}): {note_text}"
            if perf:
                line += (
                    f" | Post-signal performance ({n.get('perf_start_time','09:35')}"
                    f"-{n.get('perf_end_time','16:00')}): "
                    f"open={perf['price_at_start']:.2f}, "
                    f"close={perf['price_at_close']:.2f} "
                    f"({perf['pct_change']:+.2f}%), "
                    f"low={perf['session_low']:.2f} at {perf['low_time']} "
                    f"(max drawdown {perf['max_drawdown_pct']:+.2f}%), "
                    f"high={perf['session_high']:.2f} at {perf['high_time']} "
                    f"(max gain {perf['max_gain_pct']:+.2f}%)"
                )
            notes_lines.append(line)
        notes_block = "\n" + "\n".join(notes_lines) + "\n"

    # Queue history block — entries queued today that never triggered
    queue_block = ""
    if queue_history:
        q_lines = ["### Entry queue — queued but never triggered today\n"]
        for q in queue_history:
            close = q.get("close_price")
            pct_t = q.get("pct_trigger_to_close")
            q_lines.append(
                f"- **{q['symbol']}** {q.get('entry_type','?')} | "
                f"queued @ {q.get('price_at_queue', 0):.2f} | "
                f"trigger @ {q.get('trigger_price', 0):.2f} "
                f"(Fib {(q.get('fib_ratio') or 0)*100:.1f}%) | "
                f"close @ {close:.2f if close else 'n/a'} | "
                f"score={q.get('combined_score_at_queue', 0):+.3f} | "
                + (f"trigger {'MISSED by ' + '%+.2f%%' % pct_t if pct_t is not None else 'n/a'}")
            )
        queue_block = "\n" + "\n".join(q_lines) + "\n"

    prompt = (
        "You are reviewing today's intraday trading decisions for a small bot. "
        "Each entry has the action the bot took, the combined score, a short reasoning, "
        f"and an 'outcome' block with edge/MFE/MAE graded from 5-minute bars. "
        f"Today's dominant market regime: **{today_regime or 'unknown'}**.\n\n"
        "Produce a JSON object with exactly two keys:\n"
        "  1. `markdown` — a markdown string containing:\n"
        "       • one-sentence day summary\n"
        "       • 2-4 bullets under '### What worked'\n"
        "       • 2-4 bullets under '### What didn't work'\n"
        "       • 1-3 concrete rules under '### Rules for tomorrow'.\n"
        "     Keep the markdown under 350 words. Reference actual tickers.\n"
        "     If queue entries are present, add a brief '### Queue review' section — "
        "     note any entries where price came close to the trigger but missed, or "
        "     where the trigger level looks miscalibrated given where price closed.\n"
        f"  2. `rules` — an array of 0 to {max_new} STRUCTURED proposed rules, where each rule is:\n"
        "       { \"text\": \"<=120 chars, concrete and checkable\",\n"
        "         \"condition\": \"plain-English trigger — e.g. 'breadth < -0.3 and trend == downtrend'\",\n"
        "         \"action\": \"SKIP_BUY | FORCE_CLOSE | REDUCE_SIZE | SKIP_SELL | PREFER_HOLD\",\n"
        "         \"rationale\": \"<=160 chars\"\n"
        "       }\n"
        "     Only include rules where today's data actually supports them. "
        f"     If today is inconclusive, return `rules: []`. The rules will "
        f"     be scored over time; the dashboard shows hit rate so the user "
        f"     can retire bad ones manually — don't worry about perfection.\n\n"
        "OUTPUT FORMAT — strict:\n"
        "- Respond with ONE valid JSON object. Nothing else.\n"
        "- No markdown code fences, no // comments, no trailing commas.\n"
        "- Start with { and end with }.\n\n"
        f"Today's outcomes:\n{payload}"
        f"{notes_block}"
        f"{queue_block}"
    )

    text = chat(
        prompt=prompt,
        system=None,
        max_tokens=int(cfg["llm"]["max_tokens_reflection"]),
        temperature=0.3,
        tag="reflection",
    )
    data = extract_json_object(text)
    md = data.get("markdown")
    rules_raw = data.get("rules") or []
    # Guard against the LLM returning a single rule object instead of a list
    if isinstance(rules_raw, dict):
        rules_raw = [rules_raw]
    rules: list[dict] = []
    for r in rules_raw[:max_new]:
        if not isinstance(r, dict):
            continue
        txt = str(r.get("text") or "").strip()
        if not txt:
            continue
        rules.append({
            "text": txt[:200],
            "condition": str(r.get("condition") or "")[:300],
            "action": str(r.get("action") or "PREFER_HOLD").upper()[:32],
            "rationale": str(r.get("rationale") or "")[:300],
        })
    return {"markdown": md, "rules": rules}
