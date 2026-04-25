"""Per-ticker trend classification.

Looks at two horizons:
  * Long lens: last ~6 months of daily closes
  * Short lens: last ~30 days

Returns one of: "uptrend" | "downtrend" | "sideways", plus details for the
advisor/journal. The classification is deliberately simple & mechanical so a
human can look at the chart and agree. Rules of thumb:
  - Uptrend:   slope > 0 AND last close > 50-day SMA AND SMA20 > SMA50
  - Downtrend: slope < 0 AND last close < 50-day SMA AND SMA20 < SMA50
  - Otherwise sideways.

We return BOTH horizons; the caller typically uses short-term for sizing
decisions ("am I trading against the tape?") and long-term for context.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from ..utils.logger import get_logger

log = get_logger(__name__)


def _classify_window(closes: pd.Series) -> dict[str, Any]:
    """Classify a slice of closes as uptrend/downtrend/sideways."""
    if closes is None or len(closes) < 10:
        return {"label": "unknown", "reason": "insufficient data",
                "slope_pct": 0.0, "change_pct": 0.0}

    closes = closes.dropna().astype(float)
    n = len(closes)
    first = float(closes.iloc[0])
    last = float(closes.iloc[-1])
    change_pct = (last - first) / first if first else 0.0

    # Linear regression slope, normalized by average price
    x = np.arange(n, dtype=float)
    y = closes.values
    try:
        slope, _ = np.polyfit(x, y, 1)
    except Exception:
        slope = 0.0
    avg = float(np.mean(y)) or 1.0
    # slope per bar as % of avg price, annualized-ish by window length
    slope_pct = float(slope / avg) * n  # total rise implied by slope over window

    # SMAs
    sma_short_win = max(5, n // 10)        # ~10% of window
    sma_long_win = max(sma_short_win + 5, n // 3)
    sma_s = float(closes.tail(sma_short_win).mean())
    sma_l = float(closes.tail(sma_long_win).mean())

    up_votes = 0
    down_votes = 0
    if slope_pct > 0.01:
        up_votes += 1
    elif slope_pct < -0.01:
        down_votes += 1

    if last > sma_l:
        up_votes += 1
    elif last < sma_l:
        down_votes += 1

    if sma_s > sma_l:
        up_votes += 1
    elif sma_s < sma_l:
        down_votes += 1

    if up_votes >= 2 and down_votes == 0:
        label = "uptrend"
    elif down_votes >= 2 and up_votes == 0:
        label = "downtrend"
    else:
        label = "sideways"

    return {
        "label": label,
        "change_pct": change_pct,
        "slope_pct": slope_pct,
        "last": last,
        "sma_short": sma_s,
        "sma_long": sma_l,
        "bars": n,
    }


def trend_classification(symbol: str) -> dict[str, Any]:
    """Classify a symbol on two horizons.

    Returns:
        {
          "symbol": str,
          "long":  {label, change_pct, slope_pct, ...},   # ~6mo
          "short": {label, change_pct, slope_pct, ...},   # ~30d
          "label": str,        # blended label (see below)
          "reason": str,
        }
    Blended label rules:
        - both agree   → that label
        - long uptrend + short downtrend → "pullback_in_uptrend"
        - long downtrend + short uptrend → "bounce_in_downtrend"
        - anything with 'sideways' on either side → follow the non-sideways one
    """
    try:
        hist = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=False)
    except Exception as e:
        log.warning(f"trend {symbol}: history fetch failed: {e}")
        return _empty(symbol, f"history fetch failed: {e}")

    if hist is None or hist.empty or len(hist) < 30:
        return _empty(symbol, "insufficient history (<30 bars)")

    closes_6m = hist["Close"].tail(130)   # ~6mo of trading days
    closes_30d = hist["Close"].tail(30)

    long_res = _classify_window(closes_6m)
    short_res = _classify_window(closes_30d)

    blended = _blend(long_res["label"], short_res["label"])

    reason = (
        f"6mo: {long_res['label']} ({long_res['change_pct']:+.1%}) | "
        f"30d: {short_res['label']} ({short_res['change_pct']:+.1%}) | "
        f"blended: {blended}"
    )

    return {
        "symbol": symbol,
        "label": blended,
        "long": long_res,
        "short": short_res,
        "reason": reason,
    }


def _blend(long_lbl: str, short_lbl: str) -> str:
    if long_lbl == short_lbl:
        return long_lbl
    if long_lbl == "uptrend" and short_lbl == "downtrend":
        return "pullback_in_uptrend"
    if long_lbl == "downtrend" and short_lbl == "uptrend":
        return "bounce_in_downtrend"
    # If one side is sideways/unknown, prefer the decided one
    if short_lbl in ("sideways", "unknown"):
        return long_lbl
    if long_lbl in ("sideways", "unknown"):
        return short_lbl
    return "mixed"


def is_downtrend(trend: dict[str, Any]) -> bool:
    """Sizing gate: trade is considered against-the-tape if the *short* (30d)
    window reads downtrend, or blended is downtrend / bounce-in-downtrend."""
    if not trend:
        return False
    short = (trend.get("short") or {}).get("label", "")
    blended = trend.get("label", "")
    return short == "downtrend" or blended in ("downtrend", "bounce_in_downtrend")


def _empty(symbol: str, reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "label": "unknown",
        "long": {"label": "unknown"},
        "short": {"label": "unknown"},
        "reason": reason,
    }
