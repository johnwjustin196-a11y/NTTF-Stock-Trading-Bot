"""LLM synthesis signal.

Takes the other signal outputs for a ticker plus the cumulative lessons file
and asks the LLM to output a final directional score with a reasoning string.
This is what lets the bot "learn" day-over-day — yesterday's lessons are
literally in today's system prompt.

Three learning inputs are layered into the prompt:
  1. `lessons.md` — dated markdown sections written by EOD reflection.
     Each section is tagged with the regime it was written in so we can
     prefer regime-matching lessons when the tape matches.
  2. Active structured rules from data/rules.json — the LLM-proposed
     conditions the bot has been tracking. We prefer rules whose
     `regime_when_proposed` matches the current regime.
  3. Per-ticker track record from data/outcomes.jsonl — "how have the last
     N decisions on THIS ticker played out?" so the advisor gets per-symbol
     history instead of only market-wide lessons.

If the configured provider isn't available, returns a neutral score with a note.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.llm_client import chat, llm_available, provider_label, extract_json_object
from ..utils.logger import get_logger

try:
    from ..trading import entry_queue as _entry_queue
except Exception:
    _entry_queue = None  # type: ignore

# Lazy imports from the learning package: the advisor still works if these
# files don't exist yet (first run, or if the learning dir got wiped).
try:
    from ..learning.track_record import ticker_track_record as _track_record
except Exception:  # pragma: no cover
    _track_record = None  # type: ignore
try:
    from ..learning.rules import rules_for_prompt as _rules_for_prompt
except Exception:  # pragma: no cover
    _rules_for_prompt = None  # type: ignore

log = get_logger(__name__)


SYSTEM_PROMPT = """You are a disciplined intraday trading advisor for a small retail trading bot.

You will be given, for a single ticker:
- Technical signal score and reasoning
- News sentiment signal score and reasoning
- Overall market breadth / regime signal
- The bot's own track record on this specific ticker (if enough history)
- Active scored rules from prior reflections (with hit rate + avg edge)
- Recent lessons learned from prior trading days (what worked, what did not)
- The bot's currently held position in this ticker (if any)

Your job is to return a JSON object with:
  "score": float in [-1, 1]   (+1 = strong buy conviction, -1 = strong sell, 0 = neutral)
  "action": "BUY" | "SELL" | "HOLD" | "CLOSE"
  "confidence": float in [0, 1]
  "reason": <=40 words explaining your decision, referencing the signals AND applicable lessons/rules

Hard rules:
- If breadth score is below -0.5, strongly bias toward HOLD or CLOSE — do not buy into a broken tape.
- If the ticker has no position and ALL of technicals/news are neutral (|score|<0.15), action MUST be HOLD.
- Respect the lessons and active rules: if a rule with a good hit rate contradicts the signal setup, weight the rule.
- Per-ticker track record is informative, not dispositive — a ticker with a bad history can still be a good trade today if the signals are strong and the rules don't say otherwise.
- Never recommend position-sizing bigger than the system already allows — you are only giving a directional call.
- Do NOT recommend CLOSE on an open position solely because RSI is overbought. In a bullish regime, RSI stays overbought for extended periods — overbought alone is NOT a sell signal. Only recommend CLOSE if there is a specific negative catalyst: bad news, regime turning bearish, or breadth collapsing.

OUTPUT FORMAT — this is strict:
- Your ENTIRE response must be a single valid JSON object. Nothing else.
- No text before the opening { and no text after the closing }.
- No markdown code fences (no ```json, no ```).
- No // or /* */ comments inside the JSON. Put all commentary in the "reason" field.
- No trailing commas.
- Use double quotes for all keys and string values.

Start your response with { and end it with }."""


def llm_signal(
    symbol: str,
    tech: dict,
    news: dict,
    breadth: dict,
    position_qty: float = 0.0,
    regime: dict | None = None,
    deep_score: dict | None = None,
    decision_snapshot: dict | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    ok, why = llm_available()
    if not ok:
        return {"symbol": symbol, "source": "llm", "score": 0.0, "action": "HOLD",
                "confidence": 0.0, "reason": f"LLM advisor skipped: {why}",
                "details": {}}

    regime_label = None
    if isinstance(regime, dict):
        regime_label = regime.get("label")

    lessons = _recent_lessons(cfg["llm"]["lessons_window_days"], regime=regime_label)
    rules_block = _active_rules_block(regime=regime_label)
    track_block = _ticker_track_block(symbol)
    session_ctx = _session_context_block()
    cycle_rates_block = _cycle_win_rates_block(regime=regime_label)
    hold_cf_block = _hold_counterfactual_block()
    regime_rates_block = _regime_win_rates_block(regime=regime_label)

    # Auto-load the weekly deep score from disk when not supplied by caller
    if deep_score is None:
        try:
            from .deep_scorer import get_score
            deep_score = get_score(symbol)
        except Exception:
            deep_score = None

    ds_block = _format_deep_score_block(deep_score) if deep_score else ""
    similarity_line = _similar_setups_line(decision_snapshot, tech)

    # Inject queued-entry context so the LLM can affirm or cancel
    queued_entry = None
    if _entry_queue is not None and not position_qty:
        try:
            queued_entry = _entry_queue.get_entry(symbol)
        except Exception:
            pass

    prompt = _build_user_prompt(
        symbol, tech, news, breadth, position_qty,
        regime_label=regime_label or "",
        track_record=track_block,
        deep_score_block=ds_block,
        similarity_line=similarity_line,
        queued_entry=queued_entry,
    )
    system_parts = [SYSTEM_PROMPT]
    if rules_block:
        system_parts.append("\n\nActive scored rules (hit rate = times correct / times fired):\n" + rules_block)
    if lessons:
        system_parts.append("\n\nRecent lessons (regime-preferred first):\n" + lessons)
    if regime_rates_block:
        system_parts.append("\n\nHistorical win rates by market regime (60d):\n" + regime_rates_block)
    if cycle_rates_block:
        system_parts.append("\n\nHistorical win rates by time-of-day (30d):\n" + cycle_rates_block)
    if hold_cf_block:
        system_parts.append("\n\nRecent HOLD counterfactuals (missed moves):\n" + hold_cf_block)
    if session_ctx:
        system_parts.append("\n\nCurrent session status (from last completed cycle):\n" + session_ctx)
    system = "".join(system_parts)

    try:
        text = chat(
            prompt=prompt,
            system=system,
            max_tokens=int(cfg["llm"]["max_tokens_advisor"]),
            temperature=0.2,
            tag="advisor",
        )
        data = _extract_json(text)
        score = float(max(-1, min(1, data.get("score", 0))))
        return {
            "symbol": symbol,
            "source": "llm",
            "score": score,
            "action": data.get("action", "HOLD"),
            "confidence": float(data.get("confidence", 0.0)),
            "reason": data.get("reason", "")[:500],
            "details": {"raw": text[:1000], "provider": provider_label()},
        }
    except Exception as e:
        log.warning(f"LLM advisor failed for {symbol}: {e}")
        return {"symbol": symbol, "source": "llm", "score": 0.0, "action": "HOLD",
                "confidence": 0.0, "reason": f"LLM error: {e}", "details": {}}


def _build_user_prompt(
    symbol, tech, news, breadth, qty, *,
    regime_label: str = "",
    track_record: str,
    deep_score_block: str = "",
    similarity_line: str = "",
    queued_entry: dict | None = None,
) -> str:
    pos_line = f"Current position: {qty} shares" if qty else "No current position"
    track_line = f"Track record on {symbol}: {track_record}\n" if track_record else ""
    ds_section = f"\n{deep_score_block}\n" if deep_score_block else ""
    sim_line = f"Pattern memory: {similarity_line}\n" if similarity_line else ""
    regime_line = f"Market regime: {regime_label.upper()}\n" if regime_label else ""
    queue_line = ""
    if queued_entry:
        queue_line = (
            f"QUEUED ENTRY: This stock is waiting for a "
            f"{queued_entry.get('entry_type','').replace('_',' ')} "
            f"at ${queued_entry.get('trigger_price', 0):.2f} "
            f"(Fib {queued_entry.get('fib_ratio', 0)*100:.1f}%, "
            f"queued at {queued_entry.get('queued_cycle','?')}, "
            f"combined score was {queued_entry.get('combined_score_at_queue', 0):+.3f}). "
            f"Vote CLOSE to cancel this queued entry if conditions have deteriorated.\n"
        )
    return (
        f"Ticker: {symbol}\n"
        f"{pos_line}\n"
        f"{regime_line}"
        f"{track_line}"
        f"{sim_line}"
        f"{queue_line}"
        f"\n"
        f"Technicals: score={tech.get('score', 0):+.2f} — {tech.get('reason', '')}\n"
        f"News/sentiment: score={news.get('score', 0):+.2f} — {news.get('reason', '')}\n"
        f"Market breadth: score={breadth.get('score', 0):+.2f} — {breadth.get('reason', '')}\n"
        f"{ds_section}"
        "\nRespond only in English with JSON only."
    )


def _format_deep_score_block(entry: dict) -> str:
    """Format the weekly deep research score as a concise prompt block.

    Gives the intraday advisor the full picture the deep scorer built:
    composite grade, per-dimension scores with rationale, and key fundamental
    stats so it can weigh them against the intraday technical/news signals.
    """
    if not entry or entry.get("error"):
        return ""

    score = entry.get("score", 0)
    grade = entry.get("grade", "?")
    signal = entry.get("signal", "?")
    updated = (entry.get("updated") or "")[:10]
    bd = entry.get("breakdown") or {}
    ks = entry.get("key_stats") or {}

    def _dim(key: str, label: str, pct: str) -> str:
        d = bd.get(key) or {}
        sc = d.get("score", "n/a")
        rat = (d.get("rationale") or "")[:80]
        bull = (d.get("bull") or "")[:60]
        bear = (d.get("bear") or "")[:60]
        return f"  {label} ({pct}): {sc}/100 -- {rat}\n    Bull: {bull}\n    Bear: {bear}"

    def _ks(key: str, label: str, fmt: str = "") -> str:
        v = ks.get(key)
        if v is None:
            return ""
        if fmt == "pct":
            return f"{label}: {v*100:.1f}%"
        if fmt == "usd_m":
            return f"{label}: ${v/1e6:.0f}M" if abs(v) < 1e9 else f"{label}: ${v/1e9:.1f}B"
        return f"{label}: {v}"

    stats_parts = [
        _ks("analyst_target", "Analyst PT", ""),
        _ks("recommendation", "Consensus"),
        _ks("pe_forward", "Fwd P/E"),
        _ks("ps_ratio", "P/S"),
        _ks("short_float", "Short float", "pct"),
        _ks("beta", "Beta"),
        _ks("revenue_growth", "Rev growth", "pct"),
        _ks("gross_margin", "Gross margin", "pct"),
        _ks("free_cash_flow", "FCF", "usd_m"),
    ]
    stats_line = " | ".join(s for s in stats_parts if s)

    lines = [
        f"Weekly Deep Research (as of {updated}): {score:.1f}/100 | Grade: {grade} | Signal: {signal}",
        _dim("technical", "Technical  ", "25%"),
        _dim("fundamental", "Fundamental", "25%"),
        _dim("sentiment", "Sentiment  ", "20%"),
        _dim("risk", "Risk       ", "15%"),
        _dim("thesis", "Thesis     ", "15%"),
    ]
    if stats_line:
        lines.append(f"  Key stats: {stats_line}")

    return "\n".join(lines)


def _recent_lessons(days: int, *, regime: str | None = None) -> str:
    """Return lessons from the last `days` sections of lessons.md. If a regime
    is given and the lessons sections are regime-tagged (new format includes
    "[regime: <label>]" in the heading), preferentially return matching-regime
    sections first, filling with the remainder up to the window.
    """
    cfg = load_config()
    path = Path(cfg["paths"]["lessons_file"])
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", text, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]
    sections = sections[-max(days * 2, days):]  # take a wider pool, then filter

    if regime:
        tag = f"[regime: {regime}"
        matching = [s for s in sections if tag in s.split("\n", 1)[0]]
        others = [s for s in sections if s not in matching]
        ordered = matching + others
        return "\n\n".join(ordered[-days:]).strip()

    return "\n\n".join(sections[-days:]).strip()


def _active_rules_block(*, regime: str | None) -> str:
    """Render up to ~10 active rules as a compact list with their scoring
    so the advisor can weigh well-performing rules more heavily than fresh ones.
    """
    if _rules_for_prompt is None:
        return ""
    try:
        rules = _rules_for_prompt(regime=regime, limit=10)
    except Exception:
        return ""
    if not rules:
        return ""
    lines = []
    for r in rules:
        stats = r.get("stats") or {}
        fires = int(stats.get("fire_count", 0))
        hits = int(stats.get("hit_count", 0))
        hit_rate = (hits / fires) if fires else 0.0
        edge = float(stats.get("avg_edge", 0.0) or 0.0)
        age_tag = f" (regime={r.get('regime_when_proposed')})" if r.get("regime_when_proposed") else ""
        if fires == 0:
            score_tag = "(unfired)"
        else:
            score_tag = f"({fires} fires, {hit_rate:.0%} hit, avg edge {edge:+.2%})"
        lines.append(f"- [{r.get('action','?')}] {r.get('text','')} {score_tag}{age_tag}")
    return "\n".join(lines)


def _ticker_track_block(symbol: str) -> str:
    """One-liner summary of how the bot has done on this ticker recently.
    Returns an empty string when there's not enough history — the advisor
    then falls back to the signals alone.
    """
    if _track_record is None:
        return ""
    try:
        tr = _track_record(symbol)
    except Exception:
        return ""
    if not tr.get("has_history"):
        return ""
    return tr.get("summary_line", "")


def _regime_win_rates_block(*, regime: str | None) -> str:
    """Return a one-liner per regime showing historical win rates (60d)."""
    try:
        from ..learning.outcomes import regime_win_rates
        data = regime_win_rates(window_days=60)
        if not data:
            return ""
        lines: list[str] = []
        for r, stats in sorted(data.items()):
            total = stats.get("total", 0)
            if total < 5:
                continue
            hr = stats.get("hit_rate")
            ae = stats.get("avg_edge")
            marker = " <-- current" if r == (regime or "").lower() else ""
            lines.append(
                f"  {r}: {total} trades | hit rate {hr:.0%} | avg edge {ae:+.2%}{marker}"
                if (hr is not None and ae is not None) else f"  {r}: {total} trades"
            )
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def _hold_counterfactual_block() -> str:
    """Return a compact summary of recent HOLD decisions where price moved big.

    Shows the bot where it was too conservative (missed gains) or rightly
    cautious (HOLDed something that fell). Injected into system prompt once
    per session — small enough to not bloat the context.
    """
    try:
        from ..learning.outcomes import hold_counterfactuals
        rows = hold_counterfactuals(window_days=10, min_missed_edge=0.015)
        if not rows:
            return ""
        missed = [r for r in rows if r["missed_gain"]][:4]
        correct = [r for r in rows if not r["missed_gain"]][:2]
        lines: list[str] = []
        if missed:
            lines.append("Recent HOLDs that moved up significantly (possible over-conservatism):")
            for r in missed:
                lines.append(
                    f"  {r['date']} {r['symbol']} ({r['cycle']}): "
                    f"held, then moved {r['pct_to_eod']:+.1%} — score was {r['combined_score']:+.2f}"
                )
        if correct:
            lines.append("Recent HOLDs that fell (correct passes):")
            for r in correct:
                lines.append(
                    f"  {r['date']} {r['symbol']} ({r['cycle']}): "
                    f"held, then moved {r['pct_to_eod']:+.1%}"
                )
        return "\n".join(lines)
    except Exception:
        return ""


def _similar_setups_line(decision_snapshot: dict | None, tech: dict) -> str:
    """Query the setup fingerprint DB for similar past setups and format as one line."""
    if not decision_snapshot:
        return ""
    try:
        from ..learning.setup_memory import find_similar_setups, format_similarity_block
        current_price = float((tech.get("details") or {}).get("current_price") or 0)
        match = find_similar_setups(decision_snapshot, current_price, window_days=60, min_matches=3)
        return format_similarity_block(match)
    except Exception:
        return ""


def _cycle_win_rates_block(*, regime: str | None) -> str:
    """Return a compact text block showing BUY win rates by cycle, regime-highlighted."""
    try:
        from ..learning.outcomes import cycle_win_rates
        data = cycle_win_rates(window_days=30)
        by_cycle = data.get("by_cycle") or {}
        by_cr = data.get("by_cycle_regime") or {}
        if not by_cycle:
            return ""
        lines: list[str] = []
        for cycle in sorted(by_cycle.keys()):
            row = by_cycle[cycle]
            total = row.get("total", 0)
            if total < 5:
                continue
            hr = row.get("hit_rate")
            ae = row.get("avg_edge")
            base = (
                f"{cycle}: {total} trades | "
                f"hit rate {hr:.0%} | avg edge {ae:+.2%}"
                if (hr is not None and ae is not None) else f"{cycle}: {total} trades"
            )
            # Append regime-specific overlay if available
            if regime:
                cr_key = f"{cycle}|{regime}"
                cr = by_cr.get(cr_key)
                if cr and (cr.get("total") or 0) >= 3:
                    cr_hr = cr.get("hit_rate")
                    cr_ae = cr.get("avg_edge")
                    if cr_hr is not None:
                        base += f" | in {regime}: {cr_hr:.0%} ({cr['total']} trades)"
            lines.append(base)
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def _session_context_block() -> str:
    """Return today's session context written after the previous cycle, or ''."""
    try:
        from ..learning.session_context import load_session_context
        return load_session_context()
    except Exception:
        return ""


def _extract_json(text: str) -> dict:
    # Delegated to the shared helper in llm_client which handles code fences,
    # leading prose, and trailing commentary that reasoning models love to add.
    return extract_json_object(text)
