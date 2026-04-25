"""News + sentiment signal.

Sources, in order of preference:
 1. Alpaca News API (ALPACA_API_KEY — same key used for price data, goes back to 2015)
 2. NewsAPI.org (if NEWSAPI_KEY set in .env)
 3. yfinance .news (free, no key)
 4. Yahoo Finance RSS feed via feedparser (fallback)

Scoring:
 - Default: lightweight lexicon, no network needed once headlines fetched
 - If Anthropic API key is set, the LLM scores the full batch in one call for
   more nuanced results. We cap tokens to keep costs modest.

Returns a dict with score in [-1, 1].
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import feedparser
import requests
import yfinance as yf

from ..utils.config import load_config
from ..utils.llm_client import chat, llm_available, extract_json_object
from ..utils.logger import get_logger

log = get_logger(__name__)

POSITIVE = {
    "beat", "beats", "surge", "soar", "rally", "upgrade", "strong",
    "record", "growth", "profit", "expand", "launch", "breakthrough", "partnership",
    "outperform", "raised", "tops", "wins", "gains", "bullish",
}
NEGATIVE = {
    "miss", "misses", "plunge", "fall", "downgrade", "sell", "weak",
    "loss", "decline", "cut", "lawsuit", "probe", "recall", "warning",
    "underperform", "lowered", "drops", "slumps", "bearish", "fraud",
    # macro/regime-crisis terms absent from original set
    "tariff", "tariffs", "concern", "uncertain", "uncertainty", "headwind",
    "recession", "slowdown", "disappointing", "pressure", "retreat",
    "tumble", "sink", "crater", "rout", "selloff", "sell-off",
    "guidance cut", "below estimates", "lowers outlook",
}


def news_signal(symbol: str, lookback_hours: float | None = None) -> dict[str, Any]:
    cfg = load_config()
    news_cfg = cfg["signals"]["news"]
    if lookback_hours is not None:
        news_cfg = dict(news_cfg)
        news_cfg["lookback_hours"] = float(lookback_hours)
    headlines = _fetch_headlines(symbol, news_cfg)

    _seen_titles = set()
    _deduped = []
    for _h in headlines:
        _key = " ".join(_h.get("title", "").lower().split())[:100]
        if _key and _key not in _seen_titles:
            _seen_titles.add(_key)
            _deduped.append(_h)
    headlines = _deduped

    if not headlines:
        return {"symbol": symbol, "source": "news", "score": 0.0,
                "reason": "no news available - scored neutral", "details": {"headlines": []}}

    # Prefer LLM scoring if the configured provider is available; fall back
    # to the lexicon (no network needed) if not or on failure.
    _min_sig = int(news_cfg.get("min_polarized_signals", 3))
    ok, _why = llm_available()
    if ok:
        try:
            score, summary = _llm_score(symbol, headlines, cfg)
        except Exception as e:
            log.warning(f"LLM news scoring failed, falling back to lexicon: {e}")
            score, summary = _lexicon_score(headlines, min_signals=_min_sig)
    else:
        score, summary = _lexicon_score(headlines, min_signals=_min_sig)

    return {
        "symbol": symbol,
        "source": "news",
        "score": score,
        "reason": summary,
        "details": {"headlines": [h["title"] for h in headlines[:5]]},
    }


# -------------------------------------------------------------------- fetchers

def _fetch_headlines(symbol: str, cfg: dict) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(hours=cfg["lookback_hours"])
    limit = cfg["max_headlines_per_ticker"]
    api_key = load_config()["secrets"]["newsapi_key"]

    # 1. Alpaca News — same credentials as price data, most reliable source
    try:
        result = _from_alpaca_news(symbol, cutoff, limit)
        if result:
            return result
    except Exception as e:
        log.debug(f"Alpaca news failed for {symbol}: {e}")

    # 2. NewsAPI
    if api_key:
        try:
            return _from_newsapi(symbol, api_key, cutoff, limit)
        except Exception as e:
            log.warning(f"NewsAPI fetch failed for {symbol}: {e}")

    try:
        news = yf.Ticker(symbol).news or []
        out = []
        for n in news:
            ts = n.get("providerPublishTime", 0)
            try:
                dt = datetime.utcfromtimestamp(ts)
            except Exception:
                continue
            if dt < cutoff:
                continue
            out.append({"title": n.get("title", ""), "published": dt.isoformat(),
                        "link": n.get("link", "")})
        if out:
            return out[:limit]
    except Exception as e:
        log.debug(f"yfinance news failed for {symbol}: {e}")

    # Last resort: Yahoo RSS
    try:
        feed = feedparser.parse(
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        )
        out = []
        for e in feed.entries[:limit]:
            out.append({"title": e.get("title", ""),
                        "published": e.get("published", ""),
                        "link": e.get("link", "")})
        return out
    except Exception as e:
        log.debug(f"RSS fallback failed for {symbol}: {e}")
        return []


def _from_alpaca_news(symbol: str, cutoff: datetime, limit: int) -> list[dict]:
    import os
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return []
    r = requests.get(
        "https://data.alpaca.markets/v1beta1/news",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        params={
            "symbols": symbol,
            "start": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "sort": "desc",
        },
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("news", [])
    return [
        {"title": a["headline"], "published": a["created_at"], "link": a.get("url", "")}
        for a in items
        if a.get("headline")
    ]


def _from_newsapi(symbol: str, key: str, cutoff: datetime, limit: int) -> list[dict]:
    r = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": symbol,
            "from": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": limit,
            "apiKey": key,
        },
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("articles", [])
    return [{"title": a["title"], "published": a["publishedAt"], "link": a["url"]} for a in items]


# -------------------------------------------------------------------- scoring

def _lexicon_score(headlines: list[dict], min_signals: int = 3) -> tuple[float, str]:
    pos = neg = 0
    for h in headlines:
        text = (h.get("title") or "").lower()
        pos += sum(1 for w in POSITIVE if w in text)
        neg += sum(1 for w in NEGATIVE if w in text)
    total = pos + neg
    if total == 0:
        return 0.0, f"no polarized terms in {len(headlines)} headlines"
    if total < min_signals:
        return 0.0, f"insufficient signal ({total} polarized term(s) < min {min_signals}) — scored neutral"
    score = (pos - neg) / total
    return float(max(-1, min(1, score))), f"lexicon: {pos} positive / {neg} negative hits"


def _llm_score(symbol: str, headlines: list[dict], cfg: dict) -> tuple[float, str]:
    joined = "\n".join(f"- {h['title']}" for h in headlines)
    prompt = (
        f"You are a financial news analyst. Given these recent headlines about {symbol}, "
        f"return a JSON object with keys 'score' (float in [-1,1]; positive = bullish, "
        f"negative = bearish, 0 = neutral/mixed) and 'summary' (<=25 words). "
        f"Consider materiality — vague fluff pieces should be near 0.\n\n"
        f"Headlines:\n{joined}\n\nRespond only in English with only JSON, no other text."
    )
    # Token budget: reasoning models (DeepSeek R1, QwQ) can burn 1-3k tokens on
    # their <think> block before they emit the actual JSON. 300 is enough for
    # a classic chat model but starves a reasoner. Pull from config so it's
    # tunable per-model, with a generous default.
    mt = int(cfg.get("llm", {}).get("max_tokens_news", 3000))
    text = chat(prompt=prompt, system=None, max_tokens=mt, temperature=0.1, tag="news")
    # Shared extractor handles code fences and trailing commentary the model
    # may emit despite being asked to "respond with only JSON".
    data = extract_json_object(text)
    return float(max(-1, min(1, data["score"]))), data.get("summary", "")[:200]
