"""Technical indicator signal.

Produces a score in [-1, 1] for a ticker based on classic indicators.
Each indicator returns its own sub-score and a short human-readable reason;
we average them to keep the contribution math transparent.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from ..broker.base import Broker
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)

_SECTOR_ETF = {
    "Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

FIB_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.786)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = -delta.clip(upper=0).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().fillna(0.0)


def _finite_float(value, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _finite_scores(values: list) -> list[float]:
    out: list[float] = []
    for value in values:
        score = _finite_float(value)
        if score is not None:
            out.append(float(np.clip(score, -1.0, 1.0)))
    return out


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    """Average Directional Index — trend strength, 0-100 (>25 = real trend)."""
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0),   up_move,   0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    alpha = 1.0 / period
    atr_w     = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di   = 100 * pd.Series(plus_dm,  index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di  = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx  = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean()


def _bollinger(close: pd.Series, period: int, n_std: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower) Bollinger Bands."""
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return mid + n_std * sigma, mid, mid - n_std * sigma


def _obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative directional volume."""
    direction = np.sign(df["close"].astype(float).diff()).fillna(0)
    return (direction * df["volume"].astype(float)).cumsum()


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — caller is responsible for trimming to the right session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


def _fib_score(
    bars: pd.DataFrame, lookback: int, tolerance: float
) -> tuple[float | None, dict]:
    """Fibonacci retracement with support/resistance flip detection.

    Finds swing high and low over `lookback` bars. Scans recent candle closes to
    detect if the nearest Fib level has been "flipped":
      - Price closed above it (was resistance, now support) → positive score when near
      - Price closed below it (was support, now resistance) → negative score when near
    Falls back to direction heuristic (low-before-high = uptrend support,
    high-before-low = downtrend resistance) when no flip is detected.
    """
    n = min(lookback, len(bars))
    if n < 20:
        return None, {"error": "insufficient bars"}
    window = bars.iloc[-n:]

    high_arr  = window["high"].astype(float).values
    low_arr   = window["low"].astype(float).values
    close_arr = bars["close"].astype(float).values
    if (
        not np.isfinite(high_arr).any()
        or not np.isfinite(low_arr).any()
        or not np.isfinite(close_arr[-1])
    ):
        return None, {"error": "invalid price data"}

    swing_high = float(np.nanmax(high_arr))
    swing_low  = float(np.nanmin(low_arr))
    if not np.isfinite(swing_high) or not np.isfinite(swing_low) or swing_high <= swing_low:
        return None, {"error": "no price range"}

    last = float(close_arr[-1])
    move = swing_high - swing_low

    idx_high = int(np.nanargmax(high_arr))
    idx_low  = int(np.nanargmin(low_arr))
    baseline_uptrend = idx_low < idx_high   # low came first → uptrend pullback

    fib_prices = {r: swing_low + r * move for r in FIB_RATIOS}

    def _level_direction(level: float) -> str:
        """Walk recent closes backwards; first cross determines flipped status."""
        scan = close_arr[-min(20, len(close_arr)):]
        for i in range(len(scan) - 1, 0, -1):
            if scan[i] > level and scan[i - 1] <= level:
                return "support"     # crossed above → was resistance, now support
            if scan[i] < level and scan[i - 1] >= level:
                return "resistance"  # crossed below → was support, now resistance
        return "baseline"

    nearest_r = min(FIB_RATIOS, key=lambda r: abs(last - fib_prices[r]))
    nearest_p = fib_prices[nearest_r]
    proximity  = abs(last - nearest_p) / nearest_p if nearest_p else 1.0

    level_dir  = _level_direction(nearest_p)
    if level_dir == "baseline":
        is_support = baseline_uptrend
    else:
        is_support = (level_dir == "support")

    if proximity > tolerance:
        score = 0.0
    else:
        proximity_factor = 1.0 - (proximity / tolerance)
        # 38.2%, 50%, 61.8% are the classic "golden" levels; 23.6% & 78.6% are weaker
        level_strength = 0.8 if nearest_r in (0.382, 0.500, 0.618) else 0.4
        score = level_strength * proximity_factor * (1.0 if is_support else -1.0)

    return float(np.clip(score, -1.0, 1.0)), {
        "swing_high":        round(swing_high, 4),
        "swing_low":         round(swing_low, 4),
        "nearest_fib_ratio": nearest_r,
        "nearest_fib_price": round(nearest_p, 4),
        "fib_proximity_pct": round(proximity * 100, 3),
        "fib_direction":     "support" if is_support else "resistance",
        "fib_level_status":  level_dir,
        "fib_levels":        {str(r): round(p, 4) for r, p in fib_prices.items()},
    }


def _compute_vwap_score(broker, symbol: str, timeframe: str) -> tuple[float | None, dict]:
    """Fetch intraday bars and return (score in [-1,1], details).
    Score is based on distance of current price to today's session VWAP,
    normalized. Positive = above VWAP (bullish), negative = below (bearish).
    """
    try:
        intraday = broker.get_bars(symbol, timeframe=timeframe, limit=400)
    except Exception as e:
        return None, {"error": f"no intraday bars: {e}"}
    if intraday.empty or len(intraday) < 5:
        return None, {"error": "insufficient intraday bars"}

    # Keep only the most recent session. For daily-indexed data that's the
    # last calendar day present in the index.
    try:
        last_day = intraday.index[-1].date()
        session = intraday[intraday.index.date == last_day]
    except Exception:
        session = intraday.tail(78)  # ~ one 5m session

    if session.empty or session["volume"].sum() <= 0:
        return None, {"error": "no session volume"}

    vwap = _finite_float(_vwap(session).iloc[-1])
    last = _finite_float(session["close"].iloc[-1])
    if vwap is None or vwap <= 0 or last is None or last <= 0:
        return None, {"error": "invalid vwap"}

    distance_pct = (last - vwap) / vwap
    # tanh scaled so a 0.5% distance ~ 0.25 score, 1% ~ 0.46, 2% ~ 0.76
    score = float(np.tanh(distance_pct * 50))
    return score, {
        "vwap": float(vwap),
        "last_intraday": float(last),
        "distance_pct": float(distance_pct),
    }


def technical_signal(broker: Broker, symbol: str, regime: str | None = None) -> dict[str, Any]:
    cfg = load_config()["signals"]["technicals"]
    try:
        bars = broker.get_bars(symbol, timeframe="1d", limit=200)
    except Exception as e:
        log.warning(f"{symbol}: could not load bars — {e}")
        return _empty(symbol, reason=f"no data: {e}")

    if bars.empty or len(bars) < max(cfg["sma_long"], cfg["macd_slow"]) + 5:
        return _empty(symbol, reason="insufficient history")

    close = bars["close"].astype(float)
    last = _finite_float(close.iloc[-1])
    if last is None or last <= 0:
        return _empty(symbol, reason="invalid latest close")

    rsi = _finite_float(_rsi(close, cfg["rsi_period"]).iloc[-1], 50.0)
    macd, macd_sig = _macd(close, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    macd_cross = _finite_float(macd.iloc[-1] - macd_sig.iloc[-1], 0.0)
    sma_s = _finite_float(close.rolling(cfg["sma_short"]).mean().iloc[-1])
    sma_l = _finite_float(close.rolling(cfg["sma_long"]).mean().iloc[-1])
    atr = _finite_float(_atr(bars, cfg["atr_period"]).iloc[-1], 0.0) or 0.0

    # --- individual sub-scores in [-1, 1] ---------------------------------

    # RSI: below buy → bullish, above sell → bearish; linear in between.
    # In a bullish regime overbought RSI signals momentum, not exhaustion —
    # cap the penalty so it doesn't dominate the composite and force premature exits.
    _regime_bullish = str(regime or "").lower() == "bullish"
    if rsi <= cfg["rsi_buy"]:
        rsi_score = +0.8
    elif rsi >= cfg["rsi_sell"]:
        rsi_score = -0.3 if _regime_bullish else -0.8
    else:
        rsi_score = -(rsi - 50) / 50

    # MACD: sign of histogram, magnitude capped
    macd_score = float(np.tanh((macd_cross or 0.0) / (atr if atr > 0 else 1.0)))

    # Trend: price vs SMAs (raw direction, then ADX-weighted below)
    if sma_s is None or sma_l is None:
        trend_raw = 0.0
    elif last > sma_s > sma_l:
        trend_raw = +0.6
    elif last < sma_s < sma_l:
        trend_raw = -0.6
    elif last > sma_l:
        trend_raw = +0.3
    else:
        trend_raw = -0.3

    # ADX: scale trend by trend strength. ADX < threshold → trend contribution → 0
    adx_period    = int(cfg.get("adx_period", 14))
    adx_threshold = float(cfg.get("adx_trend_threshold", 20))
    adx_val = 0.0
    adx_factor = 0.0
    try:
        adx_series = _adx(bars, adx_period)
        adx_val = float(adx_series.iloc[-1]) if np.isfinite(adx_series.iloc[-1]) else 0.0
        # Factor 0 when ADX <= threshold, approaches 1 for very strong trends
        adx_factor = float(np.tanh(max(0.0, adx_val - adx_threshold) / 15.0))
    except Exception:
        logging.warning("ADX unavailable for %s - trend sub-score using raw SMA only", symbol)
        adx_factor = 0.5  # neutral fallback - don't suppress trend entirely

    trend_score = trend_raw * adx_factor

    # Bollinger Bands: %B position — near lower band = oversold/bullish, near upper = overbought
    bb_period   = int(cfg.get("bb_period", 20))
    bb_std_mult = float(cfg.get("bb_std", 2.0))
    bb_score: float | None = None
    bb_pct_b: float | None = None
    bb_squeeze: bool = False
    try:
        upper_bb, mid_bb, lower_bb = _bollinger(close, bb_period, bb_std_mult)
        ub = _finite_float(upper_bb.iloc[-1])
        lb = _finite_float(lower_bb.iloc[-1])
        mid = _finite_float(mid_bb.iloc[-1])
        if ub is None or lb is None or mid is None or mid <= 0:
            raise ValueError("invalid bollinger band")
        band_width = (ub - lb) / mid
        # Squeeze: current width < 50% of 20-period average width
        avg_width = ((upper_bb - lower_bb) / mid_bb.replace(0, np.nan)).rolling(20).mean().iloc[-1]
        bb_squeeze = bool(np.isfinite(avg_width) and band_width < avg_width * 0.5)
        if ub > lb:
            pct_b = (last - lb) / (ub - lb)
            bb_pct_b = float(pct_b)
            # Map 0 → +1.0 (at lower band, oversold), 0.5 → 0, 1 → -1.0 (at upper, overbought).
            # In a bullish regime, riding the upper band is momentum — cap the penalty.
            bb_score = float(np.tanh(-(pct_b - 0.5) * 4))
            if _regime_bullish and pct_b > 0.8:
                bb_score = max(bb_score, -0.3)
    except Exception:
        pass

    # OBV: directional volume trend — normalized by avg volume * lookback window
    obv_lookback = int(cfg.get("obv_lookback", 10))
    obv_score: float | None = None
    obv_chg_norm: float | None = None
    try:
        obv_series = _obv(bars)
        if len(obv_series) >= obv_lookback + 1:
            avg_vol = _finite_float(bars["volume"].astype(float).rolling(20).mean().iloc[-1], 0.0)
            obv_chg = _finite_float(obv_series.iloc[-1] - obv_series.iloc[-obv_lookback], 0.0)
            norm_denom = avg_vol * obv_lookback
            if avg_vol is not None and obv_chg is not None and norm_denom > 0:
                obv_chg_norm = obv_chg / norm_denom
                # Positive = net buying (accumulation) = bullish; negative = distribution = bearish
                obv_score = float(np.tanh(obv_chg_norm * 3))
    except Exception:
        pass

    # VWAP: price vs today's session VWAP (intraday bars)
    vwap_score, vwap_detail = _compute_vwap_score(
        broker, symbol, cfg.get("vwap_timeframe", "5m")
    )

    # Fibonacci retracement: support/resistance with S/R flip detection
    fib_lookback  = int(cfg.get("fib_lookback", 60))
    fib_tolerance = float(cfg.get("fib_tolerance", 0.02))
    fib_score_val: float | None = None
    fib_detail: dict = {}
    try:
        fib_score_val, fib_detail = _fib_score(bars, fib_lookback, fib_tolerance)
    except Exception:
        pass

    sub_scores = _finite_scores([
        rsi_score, macd_score, trend_score,
        bb_score, obv_score, vwap_score, fib_score_val,
    ])

    if len(close) >= 11:
        prev_10 = _finite_float(close.iloc[-11])
        if prev_10 is not None and prev_10 > 0:
            roc_10 = (last - prev_10) / prev_10
            roc_score = float(np.tanh(roc_10 * 10))
        else:
            roc_score = 0.0
    else:
        roc_score = 0.0
    sub_scores.extend(_finite_scores([roc_score]))

    try:
        _sector = yf.Ticker(symbol).info.get("sector", "")
        _etf_sym = _SECTOR_ETF.get(_sector, "SPY")
        _etf_hist = yf.Ticker(_etf_sym).history(period="2mo", auto_adjust=True)
        if _etf_hist is not None and len(_etf_hist) >= 20 and len(close) >= 20:
            prev_stock = _finite_float(close.iloc[-20])
            _etf_close = _etf_hist["Close"]
            etf_last = _finite_float(_etf_close.iloc[-1])
            etf_prev = _finite_float(_etf_close.iloc[-20])
            if prev_stock and prev_stock > 0 and etf_last and etf_prev and etf_prev > 0:
                stock_ret = (last / prev_stock) - 1
                etf_ret = (etf_last / etf_prev) - 1
                rs_score = float(np.tanh((stock_ret - etf_ret) * 10))
            else:
                rs_score = 0.0
        else:
            rs_score = 0.0
    except Exception:
        rs_score = 0.0
    sub_scores.extend(_finite_scores([rs_score]))

    composite = float(np.mean(sub_scores)) if sub_scores else 0.0
    composite = max(-1.0, min(1.0, composite))

    # Normalize sub-scores from [-1, 1] to [0, 1] for storage/tracking.
    # The composite `score` and the sub_scores list stay in [-1, 1] for the engine.
    def _n(s):
        score = _finite_float(s)
        return round((float(np.clip(score, -1.0, 1.0)) + 1) / 2, 4) if score is not None else None

    def _fmt_n(s) -> str:
        norm = _n(s)
        return f"{norm:.2f}" if norm is not None else "n/a"

    adx_str  = f", ADX={adx_val:.1f} (factor={adx_factor:.2f}, trend={_fmt_n(trend_score)})"
    bb_str   = (f", BB%B={bb_pct_b:.2f} ({_fmt_n(bb_score)})" +
                (" [squeeze]" if bb_squeeze else "")) if bb_score is not None else ", BB n/a"
    obv_str  = f", OBV norm={obv_chg_norm:+.3f} ({_fmt_n(obv_score)})" if obv_score is not None else ", OBV n/a"
    vwap_str = (
        f", VWAP dist={vwap_detail.get('distance_pct', 0):+.2%} ({_fmt_n(vwap_score)})"
        if vwap_score is not None else ", VWAP n/a"
    )
    fib_str = (
        f", Fib {fib_detail.get('nearest_fib_ratio', 0)*100:.1f}%"
        f"@{fib_detail.get('nearest_fib_price', 0):.2f}"
        f" prox={fib_detail.get('fib_proximity_pct', 0):.1f}%"
        f" [{fib_detail.get('fib_level_status', '?')}->{fib_detail.get('fib_direction', '?')}]"
        f" ({_fmt_n(fib_score_val)})"
        if fib_score_val is not None else ", Fib n/a"
    )
    reason = (
        f"RSI={rsi:.1f} ({_fmt_n(rsi_score)}), "
        f"MACD hist={macd_cross:+.3f} ({_fmt_n(macd_score)})"
        f"{adx_str}{bb_str}{obv_str}{vwap_str}{fib_str}"
    )

    return {
        "symbol": symbol,
        "source": "technicals",
        "score": composite,
        "reason": reason,
        "details": {
            "rsi": _finite_float(rsi),
            "macd_hist": _finite_float(macd_cross),
            "sma_short": _finite_float(sma_s),
            "sma_long":  _finite_float(sma_l),
            "atr":       _finite_float(atr) if atr else None,
            "last":      _finite_float(last),
            "adx":       float(adx_val),
            "adx_factor": float(adx_factor),
            "rsi_score":   _n(rsi_score),
            "macd_score":  _n(macd_score),
            "trend_score": _n(trend_score),
            "bb_pct_b":    bb_pct_b,
            "bb_score":    _n(bb_score),
            "bb_squeeze":  bb_squeeze,
            "obv_score":   _n(obv_score),
            "obv_chg_norm": obv_chg_norm,
            "vwap":              vwap_detail.get("vwap"),
            "vwap_distance_pct": vwap_detail.get("distance_pct"),
            "vwap_score":        _n(vwap_score),
            "fib_score":         _n(fib_score_val),
            "fib_nearest_ratio": fib_detail.get("nearest_fib_ratio"),
            "fib_nearest_price": fib_detail.get("nearest_fib_price"),
            "fib_proximity_pct": fib_detail.get("fib_proximity_pct"),
            "fib_direction":     fib_detail.get("fib_direction"),
            "fib_level_status":  fib_detail.get("fib_level_status"),
            "fib_swing_high":    fib_detail.get("swing_high"),
            "fib_swing_low":     fib_detail.get("swing_low"),
            "fib_levels":        fib_detail.get("fib_levels"),
            "roc_score":         _n(roc_score),
            "rs_etf_score":      _n(rs_score),
        },
    }


def _empty(symbol: str, reason: str) -> dict[str, Any]:
    return {"symbol": symbol, "source": "technicals", "score": 0.0, "reason": reason, "details": {}}
