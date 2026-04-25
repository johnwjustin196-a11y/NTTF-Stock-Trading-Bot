"""Market regime classification.

"Regime" here is a human-legible label for the overall market tone that the bot
uses to bias its risk appetite at a given wake-up moment. Labels we use:

    bullish     - broad uptrend, risk-on, buy dips ok
    bearish     - broad downtrend, risk-off, defer new longs
    volatile    - high VIX / conflicting signals, chop — shrink sizing
    neutral     - nothing decisive in either direction

Inputs:
  1. breadth_signal() — composite of index trends, sector breadth, VIX
  2. A small batch of top financial/world-news headlines (macro tone)
  3. (optional) LLM synthesis over (1) + (2) for a more nuanced label

If no Anthropic key is present, we fall back to a deterministic rule over
breadth + VIX only. The result is cached for cfg.regime.cache_minutes so the
same classification is shared across a cycle.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser

from ..utils.config import load_config
from ..utils.llm_client import chat, llm_available, extract_json_object
from ..utils.logger import get_logger
from .market_breadth import breadth_signal

log = get_logger(__name__)

# simple in-process cache: (label, details, computed_at)
_CACHE: dict[str, Any] = {"at": None, "result": None}
_REGIME_DISK_CACHE = Path("data/regime_cache.json")


# ----------------------------------------------------------------- public API

def classify_market_regime(force: bool = False) -> dict[str, Any]:
    cfg = load_config()
    rcfg = cfg.get("regime", {}) or {}

    if not rcfg.get("enabled", True):
        return _fallback_from_breadth()

    cache_min = int(rcfg.get("cache_minutes", 30) or 0)
    if not force and _CACHE["result"] and _CACHE["at"]:
        age = (datetime.now(timezone.utc) - _CACHE["at"]).total_seconds() / 60.0
        if age < cache_min:
            return _CACHE["result"]

    if not force and _REGIME_DISK_CACHE.exists():
        try:
            _disk = json.loads(_REGIME_DISK_CACHE.read_text(encoding="utf-8"))
            _disk_age = (datetime.now() - datetime.fromisoformat(_disk["at"])).total_seconds() / 60.0
            if _disk_age < cache_min:
                _CACHE["result"] = _disk["result"]
                _CACHE["at"] = datetime.fromisoformat(_disk["at"])
                return _disk["result"]
        except Exception:
            pass

    breadth = breadth_signal()
    headlines = _fetch_macro_headlines(rcfg.get("headlines_max", 12))

    ok, _why = llm_available()
    if ok:
        try:
            result = _llm_regime(breadth, headlines, cfg)
        except Exception as e:
            log.warning(f"LLM regime failed, falling back to rule-based: {e}")
            result = _rule_based(breadth, headlines)
    else:
        result = _rule_based(breadth, headlines)

    _CACHE["at"] = datetime.now(timezone.utc)
    _CACHE["result"] = result
    try:
        _REGIME_DISK_CACHE.write_text(
            json.dumps({"at": _CACHE["at"].isoformat(), "result": _CACHE["result"]}),
            encoding="utf-8"
        )
    except Exception:
        pass
    return result


# ----------------------------------------------------------------- headlines

def _fetch_macro_headlines(limit: int) -> list[str]:
    """Pull broad finance/world headlines from a few free RSS sources."""
    feeds = [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",      # top news
        "https://feeds.reuters.com/reuters/businessNews",              # reuters business
        "https://www.marketwatch.com/rss/topstories",                  # marketwatch
        "https://feeds.reuters.com/reuters/worldNews",                 # reuters world
    ]
    out: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:limit]:
                title = (e.get("title") or "").strip()
                if title and title not in out:
                    out.append(title)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        except Exception as ex:
            log.debug(f"macro RSS fetch {url} failed: {ex}")
    return out[:limit]


# ----------------------------------------------------------------- classifiers

def _rule_based(breadth: dict, headlines: list[str]) -> dict[str, Any]:
    cfg = load_config()
    _rcfg = cfg.get("regime", {}) if isinstance(cfg, dict) else {}
    _vix_thresh = int(_rcfg.get("vix_volatile_threshold", 25))
    score = float(breadth.get("score", 0.0))
    vix = (breadth.get("details") or {}).get("vix")
    vix_val = vix
    vix_high = (vix_val is not None and vix_val >= _vix_thresh)

    # simple lexical skim for risk-off words
    risk_off_hits = sum(
        1 for h in headlines if any(
            k in h.lower() for k in
            ("war", "attack", "sanction", "recession", "inflation", "crash",
             "panic", "selloff", "cpi hot", "fed hike", "downgrade", "default")
        )
    )
    risk_on_hits = sum(
        1 for h in headlines if any(
            k in h.lower() for k in
            ("rally", "rebound", "record high", "surge", "beat estimates",
             "cuts rates", "soft landing")
        )
    )
    news_tilt = (risk_on_hits - risk_off_hits) / max(len(headlines), 1)

    if vix_high and score < 0:
        label = "bearish"
    elif vix_high and abs(score) < 0.25:
        label = "volatile"
    elif score >= 0.25 and news_tilt >= 0:
        label = "bullish"
    elif score <= -0.25 and news_tilt <= 0:
        label = "bearish"
    elif score >= 0.4:
        label = "bullish"
    elif score <= -0.4:
        label = "bearish"
    else:
        label = "neutral"

    reason = (
        f"rule-based: breadth={score:+.2f}, vix={vix}, "
        f"news_tilt={news_tilt:+.2f} "
        f"(+{risk_on_hits}/-{risk_off_hits} over {len(headlines)} headlines)"
    )
    return {
        "label": label,
        "score": score,
        "reason": reason,
        "breadth_score": score,
        "vix": vix,
        "news_sample": headlines[:5],
        "source": "rule_based",
    }


def _llm_regime(breadth: dict, headlines: list[str], cfg: dict) -> dict[str, Any]:
    joined = "\n".join(f"- {h}" for h in headlines) if headlines else "(no headlines available)"
    details = breadth.get("details", {}) or {}
    vix = details.get("vix")
    snippet = (
        f"Breadth composite score (-1..1): {breadth.get('score', 0.0):+.2f}\n"
        f"VIX (last): {vix}\n"
        f"Sector breadth %: {details.get('sector_breadth_pct')}\n"
        f"Index trends: "
        + ", ".join(
            f"{k}={v:+.2f}" for k, v in details.items()
            if k.endswith("_trend") and isinstance(v, (int, float))
        )
    )

    prompt = (
        "You are a macro strategist classifying the overall US equity market "
        "tone for an intraday trading bot. Given the quantitative market "
        "breadth below plus the latest world/financial headlines, return a JSON "
        "object with keys:\n"
        "  label   - one of: bullish | bearish | neutral | volatile\n"
        "  score   - float in [-1,1] (-1 very bearish, +1 very bullish)\n"
        "  reason  - <=35 words explaining the call\n\n"
        f"Market breadth:\n{snippet}\n\n"
        f"Headlines:\n{joined}\n\n"
        "Respond with only JSON."
    )

    # Token budget: reasoning models (DeepSeek R1, QwQ) can burn 1-3k tokens on
    # their <think> block before they emit the actual JSON. 400 is enough for a
    # classic chat model but nowhere near enough for a reasoner. Pull from config
    # so it's tunable per-model, with a generous default.
    mt = int(cfg.get("llm", {}).get("max_tokens_regime", 4000))
    text = chat(prompt=prompt, system=None, max_tokens=mt, temperature=0.2, tag="regime")
    # Use the shared extractor — handles code fences, trailing prose, and
    # reasoning-model chattiness after the JSON block.
    data = extract_json_object(text)

    label = str(data.get("label", "neutral")).lower().strip()
    if label not in ("bullish", "bearish", "neutral", "volatile"):
        label = "neutral"
    score = float(max(-1, min(1, data.get("score", 0))))

    return {
        "label": label,
        "score": score,
        "reason": str(data.get("reason", ""))[:400],
        "breadth_score": breadth.get("score"),
        "vix": vix,
        "news_sample": headlines[:5],
        "source": "llm",
    }


def _fallback_from_breadth() -> dict[str, Any]:
    breadth = breadth_signal()
    score = float(breadth.get("score", 0.0))
    if score >= 0.3:
        label = "bullish"
    elif score <= -0.3:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "label": label,
        "score": score,
        "reason": f"breadth-only ({breadth.get('reason','')[:120]})",
        "breadth_score": score,
        "vix": (breadth.get("details") or {}).get("vix"),
        "news_sample": [],
        "source": "breadth_only",
    }
