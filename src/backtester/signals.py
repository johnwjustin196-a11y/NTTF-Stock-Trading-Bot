"""Backtest-aware signal wrappers.

These replace the live versions used in `decision_engine.py` during a backtest:
  - `backtest_breadth(cache, date)` — SPY/QQQ/IWM momentum from cached bars
  - `backtest_regime(cache, date)` — rule-based regime (no LLM call)
  - `backtest_trend(cache, symbol, date)` — trend from cached daily bars
  - `backtest_news_signal(symbol, date)` — Alpaca News → Finnhub → NewsAPI
  - `backtest_llm_signal(...)` — wraps llm_advisor with temporal system prompt

All functions accept an `as_of_date` argument and must not use any data from
after that date.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .data_cache import DataCache
from ..utils.logger import get_logger

log = get_logger(__name__)


# ------------------------------------------------------------------ breadth

def backtest_breadth(cache: DataCache, as_of_date) -> dict[str, Any]:
    """Simple breadth score from SPY/QQQ/IWM price vs 20-day MA."""
    score = 0.0
    reasons: list[str] = []

    for sym, weight in [("SPY", 0.4), ("QQQ", 0.35), ("IWM", 0.25)]:
        bars = cache.daily_bars(sym, as_of_date)
        if bars.empty or len(bars) < 21:
            reasons.append(f"{sym}:n/a")
            continue
        close = bars["Close"].astype(float)
        price = float(close.iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])

        trend_contrib = weight * (0.5 if price > sma20 else -0.5)
        score += trend_contrib
        reasons.append(f"{sym}{'+' if price > sma20 else '-'}")

        # Short-term momentum amplifier (5d)
        if len(close) >= 6:
            p5 = float(close.iloc[-6])
            if p5 > 0:
                mom = (price - p5) / p5
                score += weight * mom * 1.5  # amplified but capped below

    score = max(-1.0, min(1.0, score))
    return {
        "score": round(float(score), 3),
        "source": "backtest_breadth",
        "reason": f"index trend: {', '.join(reasons)}",
        "details": {"as_of": str(as_of_date)},
    }


# ------------------------------------------------------------------ regime

def backtest_regime(cache: DataCache, as_of_date) -> dict[str, Any]:
    """Rule-based market regime from SPY momentum, MAs, and realised vol."""
    bars = cache.daily_bars("SPY", as_of_date)
    if bars.empty or len(bars) < 50:
        return {"label": "neutral", "score": 0.0,
                "reason": "insufficient SPY history", "source": "backtest_regime"}

    close = bars["Close"].astype(float)
    price = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else price
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else price

    rets = close.pct_change().dropna()
    vol_20d = float(rets.tail(20).std() * (252 ** 0.5)) if len(rets) >= 20 else 0.20

    perf_20d = (price - float(close.iloc[-21])) / float(close.iloc[-21]) if len(close) >= 21 else 0.0

    if price > sma20 > sma50 and perf_20d > 0.01 and vol_20d < 0.30:
        label, score = "bullish", 0.6
    elif price < sma20 < sma50 and perf_20d < -0.01:
        label, score = "bearish", -0.6
    elif vol_20d > 0.35:
        label, score = "volatile", -0.3
    elif price > sma20 or perf_20d > 0:
        label, score = "neutral", 0.1
    else:
        label, score = "neutral", -0.1

    return {
        "label": label,
        "score": round(score, 2),
        "reason": (
            f"SPY ${price:.0f} vs SMA20=${sma20:.0f}/SMA50={sma50:.0f} | "
            f"20d_ret={perf_20d:+.1%} vol={vol_20d:.1%}"
        ),
        "source": "backtest_regime",
    }


# ------------------------------------------------------------------ trend

def backtest_trend(cache: DataCache, symbol: str, as_of_date) -> dict[str, Any]:
    """Classify trend from cached daily bars (no live yfinance call)."""
    bars = cache.daily_bars(symbol, as_of_date)
    if bars.empty or len(bars) < 30:
        return {"label": "sideways",
                "short": {"label": "sideways"}, "long": {"label": "sideways"}}

    close = bars["Close"].astype(float)
    price = float(close.iloc[-1])

    def _label(vs_ma: float, perf: float) -> str:
        if vs_ma > 0.02 and perf > 0.03:
            return "uptrend"
        if vs_ma < -0.02 and perf < -0.03:
            return "downtrend"
        return "sideways"

    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else price
    perf_20d = (price - float(close.iloc[-21])) / float(close.iloc[-21]) if len(close) >= 21 else 0.0
    short = _label((price / sma20 - 1) if sma20 > 0 else 0, perf_20d)

    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else price
    perf_90d = (price - float(close.iloc[-91])) / float(close.iloc[-91]) if len(close) >= 91 else 0.0
    long_ = _label((price / sma50 - 1) if sma50 > 0 else 0, perf_90d)

    overall = long_ if long_ != "sideways" else short
    return {
        "label": overall,
        "short": {"label": short},
        "long": {"label": long_},
    }


# ------------------------------------------------------------------ news

# Per-run cache: (symbol_upper, date_str) -> list of normalized article dicts.
# Populated once per ticker per simulation day; all six intraday cycles reuse it.
# Each dict has 'headline' (str) and 'datetime' (Unix int timestamp).
_alpaca_news_day_cache: dict[tuple[str, str], list[dict]] = {}
_finnhub_day_cache: dict[tuple[str, str], list[dict]] = {}

# Bulk pre-fetch cache: symbol_upper -> list of ALL articles for the full backtest window.
# Populated once at startup by prefetch_alpaca_news_bulk(). When a symbol is present
# here (even with []), _alpaca_news_fetch_day() filters in-memory and skips the API call.
# Symbols absent from this dict were not covered by the bulk fetch and fall back to
# the original per-day API call path.
_alpaca_news_bulk_cache: dict[str, list[dict]] = {}


import json as _json
from pathlib import Path as _Path

_NEWS_CACHE_DEFAULT = "data/news_bulk_cache.json"


def _load_news_disk_cache(cache_path: str, cull_days: int) -> tuple[dict, dict]:
    """Load the persistent news cache from disk. Returns (meta, articles_by_symbol).
    Culls articles older than cull_days. Returns empty structures on any read error.
    """
    path = _Path(cache_path)
    empty_meta: dict = {"cached_start": None, "cached_end": None}
    if not path.exists():
        return empty_meta, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
        meta: dict = raw.get("meta", empty_meta)
        articles: dict[str, list[dict]] = raw.get("data", {})
    except Exception as e:
        log.warning(f"[news_cache] load failed ({e}) — starting fresh")
        return empty_meta, {}

    cutoff_ts = (datetime.utcnow() - timedelta(days=cull_days)).timestamp()
    culled = 0
    for sym in list(articles.keys()):
        before = len(articles[sym])
        articles[sym] = [a for a in articles[sym] if a.get("datetime", 0) >= cutoff_ts]
        culled += before - len(articles[sym])
    if culled:
        log.info(f"[news_cache] culled {culled} articles older than {cull_days} days")
    return meta, articles


def _save_news_disk_cache(cache_path: str, meta: dict, articles: dict) -> None:
    """Write the news cache to disk. Logs a warning on failure (never raises)."""
    path = _Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"meta": meta, "data": articles}, f, separators=(",", ":"))
        total = sum(len(v) for v in articles.values())
        log.info(f"[news_cache] saved — {total} articles / {len(articles)} symbols → {cache_path}")
    except Exception as e:
        log.warning(f"[news_cache] save failed: {e}")


def _fetch_alpaca_news_range(
    symbols: list[str], fetch_start: str, fetch_end: str
) -> dict[str, list[dict]]:
    """Paginated Alpaca news fetch for symbols over [fetch_start, fetch_end].
    Returns dict[symbol_upper -> article list]. Empty dict if no credentials.
    """
    import os
    import requests as _req

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return {}

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    result: dict[str, list[dict]] = {s: [] for s in symbols}
    chunk_size = 50

    for ci in range(0, len(symbols), chunk_size):
        chunk = symbols[ci: ci + chunk_size]
        page_token = None
        pages = 0
        while True:
            params: dict = {
                "symbols": ",".join(chunk),
                "start": fetch_start,
                "end": fetch_end,
                "limit": 50,
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token
            try:
                r = _req.get(
                    "https://data.alpaca.markets/v1beta1/news",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                if not r.ok:
                    log.warning(f"[news_bulk] HTTP {r.status_code} — stopping chunk {ci // chunk_size + 1}")
                    break
                data = r.json()
                for a in data.get("news", []):
                    if not a.get("headline"):
                        continue
                    try:
                        ts = int(datetime.fromisoformat(
                            a["created_at"].replace("Z", "+00:00")
                        ).timestamp())
                    except Exception:
                        continue
                    art = {"headline": a["headline"], "datetime": ts}
                    for s in (a.get("symbols") or []):
                        s_up = s.upper()
                        if s_up in result:
                            result[s_up].append(art)
                pages += 1
                page_token = data.get("next_page_token")
                if not page_token or pages >= 200:
                    break
            except Exception as e:
                log.warning(f"[news_bulk] fetch error chunk {ci // chunk_size + 1}: {e}")
                break
        log.info(
            f"[news_bulk] chunk {ci // chunk_size + 1}/{-(-len(symbols) // chunk_size)} "
            f"({fetch_start} → {fetch_end}, {pages} pages)"
        )
    return result


def prefetch_alpaca_news_bulk(
    symbols: list[str],
    start_date,
    end_date,
    lookback_extra_days: int = 3,
    cache_path: str = _NEWS_CACHE_DEFAULT,
    cull_days: int = 250,
) -> bool:
    """Bulk-fetch Alpaca news with persistent disk cache.

    On each call:
      1. Loads disk cache, culls articles older than cull_days.
      2. Computes which date ranges / symbols are missing from the cache.
      3. Fetches only the gaps from Alpaca (skips entirely when fully covered).
      4. Deduplicates, saves back to disk, populates _alpaca_news_bulk_cache.

    Returns True when the in-memory cache is usable. Any symbol absent from
    _alpaca_news_bulk_cache falls back to per-day API calls automatically.
    """
    from datetime import date as _date

    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    needed_start = start_date - timedelta(days=lookback_extra_days)
    needed_end = end_date
    syms_upper = [s.upper() for s in symbols]

    # ── 1. Load disk cache into memory ───────────────────────────────────
    meta, disk_articles = _load_news_disk_cache(cache_path, cull_days)
    for sym, arts in disk_articles.items():
        _alpaca_news_bulk_cache[sym] = list(arts)

    cached_start: "_date | None" = None
    cached_end: "_date | None" = None
    try:
        if meta.get("cached_start"):
            cached_start = _date.fromisoformat(meta["cached_start"])
        if meta.get("cached_end"):
            cached_end = _date.fromisoformat(meta["cached_end"])
    except Exception:
        pass

    # ── 2. Determine what needs fetching ─────────────────────────────────
    new_syms = [s for s in syms_upper if s not in _alpaca_news_bulk_cache]
    first_run = cached_start is None
    need_before = not first_run and needed_start < cached_start
    need_after = first_run or needed_end > cached_end

    if not new_syms and not need_before and not need_after:
        for s in syms_upper:
            _alpaca_news_bulk_cache.setdefault(s, [])
        log.info(
            f"[news_cache] fully covered by disk cache "
            f"({cached_start} → {cached_end}) — no fetch needed"
        )
        return True

    # ── 3. Fetch only the missing pieces ─────────────────────────────────
    fetched_any = False

    if new_syms:
        log.info(f"[news_bulk] {len(new_syms)} new symbols — fetching {needed_start} → {needed_end}")
        for sym, arts in _fetch_alpaca_news_range(
            new_syms, needed_start.isoformat(), needed_end.isoformat()
        ).items():
            _alpaca_news_bulk_cache[sym] = arts
        fetched_any = True

    if need_before:
        gap_end = (cached_start - timedelta(days=1)).isoformat()
        log.info(f"[news_bulk] pre-cache gap — fetching {needed_start} → {gap_end}")
        for sym, arts in _fetch_alpaca_news_range(
            syms_upper, needed_start.isoformat(), gap_end
        ).items():
            _alpaca_news_bulk_cache.setdefault(sym, [])
            _alpaca_news_bulk_cache[sym] = arts + _alpaca_news_bulk_cache[sym]
        fetched_any = True

    if need_after:
        gap_start = needed_start if first_run else cached_end + timedelta(days=1)
        log.info(f"[news_bulk] post-cache gap — fetching {gap_start} → {needed_end}")
        for sym, arts in _fetch_alpaca_news_range(
            syms_upper, gap_start.isoformat(), needed_end.isoformat()
        ).items():
            existing = _alpaca_news_bulk_cache.get(sym, [])
            _alpaca_news_bulk_cache[sym] = existing + arts
        fetched_any = True

    # ── 4. Deduplicate, save, mark all requested symbols as covered ───────
    if fetched_any:
        for sym in list(_alpaca_news_bulk_cache.keys()):
            seen: set = set()
            deduped: list[dict] = []
            for a in _alpaca_news_bulk_cache[sym]:
                k = (a.get("headline", "")[:100], a.get("datetime", 0))
                if k not in seen:
                    seen.add(k)
                    deduped.append(a)
            _alpaca_news_bulk_cache[sym] = deduped

        new_start = needed_start if cached_start is None else min(cached_start, needed_start)
        new_end = needed_end if cached_end is None else max(cached_end, needed_end)
        _save_news_disk_cache(
            cache_path,
            {"cached_start": new_start.isoformat(), "cached_end": new_end.isoformat(),
             "cull_days": cull_days},
            _alpaca_news_bulk_cache,
        )

    for s in syms_upper:
        _alpaca_news_bulk_cache.setdefault(s, [])

    total = sum(len(v) for v in _alpaca_news_bulk_cache.values())
    log.info(
        f"[news_bulk] ready — {total} articles / "
        f"{sum(1 for v in _alpaca_news_bulk_cache.values() if v)} symbols with news"
    )
    return total > 0


def _alpaca_news_fetch_day(symbol: str, sim_date, lookback_days: int = 3) -> list[dict]:
    """Fetch (and cache) Alpaca news for the lookback window ending on sim_date.

    Checks the bulk pre-fetch cache first (populated by prefetch_alpaca_news_bulk at
    backtest startup). If the symbol is present there, filters in-memory with no API
    call. Falls back to a live Alpaca request only when the bulk cache was not used.
    """
    import os
    import requests

    date_str = str(sim_date)[:10]
    cache_key = (symbol.upper(), date_str)
    if cache_key in _alpaca_news_day_cache:
        return _alpaca_news_day_cache[cache_key]

    # Fast path: bulk cache was pre-fetched at startup — filter in memory.
    sym_upper = symbol.upper()
    if sym_upper in _alpaca_news_bulk_cache:
        if isinstance(sim_date, datetime):
            end_d = sim_date.date()
        else:
            end_d = sim_date
        start_d = end_d - timedelta(days=lookback_days)
        start_ts = datetime.combine(start_d, datetime.min.time()).timestamp()
        end_ts = datetime.combine(end_d, datetime.max.time()).timestamp()
        filtered = [
            a for a in _alpaca_news_bulk_cache[sym_upper]
            if start_ts <= a.get("datetime", 0) <= end_ts
        ]
        _alpaca_news_day_cache[cache_key] = filtered
        return filtered

    # Slow path: no bulk cache for this symbol — call the API per day.
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        _alpaca_news_day_cache[cache_key] = []
        return []

    if isinstance(sim_date, datetime):
        end_date = sim_date.date()
    else:
        end_date = sim_date
    start_date = end_date - timedelta(days=lookback_days)

    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={
                "symbols": symbol,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "limit": 50,
                "sort": "desc",
            },
            timeout=10,
        )
        raw = r.json().get("news", []) if r.ok else []
        articles = []
        for a in raw:
            if not a.get("headline"):
                continue
            try:
                ts = int(datetime.fromisoformat(
                    a["created_at"].replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                continue
            articles.append({"headline": a["headline"], "datetime": ts})
    except Exception as e:
        log.debug(f"[backtest_news] Alpaca news fetch failed for {symbol} on {date_str}: {e}")
        articles = []

    _alpaca_news_day_cache[cache_key] = articles
    return articles


def _finnhub_fetch_day(symbol: str, sim_date, lookback_days: int = 3) -> list[dict]:
    """Fetch (and cache) all Finnhub articles for the lookback window ending on sim_date.

    Returns raw article dicts (each has 'headline' and 'datetime' Unix timestamp).
    The 1s sleep fires only on real API calls, not cache hits.
    """
    import os
    import time
    import requests

    date_str = str(sim_date)[:10]
    cache_key = (symbol.upper(), date_str)
    if cache_key in _finnhub_day_cache:
        return _finnhub_day_cache[cache_key]

    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        _finnhub_day_cache[cache_key] = []
        return []

    if isinstance(sim_date, datetime):
        end_date = sim_date.date()
    else:
        end_date = sim_date
    start_date = end_date - timedelta(days=lookback_days)

    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": start_date.strftime("%Y-%m-%d"),
                "to": end_date.strftime("%Y-%m-%d"),
                "token": key,
            },
            timeout=10,
        )
        time.sleep(1)  # Stay under 60 calls/min free-tier limit
        articles = r.json() if r.ok else []
    except Exception as e:
        log.debug(f"[backtest_news] Finnhub fetch failed for {symbol} on {date_str}: {e}")
        articles = []

    _finnhub_day_cache[cache_key] = articles
    return articles


def backtest_news_signal(
    symbol: str,
    as_of_date,
    newsapi_key: str = "",
    lookback_days: int = 3,
) -> dict[str, Any]:
    """Fetch headlines for symbol published up to as_of_date (time-aware).

    Sources tried in order: Alpaca News → Finnhub (FINNHUB_API_KEY) → NewsAPI.
    Results are cached per (symbol, day) so each API is called only once per
    ticker per simulation day; each intraday cycle then filters articles to
    those published at or before the cycle time, preventing lookahead bias.

    Falls back to neutral 0.0 when no keys are set or all calls fail.
    Scoring is lexicon-based (same weights as the live news_sentiment module).
    """
    from ..analysis.news_sentiment import POSITIVE, NEGATIVE

    if isinstance(as_of_date, datetime):
        end_dt = as_of_date
    else:
        end_dt = datetime.combine(as_of_date, time(hour=9, minute=30))
    start_dt = end_dt - timedelta(days=lookback_days)

    headlines: list[str] = []
    source_label = "news"
    end_ts = end_dt.timestamp()

    # 1. Alpaca News — goes back to 2015, same credentials as price data
    articles = _alpaca_news_fetch_day(symbol, as_of_date, lookback_days)
    if articles:
        filtered = [a for a in articles if a.get("datetime", 0) <= end_ts]
        headlines = [a["headline"] for a in filtered[:10] if a.get("headline")]
        if headlines:
            source_label = "alpaca"

    # 2. Finnhub — fetch once per day (cached), filter by cycle time
    if not headlines:
        articles = _finnhub_fetch_day(symbol, as_of_date, lookback_days)
        if articles:
            filtered = [a for a in articles if a.get("datetime", 0) <= end_ts]
            headlines = [a["headline"] for a in filtered[:10] if a.get("headline")]
            if headlines:
                source_label = "finnhub"

    # 3. Fall back to NewsAPI
    if not headlines and newsapi_key:
        try:
            import requests
            r = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": symbol,
                    "from": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "to": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": 10,
                    "apiKey": newsapi_key,
                },
                timeout=10,
            )
            if r.ok:
                headlines = [a.get("title", "") for a in r.json().get("articles", [])[:10] if a.get("title")]
                if headlines:
                    source_label = "newsapi"
        except Exception as e:
            log.debug(f"[backtest_news] NewsAPI failed for {symbol}: {e}")

    if not headlines:
        return {
            "symbol": symbol, "source": "news", "score": 0.0,
            "reason": "no news available - scored neutral", "details": {"headlines": []},
        }

    pos = neg = 0
    for h in headlines:
        text = (h or "").lower()
        pos += sum(1 for w in POSITIVE if w in text)
        neg += sum(1 for w in NEGATIVE if w in text)
    total = pos + neg

    try:
        from ..utils.config import load_config as _lc
        _min_sig = int(_lc().get("signals", {}).get("news", {}).get("min_polarized_signals", 3))
    except Exception:
        _min_sig = 3

    if total == 0:
        score, reason = 0.0, f"no polarized terms in {len(headlines)} headlines"
    elif total < _min_sig:
        score, reason = 0.0, f"insufficient signal ({total} term(s) < min {_min_sig}) — scored neutral"
    else:
        raw = (pos - neg) / total
        score = float(max(-1.0, min(1.0, raw)))
        reason = f"lexicon ({source_label}): {pos} pos / {neg} neg in {len(headlines)} headlines"

    return {
        "symbol": symbol, "source": "news", "score": score,
        "reason": reason,
        "details": {"headlines": headlines[:5], "as_of": str(as_of_date)},
    }


# ------------------------------------------------------------------ LLM

def backtest_llm_signal(
    symbol: str,
    tech: dict,
    news: dict,
    breadth: dict,
    position_qty: float,
    regime: dict,
    as_of_date,
    deep_score: dict | None = None,
    similarity_line: str = "",
) -> dict[str, Any]:
    """LLM advisor with temporal guard to prevent lookahead bias.

    Injects a system-prompt prefix: "Analyze AS OF <date>. Don't use data after."
    Also includes the week's deep research score in the prompt when available,
    so the advisor sees fundamental quality, risk profile, and prior thesis
    alongside the intraday technical/news signals.
    """
    from ..analysis.llm_advisor import SYSTEM_PROMPT, _build_user_prompt, _format_deep_score_block
    from ..utils.llm_client import chat, llm_available, extract_json_object, provider_label
    from ..utils.config import load_config

    ok, why = llm_available()
    if not ok:
        return {
            "symbol": symbol, "source": "llm", "score": 0.0, "action": "HOLD",
            "confidence": 0.0, "reason": f"LLM not available: {why}", "details": {},
        }

    if isinstance(as_of_date, datetime):
        date_str = as_of_date.strftime("%Y-%m-%d")
    elif hasattr(as_of_date, "strftime"):
        date_str = as_of_date.strftime("%Y-%m-%d")
    else:
        date_str = str(as_of_date)

    temporal_prefix = (
        f"IMPORTANT — BACKTEST CONTEXT: You are analyzing this stock AS OF {date_str}. "
        f"Do NOT reference any price movements, earnings results, news, analyst upgrades, "
        f"or any other events that occurred after {date_str}. "
        f"Treat all signal data below as if today is {date_str}.\n\n"
    )
    system = temporal_prefix + SYSTEM_PROMPT

    ds_block = _format_deep_score_block(deep_score) if deep_score else ""
    prompt = _build_user_prompt(
        symbol, tech, news, breadth, position_qty,
        track_record="",
        deep_score_block=ds_block,
        similarity_line=similarity_line,
        regime_label=str(regime.get("label", "")) if isinstance(regime, dict) else "",
    )
    cfg = load_config()

    try:
        text = chat(
            prompt=prompt,
            system=system,
            max_tokens=int(cfg["llm"]["max_tokens_advisor"]),
            temperature=0.2,
        )
        data = extract_json_object(text)
        score = float(max(-1, min(1, data.get("score", 0))))
        return {
            "symbol": symbol, "source": "llm", "score": score,
            "action": data.get("action", "HOLD"),
            "confidence": float(data.get("confidence", 0.0)),
            "reason": data.get("reason", "")[:500],
            "details": {"provider": provider_label(), "as_of": date_str},
        }
    except Exception as e:
        log.warning(f"[backtest_llm] {symbol} failed: {e}")
        return {
            "symbol": symbol, "source": "llm", "score": 0.0, "action": "HOLD",
            "confidence": 0.0, "reason": f"LLM error: {e}", "details": {},
        }


# ------------------------------------------------------------------ pre-entry filters

def backtest_gap_up(cache: DataCache, symbol: str, sim_date) -> tuple[bool, float]:
    """True if today's open gapped up more than threshold vs previous close.

    Uses cached daily bars — no live yfinance call. Mirrors check_gap_up() from
    position_reviewer.py but safe for historical simulation.
    """
    from ..utils.config import load_config
    threshold = float(load_config().get("trading", {}).get("gap", {}).get("up_threshold_pct", 0.05))
    bars = cache.daily_bars(symbol, sim_date)
    if len(bars) < 2:
        return False, 0.0
    try:
        prev_close = float(bars.iloc[-2].get("Close", bars.iloc[-2].get("close", 0)))
        today_open = float(bars.iloc[-1].get("Open", bars.iloc[-1].get("open", 0)))
    except Exception:
        return False, 0.0
    if prev_close <= 0:
        return False, 0.0
    gap_pct = (today_open - prev_close) / prev_close
    return gap_pct >= threshold, float(gap_pct)


def backtest_volume_ok(cache: DataCache, symbol: str, sim_date) -> tuple[bool, float]:
    """True if today's volume meets the minimum ratio vs 60-day average.

    Uses cached daily bars — no live yfinance call. Mirrors check_volume_confirmation()
    from position_reviewer.py but safe for historical simulation.
    """
    from ..utils.config import load_config
    min_ratio = float(load_config().get("trading", {}).get("volume", {}).get("min_ratio", 0.50))
    bars = cache.daily_bars(symbol, sim_date)
    if len(bars) < 20:
        return True, 1.0  # fail open on insufficient history
    vol_col = "Volume" if "Volume" in bars.columns else "volume"
    if vol_col not in bars.columns:
        return True, 1.0
    today_vol = float(bars[vol_col].iloc[-1])
    lookback = bars[vol_col].iloc[-61:-1]  # previous 60 days, exclude today
    if lookback.empty:
        return True, 1.0
    avg_vol = float(lookback.mean())
    if avg_vol <= 0:
        return True, 1.0
    ratio = today_vol / avg_vol
    return ratio >= min_ratio, float(ratio)


def backtest_earnings_blackout(
    symbol: str,
    sim_date,
    deep_score_entry: dict | None,
    news_score: float = 0.0,
) -> tuple[bool, str]:
    """True if sim_date falls within the earnings blackout window.

    Uses next_earnings from the deep score entry (computed with as_of_date guard).
    Mirrors check_earnings_blackout() from position_reviewer.py but uses cached data.
    """
    from ..utils.config import load_config
    from datetime import date as date_type
    earn_cfg = load_config().get("trading", {}).get("earnings", {}) or {}
    blackout_days = int(earn_cfg.get("blackout_days", 3))
    min_news = float(earn_cfg.get("min_news_score_to_trade", 0.5))
    if not deep_score_entry:
        return False, ""
    next_earn = deep_score_entry.get("next_earnings")
    if not next_earn:
        return False, ""
    try:
        edate = date_type.fromisoformat(str(next_earn)[:10])
        sim = sim_date if isinstance(sim_date, date_type) else sim_date.date()
        days_away = (edate - sim).days
        if 0 <= days_away <= blackout_days:
            if news_score >= min_news:
                return False, f"near earnings ({days_away}d) but strong news catalyst"
            return True, f"earnings in {days_away}d"
    except Exception:
        pass
    return False, ""
