"""Position sizing + risk checks.

This module does three things:

1. ``compute_size`` — translates account equity + config % into a share count,
   with a trend-aware haircut: if the stock is in a downtrend, we halve the
   allocation from ``per_trade_pct`` to ``downtrend_size_pct`` (default 2.5%).

2. ``compute_dynamic_stop`` — looks at the last N candles on a chosen timeframe
   and returns the lowest low as the hard stop price (capped by
   ``stop_loss_max_pct`` so we never wear a stupidly wide stop on a fast ticker).

3. ``should_flatten_for_risk`` — honored at the start of every decision cycle:
   if price has pierced the per-position stop (or take-profit), return True so
   the caller closes the position.
"""
from __future__ import annotations

import math
from typing import Any

from ..broker.base import Account, Broker, Position
from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)


# ------------------------------------------------------------------ sizing

def compute_size(
    account: Account,
    price: float,
    trend: dict | None = None,
    stop_price: float | None = None,
    regime: dict | None = None,
) -> tuple[int, dict]:
    """Return (share count, details) for a new entry.

    Sizing rules, applied as a chain of constraints:

    1. **% cap (equity):** ``per_trade_pct`` of equity (or
       ``downtrend_size_pct`` if the stock is in a 30d downtrend), capped by
       ``max_trade_usd``. Then scaled by the regime multiplier — bearish /
       volatile regimes shrink this further. This is the maximum *notional*
       we'll deploy.
    2. **Risk cap ($):** if ``risk_per_trade_pct`` is set AND ``stop_price`` is
       provided, limit shares so the $ loss between entry and stop is no more
       than ``equity * risk_per_trade_pct``. On trades with a wider stop this
       will be the binding constraint and will size the trade *down*.
    3. **Buying power:** can't buy what you can't pay for.

    Final qty = min of the three. The details dict reports which rule was
    binding so the dashboard/journal can show *why* the size came out the way
    it did.
    """
    t = load_config()["trading"]

    # --- Rule 1: % of equity cap ---
    pct = float(t.get("per_trade_pct", 0.05))
    sizing_mode = "normal"
    if trend and _is_downtrend(trend):
        pct = float(t.get("downtrend_size_pct", pct / 2))
        sizing_mode = "downtrend_reduced"

    # Regime-aware multiplier — stacks on top of any downtrend haircut.
    regime_mult = 1.0
    regime_label = ""
    if regime:
        regime_label = str(regime.get("label") or "").lower()
        mult_cfg = t.get("regime_size_multiplier", {}) or {}
        regime_mult = float(mult_cfg.get(regime_label, 1.0))
    if regime_mult != 1.0:
        sizing_mode = (f"{sizing_mode}+regime_{regime_label}"
                       if sizing_mode != "normal"
                       else f"regime_{regime_label}")
    pct *= regime_mult

    target_usd = min(account.equity * pct, t.get("max_trade_usd", 1e9))
    details: dict = {
        "pct_used": pct,
        "regime_mult": regime_mult,
        "regime": regime_label or None,
        "sizing_mode": sizing_mode,
        "target_usd": target_usd,
        "binding_constraint": "pct_cap",
    }

    if target_usd < t.get("min_trade_usd", 0):
        details["reason"] = f"target ${target_usd:.0f} below min_trade_usd"
        return 0, details
    if price <= 0:
        details["reason"] = "non-positive price"
        return 0, details

    shares_by_pct = int(math.floor(target_usd / price))

    # --- Rule 2: risk-based cap (only if we have a stop + config is on) ---
    risk_pct_cfg = t.get("risk_per_trade_pct", None)
    shares_by_risk: int | None = None
    if risk_pct_cfg is not None and stop_price is not None and stop_price > 0 \
            and stop_price < price:
        risk_pct = float(risk_pct_cfg)
        dollar_risk_budget = account.equity * risk_pct
        stop_distance = price - stop_price
        if stop_distance > 0:
            shares_by_risk = int(math.floor(dollar_risk_budget / stop_distance))
            details["risk_budget_usd"] = dollar_risk_budget
            details["stop_distance"] = stop_distance
            details["shares_by_risk"] = shares_by_risk

    # --- Rule 3: buying power cap ---
    max_by_bp = int(math.floor(account.buying_power / price)) if price else 0

    # Take the tightest constraint
    qty = max(0, min(shares_by_pct, max_by_bp))
    if shares_by_risk is not None and shares_by_risk < qty:
        qty = max(0, shares_by_risk)
        details["binding_constraint"] = "risk_budget"
        details["sizing_mode"] = f"{sizing_mode}+risk_limited"
    elif max_by_bp < shares_by_pct:
        details["binding_constraint"] = "buying_power"

    details["shares_by_pct"] = shares_by_pct
    details["qty"] = qty
    return qty, details


def _is_downtrend(trend: dict) -> bool:
    short = (trend.get("short") or {}).get("label", "")
    blended = trend.get("label", "")
    return short == "downtrend" or blended in ("downtrend", "bounce_in_downtrend")


# ------------------------------------------------------------------ dynamic stop

def compute_dynamic_stop(broker: Broker, symbol: str, entry_price: float) -> dict[str, Any]:
    """Return a dict with the stop-loss price and metadata.

    Rules:
      - Pull the last N candles on ``stop_loss_timeframe`` (excluding the
        current forming candle, so we use the N candles *prior* to entry).
      - Stop = lowest low across those candles. Width in % is whatever that
        low-to-entry distance works out to — it will vary trade to trade.
      - If ``stop_loss_max_pct`` is set (non-null) AND the raw stop would be
        wider than that, the stop is clamped. By default this is null, so the
        stop is always exactly the prior-candle low.
      - If the bar data is unavailable OR mode=='fixed', fall back to
        ``entry_price * (1 - stop_loss_pct)``.
    """
    t = load_config()["trading"]
    mode = t.get("stop_loss_mode", "dynamic")
    fallback_pct = float(t.get("stop_loss_pct", 0.02))
    max_pct_raw = t.get("stop_loss_max_pct", None)
    max_pct = float(max_pct_raw) if max_pct_raw is not None else None

    fallback_stop = entry_price * (1 - fallback_pct)

    if mode == "fixed":
        return {"stop": fallback_stop, "source": "fixed_pct",
                "pct": fallback_pct, "reason": f"fixed {fallback_pct:.1%} stop"}

    lookback = int(t.get("stop_loss_lookback_candles", 2))
    tf = str(t.get("stop_loss_timeframe", "15m"))

    try:
        # Fetch lookback+2 to guarantee we can drop the current forming bar
        # and still have `lookback` completed candles below entry.
        bars = broker.get_bars(symbol, timeframe=tf, limit=lookback + 2)
    except Exception as e:
        log.debug(f"{symbol}: dynamic stop bars fetch failed ({e}); using fixed")
        return {"stop": fallback_stop, "source": "fixed_fallback",
                "pct": fallback_pct, "reason": f"bars unavailable: {e}"}

    if bars is None or bars.empty or len(bars) < 2:
        return {"stop": fallback_stop, "source": "fixed_fallback",
                "pct": fallback_pct, "reason": "insufficient bars"}

    # Drop the last bar (likely the forming / current one) then take the
    # last ``lookback`` candles.
    prior = bars.iloc[:-1].tail(lookback)
    if prior.empty:
        return {"stop": fallback_stop, "source": "fixed_fallback",
                "pct": fallback_pct, "reason": "no prior bars"}

    raw_stop = float(prior["low"].min())
    # Sanity: raw_stop must be below entry. If not, fall back to fixed.
    if raw_stop >= entry_price:
        return {"stop": fallback_stop, "source": "fixed_fallback",
                "pct": fallback_pct,
                "reason": f"prior-candle low {raw_stop:.2f} >= entry {entry_price:.2f}"}

    raw_pct = (entry_price - raw_stop) / entry_price if entry_price else 0.0

    # Optional safety clamp: only applies if the user set stop_loss_max_pct
    if max_pct is not None and raw_pct > max_pct:
        clamped = entry_price * (1 - max_pct)
        reason = (f"prior-candle low {raw_stop:.2f} ({raw_pct:.2%}) exceeded "
                  f"max cap {max_pct:.1%}; clamped to {clamped:.2f}")
        return {"stop": clamped, "source": "dynamic_clamped",
                "pct": max_pct, "reason": reason,
                "raw_stop": raw_stop, "raw_pct": raw_pct,
                "lookback": lookback, "tf": tf}

    stop_price = raw_stop
    try:
        _bars_15m = broker.get_bars(symbol, timeframe="15m", limit=20)
        if _bars_15m is not None and len(_bars_15m) >= 14:
            import pandas_ta as _pta
            _atr_s = _pta.atr(_bars_15m["high"], _bars_15m["low"], _bars_15m["close"], length=14)
            if _atr_s is not None and len(_atr_s) > 0 and not _atr_s.isna().iloc[-1]:
                _atr15 = float(_atr_s.iloc[-1])
                _min_dist = max(1.5 * _atr15, entry_price * 0.015)
                _min_stop = entry_price - _min_dist
                if stop_price > _min_stop:
                    stop_price = _min_stop
    except Exception:
        pass
    raw_pct = (entry_price - stop_price) / entry_price if entry_price else 0.0
    reason = (f"dynamic stop = min(low) of last {lookback} {tf} candles "
              f"= {stop_price:.2f} ({raw_pct:.2%} below entry)")
    return {"stop": stop_price, "source": "dynamic",
            "pct": raw_pct, "reason": reason,
            "lookback": lookback, "tf": tf}


def compute_take_profit(entry_price: float) -> float:
    t = load_config()["trading"]
    return entry_price * (1 + float(t.get("take_profit_pct", 0.05)))


# ------------------------------------------------------------------ trailing stop

def compute_trailing_stop(
    current_stop: float,
    current_price: float,
    trail_pct: float,
) -> dict[str, Any]:
    """Percentage trailing stop.

    At entry, we record the INITIAL stop width as a percentage of the entry
    price (e.g. entry=$10, stop=$9.20 -> trail_pct = 8%). From then on, the
    stop trails the current price by that same percentage — so if price runs
    from $10 -> $12, the stop lifts from $9.20 to $12 * 0.92 = $11.04.

    The stop ONLY ever rises. On pullbacks, the prior (higher) stop stays in
    place — that's what locks in gains.

    Returns a dict with:
      - new_stop: the stop to use going forward (may equal current_stop)
      - raised: bool — True if we actually moved the stop up
      - candidate: the price-based trail target we compared against
      - reason: human-readable explanation
    """
    if current_price <= 0 or trail_pct <= 0:
        return {"new_stop": current_stop, "raised": False,
                "candidate": current_stop,
                "reason": "invalid inputs (price or trail_pct <= 0)"}

    candidate = current_price * (1.0 - trail_pct)
    if candidate > current_stop:
        return {"new_stop": candidate, "raised": True,
                "candidate": candidate,
                "reason": (f"trail raised to {candidate:.2f} "
                           f"(price {current_price:.2f} - {trail_pct:.2%})")}
    return {"new_stop": current_stop, "raised": False,
            "candidate": candidate,
            "reason": (f"trail unchanged (candidate {candidate:.2f} <= "
                       f"current stop {current_stop:.2f})")}


# ------------------------------------------------------------------ risk check

def should_flatten_for_risk(position: Position, last_price: float) -> tuple[bool, str]:
    """Stop-loss / take-profit check against a live price.

    Prefers the per-position ``stop_loss`` / ``take_profit`` stored on the
    Position (set at entry time); falls back to the config % if the position
    pre-dates the dynamic-stop feature.
    """
    t = load_config()["trading"]
    if position.quantity == 0 or position.avg_entry == 0:
        return False, ""

    # Per-position stop (dynamic) takes precedence
    stop = getattr(position, "stop_loss", None)
    tp = getattr(position, "take_profit", None)

    if position.quantity > 0:
        if stop and last_price <= stop:
            return True, f"stop-loss hit @ {last_price:.2f} (stop={stop:.2f})"
        if tp and last_price >= tp:
            return True, f"take-profit hit @ {last_price:.2f} (tp={tp:.2f})"

        # Fallback % rules for positions without a stored stop
        if not stop:
            pnl_pct = (last_price - position.avg_entry) / position.avg_entry
            if pnl_pct <= -float(t.get("stop_loss_pct", 0.02)):
                return True, f"stop-loss hit ({pnl_pct:+.2%}, fixed)"
            if pnl_pct >= float(t.get("take_profit_pct", 0.05)):
                return True, f"take-profit hit ({pnl_pct:+.2%}, fixed)"
    else:
        # Short position — symmetric
        if stop and last_price >= stop:
            return True, f"stop-loss hit (short) @ {last_price:.2f} (stop={stop:.2f})"
        pnl_pct = -(last_price - position.avg_entry) / position.avg_entry
        if pnl_pct <= -float(t.get("stop_loss_pct", 0.02)):
            return True, f"stop-loss hit (short, fixed, {pnl_pct:+.2%})"

    return False, ""
