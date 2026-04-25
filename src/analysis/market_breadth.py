"""Market breadth / regime signal.

Not per-ticker. Returns a single regime score in [-1, 1] applied uniformly to
every candidate — the idea being "don't buy into a broken tape regardless of
how good the individual chart looks, and don't short into rip-your-face-off
strength".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import yfinance as yf

from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)

_BREADTH_CACHE: dict = {"at": None, "result": None}
_BREADTH_TTL_SECONDS = 900


def breadth_signal() -> dict[str, Any]:
    if _BREADTH_CACHE["at"] is not None:
        if (datetime.now() - _BREADTH_CACHE["at"]).total_seconds() < _BREADTH_TTL_SECONDS:
            return _BREADTH_CACHE["result"]

    cfg = load_config()["signals"]["breadth"]
    details: dict[str, Any] = {}
    scores = []

    # 1. Index trend: SPY/QQQ/IWM closing above 20-day SMA = positive
    for sym in cfg["index_symbols"]:
        s = _trend_score(sym)
        if s is not None:
            scores.append(s)
            details[f"{sym}_trend"] = s

    # 2. VIX level: high = risk-off (invert sign)
    try:
        hist = yf.Ticker(cfg["vix_symbol"]).history(period="3mo", auto_adjust=False)
        if not hist.empty:
            vix = float(hist["Close"].iloc[-1])
            vix_ma = float(hist["Close"].tail(20).mean())
            # Compare VIX to its own 20-day MA. High & rising = fearful.
            vix_score = -np.tanh((vix - vix_ma) / 3.0)
            scores.append(float(vix_score))
            details["vix"] = vix
            details["vix_ma20"] = vix_ma
            details["vix_score"] = float(vix_score)
    except Exception as e:
        log.warning(f"VIX read failed: {e}")

    # 3. Sector breadth: % of sector ETFs above their 20-day SMA
    up = 0
    total = 0
    for sym in cfg["sectors"]:
        s = _trend_score(sym)
        if s is not None:
            total += 1
            up += 1 if s > 0 else 0
    if total:
        frac = up / total
        sector_score = (frac - 0.5) * 2  # map [0,1] → [-1,1]
        scores.append(sector_score)
        details["sector_breadth_pct"] = frac
        details["sector_breadth_score"] = sector_score

    if not scores:
        _result = {"symbol": "_market", "source": "breadth", "score": 0.0,
                   "reason": "no breadth data", "details": details}
        _BREADTH_CACHE["at"] = datetime.now()
        _BREADTH_CACHE["result"] = _result
        return _result

    composite = float(np.mean(scores))
    composite = max(-1.0, min(1.0, composite))

    reason = (
        f"indices+sectors+vix composite: "
        + ", ".join(f"{k}={v:.2f}" for k, v in details.items() if isinstance(v, (int, float)))[:180]
    )
    _result = {"symbol": "_market", "source": "breadth", "score": composite,
               "reason": reason, "details": details}
    _BREADTH_CACHE["at"] = datetime.now()
    _BREADTH_CACHE["result"] = _result
    return _result


def _trend_score(symbol: str) -> float | None:
    try:
        hist = yf.Ticker(symbol).history(period="3mo", auto_adjust=False)
        if hist.empty or len(hist) < 25:
            return None
        last = float(hist["Close"].iloc[-1])
        sma = float(hist["Close"].tail(20).mean())
        return float(np.tanh((last - sma) / sma * 20))
    except Exception as e:
        log.debug(f"trend {symbol} failed: {e}")
        return None
