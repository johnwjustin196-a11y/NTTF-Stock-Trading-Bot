"""Pre-market screener.

Builds a shortlist of tickers to actively consider during the trading session.

The shortlist = static watchlist (from settings.yaml) + dynamic filter pass over
top pre-market movers. We keep it at most `shortlist_size` names.

Each symbol is tagged with a trend classification (6-month & 30-day) so the
decision engine and the dashboard can see at a glance whether a pick is with
or against the tape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yfinance as yf

from ..utils.config import load_config
from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)


def build_shortlist() -> list[str]:
    cfg = load_config()
    sc = cfg["screener"]

    # ---------------- LARGE-CAP BUCKET ----------------
    shortlist: list[str] = list(dict.fromkeys(sc["watchlist"]))  # preserve order, de-dupe

    # Dynamic: pull top gainers, losers, most actives
    movers = _pull_movers(sc["top_movers"])
    log.info(f"Fetched {len(movers)} candidate movers")

    large_filters = {
        "min_price": sc["min_price"],
        "max_price": sc["max_price"],
        "min_avg_volume": sc["min_avg_volume"],
        "min_market_cap": sc["min_market_cap"],
    }
    for sym in movers:
        if sym in shortlist:
            continue
        if _passes_filters(sym, large_filters):
            shortlist.append(sym)
        if len(shortlist) >= sc["shortlist_size"]:
            break

    shortlist = shortlist[: sc["shortlist_size"]]
    log.info(f"Large-cap bucket: {len(shortlist)} names")

    # ---------------- SMALL-CAP BUCKET ----------------
    small_cfg = sc.get("small_caps", {}) or {}
    small_caps_added: list[str] = []
    if small_cfg.get("enabled"):
        target = int(small_cfg.get("target_count", 12))
        small_filters = {
            "min_price": small_cfg.get("min_price", 1.0),
            "max_price": small_cfg.get("max_price", 15.0),
            "min_avg_volume": small_cfg.get("min_avg_volume", 500000),
            "min_market_cap": small_cfg.get("min_market_cap", 100_000_000),
            "max_market_cap": small_cfg.get("max_market_cap", 3_000_000_000),
        }
        # Broad candidate pool — small-cap-specific screens first, then
        # fall back to the day-gainer/most-active lists we already pulled.
        candidates = _pull_small_cap_movers(pull_each=max(30, target * 3))
        candidates += movers  # day-gainers/losers/most-actives may include small-caps

        for sym in candidates:
            if sym in shortlist or sym in small_caps_added:
                continue
            if _passes_filters(sym, small_filters):
                small_caps_added.append(sym)
            if len(small_caps_added) >= target:
                break

        log.info(f"Small-cap bucket: {len(small_caps_added)} names -> {small_caps_added}")
        shortlist.extend(small_caps_added)

    # Annotate each symbol with trend classification for downstream visibility
    trend_map = _tag_trends(shortlist)

    out_path = Path(cfg["paths"]["shortlist_file"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "date": today_str(),
            "symbols": shortlist,
            "trends": trend_map,
        }, f, indent=2)

    trend_summary = ", ".join(f"{s}={trend_map.get(s,{}).get('label','?')}" for s in shortlist[:8])
    log.info(f"Shortlist ({len(shortlist)}): {shortlist}")
    log.info(f"Trends: {trend_summary}...")
    return shortlist


def load_shortlist() -> list[str]:
    cfg = load_config()
    path = Path(cfg["paths"]["shortlist_file"])
    if not path.exists():
        return cfg["screener"]["watchlist"]
    with open(path) as f:
        data = json.load(f)
    if data.get("date") != today_str():
        log.info("Shortlist is stale; rebuilding")
        return build_shortlist()
    return data.get("symbols", [])


def load_shortlist_trends() -> dict[str, dict]:
    """Return the trend map saved alongside the shortlist, or {}."""
    cfg = load_config()
    path = Path(cfg["paths"]["shortlist_file"])
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("trends", {}) or {}
    except Exception:
        return {}


# -------------------------------------------------------------- internals

def _pull_movers(top_n: int) -> list[str]:
    """Fetch top movers via whichever yfinance screener API is available.

    yfinance's screener shape has changed several times across versions; this
    function tries each known entry point in order and falls back to scraping
    the public Yahoo predefined-screener endpoint. Always returns a list —
    empty on total failure, so the caller can still proceed from the watchlist.
    """
    screens = ("day_gainers", "day_losers", "most_actives")
    symbols: list[str] = []

    # 1. Newer API: yfinance.Screener() (>=0.2.40)
    for s in screens:
        try:
            from yfinance import Screener  # type: ignore
            sc = Screener()
            sc.set_predefined_body(s)
            data = sc.response or {}
            quotes = data.get("quotes") or (data.get("finance", {}).get("result") or [{}])[0].get("quotes", [])
            for q in quotes[:top_n]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
        except Exception as e:
            log.debug(f"yfinance Screener {s} failed: {e}")

    if symbols:
        return list(dict.fromkeys(symbols))

    # 2. Older API: yfinance.screener.Screen(name)
    for s in screens:
        try:
            from yfinance import screener  # type: ignore
            res = screener.Screen(s).response.get("quotes", [])
            for q in res[:top_n]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
        except Exception as e:
            log.debug(f"yfinance screener.Screen {s} failed: {e}")

    if symbols:
        return list(dict.fromkeys(symbols))

    # 3. Last-resort: hit Yahoo's predefined-screener endpoint directly
    try:
        import requests
        for s in screens:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": s, "count": top_n, "start": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()
            quotes = (data.get("finance", {}).get("result") or [{}])[0].get("quotes", [])
            for q in quotes[:top_n]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
    except Exception as e:
        log.debug(f"Yahoo predefined-screener fallback failed: {e}")

    return list(dict.fromkeys(symbols))


def _passes_filters(symbol: str, filters: dict[str, Any]) -> bool:
    """Check a symbol against a filter dict. Accepts any/all of:
    min_price, max_price, min_avg_volume, min_market_cap, max_market_cap.
    Missing keys are treated as unbounded.
    """
    try:
        t = yf.Ticker(symbol)
        info = _safe_info(t)
        price = float(info.get("regularMarketPrice") or info.get("previousClose") or 0)
        avg_vol = int(info.get("averageVolume") or 0)
        mcap = int(info.get("marketCap") or 0)

        if "min_price" in filters and price < filters["min_price"]:
            return False
        if "max_price" in filters and price > filters["max_price"]:
            return False
        if "min_avg_volume" in filters and avg_vol < filters["min_avg_volume"]:
            return False
        if mcap:
            if "min_market_cap" in filters and mcap < filters["min_market_cap"]:
                return False
            if "max_market_cap" in filters and mcap > filters["max_market_cap"]:
                return False
        if price is not None and price < 15.0:
            return False
        try:
            import pandas_ta as _pta
            _hist = yf.Ticker(symbol).history(period="1mo", auto_adjust=True)
            if _hist is not None and len(_hist) >= 14:
                _atr_s = _pta.atr(_hist["High"], _hist["Low"], _hist["Close"], length=14)
                if _atr_s is not None and len(_atr_s) > 0 and not _atr_s.isna().iloc[-1]:
                    _atr_val = float(_atr_s.iloc[-1])
                    if price and _atr_val / price > 0.04:
                        return False
        except Exception:
            pass
        return True
    except Exception as e:
        log.debug(f"filter {symbol} failed: {e}")
        return False


def _pull_small_cap_movers(pull_each: int = 30) -> list[str]:
    """Pull candidates from small-cap-specific Yahoo predefined screens.

    Screens we try: small_cap_gainers, aggressive_small_caps. If those are
    unavailable (API flux), the caller will still have day-gainer movers as a
    fallback pool — filtering does the rest.
    """
    screens = ("small_cap_gainers", "aggressive_small_caps")
    symbols: list[str] = []

    # 1. Newer yfinance.Screener()
    for s in screens:
        try:
            from yfinance import Screener  # type: ignore
            sc_obj = Screener()
            sc_obj.set_predefined_body(s)
            data = sc_obj.response or {}
            quotes = data.get("quotes") or (
                (data.get("finance", {}).get("result") or [{}])[0].get("quotes", [])
            )
            for q in quotes[:pull_each]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
        except Exception as e:
            log.debug(f"yfinance Screener {s} failed: {e}")

    if symbols:
        return list(dict.fromkeys(symbols))

    # 2. Older yfinance.screener.Screen()
    for s in screens:
        try:
            from yfinance import screener  # type: ignore
            res = screener.Screen(s).response.get("quotes", [])
            for q in res[:pull_each]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
        except Exception as e:
            log.debug(f"yfinance screener.Screen {s} failed: {e}")

    if symbols:
        return list(dict.fromkeys(symbols))

    # 3. Direct HTTP to Yahoo's public predefined-screener endpoint
    try:
        import requests
        for s in screens:
            r = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": s, "count": pull_each, "start": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()
            quotes = (data.get("finance", {}).get("result") or [{}])[0].get("quotes", [])
            for q in quotes[:pull_each]:
                sym = q.get("symbol")
                if sym:
                    symbols.append(sym)
    except Exception as e:
        log.debug(f"Yahoo small-cap fallback failed: {e}")

    return list(dict.fromkeys(symbols))


def _safe_info(ticker: yf.Ticker) -> dict:
    """yfinance .info is fragile; use fast_info when available."""
    try:
        fi = ticker.fast_info
        return {
            "regularMarketPrice": getattr(fi, "last_price", None),
            "previousClose": getattr(fi, "previous_close", None),
            "averageVolume": getattr(fi, "three_month_average_volume", None)
                or getattr(fi, "ten_day_average_volume", None),
            "marketCap": getattr(fi, "market_cap", None),
        }
    except Exception:
        return ticker.info or {}


def get_premarket_ratings(symbols: list[str], broker=None) -> dict[str, dict]:
    """Compute tech + news composite for each symbol. No LLM, no deep score.

    Returns a dict keyed by symbol with tech_score, news_score, composite, weak.
    On per-ticker failure, falls back to neutral (0.0) scores — ticker is NOT
    marked weak on Condition B alone without a signal.
    """
    from ..analysis.technicals import technical_signal
    from ..analysis.news_sentiment import news_signal

    cfg = load_config()
    weak_threshold = float(cfg.get("screener", {}).get("premarket_weak_threshold", -0.10))

    ratings: dict[str, dict] = {}
    for sym in symbols:
        try:
            tech = technical_signal(broker, sym)
            news = news_signal(sym)
            composite = tech["score"] * 0.6 + news["score"] * 0.4
            ratings[sym] = {
                "tech_score": tech["score"],
                "news_score": news["score"],
                "composite": composite,
                "weak": composite < weak_threshold,
            }
        except Exception as e:
            log.debug(f"premarket rating {sym} failed: {e}")
            ratings[sym] = {"tech_score": 0.0, "news_score": 0.0, "composite": 0.0, "weak": False}
    return ratings


def filter_and_replace_weak_tickers(
    shortlist: list[str],
    premarket_ratings: dict[str, dict],
    broker=None,
) -> list[str]:
    """Remove tickers failing quality checks and backfill from movers.

    Weak = deep_score < 55 (below B) OR pre-market tech+news composite weak.
    Tickers with a MISSING score are scored first before judgment.
    If the final list is empty, logs a warning — caller is responsible for
    closing positions and skipping decision cycles.

    Returns the filtered + backfilled symbol list (may be shorter than original).
    """
    from ..analysis.deep_scorer import get_score, score_ticker

    cfg = load_config()
    sc = cfg["screener"]
    weak_threshold = float(sc.get("premarket_weak_threshold", -0.10))
    target_size = len(shortlist)

    def _is_weak(sym: str) -> bool:
        entry = get_score(sym)
        if entry is not None and entry.get("score", 50) < 55:
            return True
        return bool(premarket_ratings.get(sym, {}).get("weak", False))

    # Step 1: score any tickers still missing a fresh score
    stale = [s for s in shortlist if get_score(s) is None]
    if stale:
        log.info(f"[watchlist-cull] scoring {len(stale)} unscored tickers before cull: {stale}")
        for sym in stale:
            try:
                score_ticker(sym)
            except Exception as e:
                log.debug(f"[watchlist-cull] score {sym} failed: {e}")

    # Step 2: evaluate and split
    kept: list[str] = []
    dropped: list[str] = []
    for sym in shortlist:
        entry = get_score(sym)
        rating = premarket_ratings.get(sym, {})
        ds = entry.get("score", 50) if entry else None
        composite = rating.get("composite", None)
        if _is_weak(sym):
            dropped.append(sym)
            _why = (
                f"deep_score={ds:.0f} < 55" if (ds is not None and ds < 55)
                else f"composite={composite:.2f} < {weak_threshold:.2f}" if composite is not None
                else "no_score"
            )
            log.debug(f"[watchlist-cull] {sym}: {_why}")
        else:
            kept.append(sym)

    if not dropped:
        return kept

    log.info(f"[watchlist-cull] dropped {len(dropped)} weak tickers: {dropped}")

    # Step 3: backfill from movers pool
    large_filters = {
        "min_price": sc["min_price"],
        "max_price": sc["max_price"],
        "min_avg_volume": sc["min_avg_volume"],
        "min_market_cap": sc["min_market_cap"],
    }
    candidates = _pull_movers(len(dropped) * 3)
    added: list[str] = []
    for sym in candidates:
        if sym in kept or sym in added or sym in shortlist:
            continue
        if not _passes_filters(sym, large_filters):
            continue
        # Score candidate if no fresh score
        if get_score(sym) is None:
            try:
                score_ticker(sym)
            except Exception as e:
                log.debug(f"[watchlist-cull] candidate score {sym} failed: {e}")
        # Compute tech+news rating for this new candidate
        if sym not in premarket_ratings:
            try:
                from ..analysis.technicals import technical_signal
                from ..analysis.news_sentiment import news_signal
                tech = technical_signal(broker, sym)
                news = news_signal(sym)
                composite = tech["score"] * 0.6 + news["score"] * 0.4
                premarket_ratings[sym] = {
                    "tech_score": tech["score"],
                    "news_score": news["score"],
                    "composite": composite,
                    "weak": composite < weak_threshold,
                }
            except Exception as e:
                log.debug(f"[watchlist-cull] rating candidate {sym} failed: {e}")
        if not _is_weak(sym):
            added.append(sym)
            log.debug(f"[watchlist-cull] added replacement: {sym}")
        if len(kept) + len(added) >= target_size:
            break

    if added:
        log.info(f"[watchlist-cull] backfilled {len(added)} replacements: {added}")

    final = kept + added

    if not final:
        log.warning("[watchlist-cull] watchlist EMPTY — no qualifying tickers found after cull")
    elif len(final) < target_size:
        log.info(f"[watchlist-cull] watchlist shrank {target_size} -> {len(final)} tickers")

    # Persist updated shortlist (preserving existing trend tags for kept symbols)
    _save_filtered_shortlist(final, shortlist)
    return final


def _save_filtered_shortlist(symbols: list[str], previous: list[str]) -> None:
    """Write updated symbol list to shortlist.json, preserving trend data."""
    cfg = load_config()
    out_path = Path(cfg["paths"]["shortlist_file"])
    existing_trends: dict = {}
    if out_path.exists():
        try:
            import json as _json
            with open(out_path) as f:
                existing_trends = _json.load(f).get("trends", {})
        except Exception:
            pass
    # Tag trends for any brand-new tickers not in previous
    new_syms = [s for s in symbols if s not in previous]
    if new_syms:
        new_trends = _tag_trends(new_syms)
        existing_trends.update(new_trends)
    kept_trends = {s: existing_trends[s] for s in symbols if s in existing_trends}
    import json as _json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        _json.dump({
            "date": today_str(),
            "symbols": symbols,
            "trends": kept_trends,
        }, f, indent=2)


def _tag_trends(symbols: list[str]) -> dict[str, dict]:
    """Classify each symbol's trend. Isolated failures don't break the list."""
    # Lazy import to avoid pulling in analysis at module import time
    from ..analysis.trend import trend_classification

    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            t = trend_classification(sym)
            out[sym] = {
                "label": t.get("label"),
                "short": (t.get("short") or {}).get("label"),
                "long": (t.get("long") or {}).get("label"),
                "change_6mo": (t.get("long") or {}).get("change_pct"),
                "change_30d": (t.get("short") or {}).get("change_pct"),
            }
        except Exception as e:
            log.debug(f"trend tag {sym} failed: {e}")
            out[sym] = {"label": "unknown"}
    return out
