"""9:00am trade plan — LLM synthesis over the pre-scored watchlist.

Ranks all watchlist candidates by a composite of deep_score, technicals, and
news, asks the LLM to confirm the top 20 for today, and writes the narrowed
list back to shortlist.json. Falls back to a pure numeric sort if the LLM
call fails.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..utils.config import load_config
from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)

_PLAN_TOP_N = 20


def build_trade_plan(
    shortlist: list[str],
    premarket_ratings: dict[str, dict],
    regime_label: str = "neutral",
) -> list[str]:
    """Rank watchlist by deep_score + tech + news, confirm with LLM, return top 20.

    Args:
        shortlist:         current watchlist symbols
        premarket_ratings: output of get_premarket_ratings() — per-symbol scores
        regime_label:      current market regime string for the LLM prompt

    Returns:
        Ordered list of up to _PLAN_TOP_N symbols, best conviction first.
        Also writes the narrowed list to data/shortlist.json.
    """
    from ..analysis.deep_scorer import get_score

    if not shortlist:
        log.warning("[trade-plan] empty shortlist — nothing to rank")
        return []

    # Build a scored candidate list for the prompt
    candidates: list[dict] = []
    for sym in shortlist:
        rating = premarket_ratings.get(sym, {})
        score_entry = get_score(sym) or {}
        deep = float(score_entry.get("score", 50))
        tech = float(rating.get("tech_score", 0.0))
        news = float(rating.get("news_score", 0.0))
        grade = score_entry.get("grade", "C")
        composite = deep * 0.40 + (tech * 50 + 50) * 0.35 + (news * 50 + 50) * 0.25
        candidates.append({
            "symbol": sym,
            "deep_score": deep,
            "deep_grade": grade,
            "tech_score": round(tech, 3),
            "news_score": round(news, 3),
            "composite": round(composite, 1),
        })

    # Sort by composite descending for the fallback and for the prompt order
    candidates.sort(key=lambda x: x["composite"], reverse=True)

    top_n = min(_PLAN_TOP_N, len(candidates))
    fallback = [c["symbol"] for c in candidates[:top_n]]

    # Ask the LLM to confirm/reorder
    try:
        ranked = _llm_rank(candidates, regime_label, top_n)
        if not ranked:
            ranked = fallback
    except Exception as e:
        log.warning(f"[trade-plan] LLM ranking failed ({e}) — using numeric fallback")
        ranked = fallback

    _score_map = {c["symbol"]: c["composite"] for c in candidates}
    _top_preview = ", ".join(
        f"{s}={_score_map.get(s, 0):.1f}" for s in ranked[:5]
    )
    log.info(
        f"[trade-plan] {len(shortlist)} -> top {len(ranked)} | {_top_preview}"
        + (f" ... +{len(ranked)-5} more" if len(ranked) > 5 else "")
    )

    # Persist narrowed shortlist (keeps trends from existing file)
    _write_plan_shortlist(ranked)
    return ranked


def _llm_rank(candidates: list[dict], regime: str, top_n: int) -> list[str]:
    """Single LLM call: rank candidates, return top_n symbol list."""
    from ..utils.llm_client import chat

    cfg = load_config()
    max_tokens = int(cfg.get("llm", {}).get("max_tokens_advisor", 1500))

    lines = "\n".join(
        f"  {c['symbol']:6s}  deep={c['deep_score']:.0f}{c['deep_grade']}  "
        f"tech={c['tech_score']:+.2f}  news={c['news_score']:+.2f}"
        for c in candidates
    )

    prompt = (
        f"Market regime today: {regime.upper()}\n\n"
        f"Pre-market stock scores ({len(candidates)} candidates):\n{lines}\n\n"
        f"Task: Select the best {top_n} stocks to actively trade today based on "
        f"technical momentum, news sentiment, and fundamental quality (deep score). "
        f"Consider the regime — in bearish/volatile regimes prefer defensive or "
        f"high-conviction names. In bullish/neutral regimes, favour momentum.\n\n"
        f"Reply with ONLY a comma-separated list of exactly {top_n} ticker symbols "
        f"in order of conviction, highest first. No explanation, no other text.\n"
        f"Example: AAPL, NVDA, MSFT, ..."
    )

    raw = chat(prompt, max_tokens=max_tokens, temperature=0.1, tag="trade_planner")
    return _parse_symbol_list(raw, {c["symbol"] for c in candidates}, top_n)


def _parse_symbol_list(text: str, valid: set[str], top_n: int) -> list[str]:
    """Extract ticker symbols from LLM free-text response."""
    if not valid:
        import logging
        logging.warning("_parse_symbol_list: valid set is empty - returning no symbols")
        return []
    # Grab anything that looks like a ticker: 1-5 uppercase letters, optionally
    # preceded by a comma, space, or newline
    tokens = re.findall(r"\b([A-Z]{1,5})\b", text.upper())
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if tok in valid and tok not in seen:
            seen.add(tok)
            result.append(tok)
        if len(result) >= top_n:
            break
    return result


def _write_plan_shortlist(symbols: list[str]) -> None:
    """Overwrite shortlist.json symbols with the trade plan list, keep trends."""
    cfg = load_config()
    out_path = Path(cfg["paths"]["shortlist_file"])
    existing: dict = {}
    if out_path.exists():
        try:
            with open(out_path) as f:
                existing = json.load(f)
        except Exception:
            pass
    trends = {s: existing.get("trends", {}).get(s, {}) for s in symbols}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "date": today_str(),
            "symbols": symbols,
            "trends": trends,
            "trade_plan": True,
        }, f, indent=2)
