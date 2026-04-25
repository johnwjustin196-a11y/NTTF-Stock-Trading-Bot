"""
Standalone indicator backtester.

Runs one indicator in isolation against historical 15-min candles to measure
how well its signals actually predict profitable moves.

Signal logic mirrors the live entry queue exactly:
  - Support bounce : any of last-10 candle lows <= trigger * 1.01
                     AND last candle close > trigger AND > prev candle close
  - Resistance breakout : last candle open AND close both above trigger

Exit logic mirrors the live bot exactly:
  - Phase 1 (before TP): exit if candle low <= stop
  - Phase 2 (after TP hit): stop moves to TP price, then ratchets 50% toward
    current price every ~4 candles (1 hour), until stopped out

Usage:
    python scripts/test_indicator.py --indicator fib --days 180
    python scripts/test_indicator.py --indicator fib --days 252 --tickers AMD NVDA AAPL
    python scripts/test_indicator.py --indicator fib --days 90 --tp 0.05 --sl 0.03
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Project root on path so src.* imports work from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.backtester.data_cache import DataCache
from src.utils.config import load_config
from src.analysis.technicals import _rsi, _adx

FIB_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.786)


# ══════════════════════════════════════════════════════ data helpers

def _build_cache(tickers: list[str], days: int) -> DataCache:
    # +90 daily days gives enough warm-up for the 60-bar fib lookback
    cache = DataCache(symbols=tickers)
    print(f"Loading bar cache for {len(tickers)} tickers "
          f"(daily +90d warm-up, intraday +10d buffer)...")
    cache.fetch_all(daily_days=days + 90, intraday_days=days + 10)
    return cache


def _trading_day_list(cache: DataCache, days: int) -> list[date]:
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6))   # buffer for weekends/holidays
    all_days = cache.trading_days(start, end)
    return list(all_days[-days:]) if len(all_days) > days else list(all_days)


# ══════════════════════════════════════════════════════ fib level computation

def _fib_levels(daily_bars: pd.DataFrame, lookback: int) -> dict | None:
    """Compute swing-based fib levels from daily bars. Returns None if insufficient data."""
    n = min(lookback, len(daily_bars))
    if n < 20:
        return None

    window    = daily_bars.iloc[-n:]
    high_arr  = window["high"].astype(float).values
    low_arr   = window["low"].astype(float).values
    swing_high = float(high_arr.max())
    swing_low  = float(low_arr.min())
    if swing_high <= swing_low:
        return None

    move   = swing_high - swing_low
    levels = {r: swing_low + r * move for r in FIB_RATIOS}

    idx_high   = int(high_arr.argmax())
    idx_low    = int(low_arr.argmin())
    uptrend    = idx_low < idx_high          # low came before high = uptrend pullback

    return {"swing_high": swing_high, "swing_low": swing_low,
            "levels": levels, "uptrend": uptrend}


def _nearest_level(price: float, fib_info: dict, near_pct: float) -> dict | None:
    """Find the fib level nearest to price. Returns None if outside near_pct."""
    levels = fib_info["levels"]
    nearest_ratio = min(levels, key=lambda r: abs(price - levels[r]))
    nearest_price = levels[nearest_ratio]
    proximity     = abs(price - nearest_price) / nearest_price if nearest_price else 1.0

    if proximity > near_pct:
        return None

    direction = "support" if price >= nearest_price else "resistance"
    return {
        "ratio":         nearest_ratio,
        "price":         nearest_price,
        "proximity_pct": proximity,
        "direction":     direction,
    }


# ══════════════════════════════════════════════════════ signal detection
# Mirrors entry_queue._check_bounce() and _check_breakout() exactly.

def _check_bounce(window: pd.DataFrame, trigger: float, touch_pct: float) -> bool:
    """last-10-candle window. Any low within touch_pct of trigger, then recovery."""
    if len(window) < 2:
        return False
    touch_band = trigger * (1.0 + touch_pct)
    tested     = bool((window["low"].astype(float) <= touch_band).any())
    last, prev = window.iloc[-1], window.iloc[-2]
    recovered  = (float(last["close"]) > trigger
                  and float(last["close"]) > float(prev["close"]))
    return tested and recovered


def _check_breakout(window: pd.DataFrame, trigger: float) -> bool:
    """Full candle body (open AND close) above trigger."""
    if window.empty:
        return False
    last = window.iloc[-1]
    return float(last["close"]) > trigger and float(last["open"]) > trigger


# ══════════════════════════════════════════════════════ stop / exit

def _dynamic_stop(pre_entry_bars: pd.DataFrame,
                  entry_price: float, cfg: dict) -> float:
    """Initial stop from recent swing lows, clamped to config bounds."""
    t        = cfg["trading"]
    lookback = int(t.get("stop_loss_lookback_candles", 5))
    min_pct  = float(t.get("stop_loss_pct", 0.02))
    max_pct  = float(t.get("stop_loss_max_pct", 0.05))

    recent = pre_entry_bars.tail(lookback)
    if not recent.empty:
        swing_low = float(recent["low"].astype(float).min())
        stop = max(swing_low,          entry_price * (1.0 - max_pct))
        stop = min(stop,               entry_price * (1.0 - min_pct))
    else:
        stop = entry_price * (1.0 - min_pct)
    return stop


def _track_exit(
    cache: DataCache,
    ticker: str,
    entry_date: date,
    entry_candle_pos: int,
    entry_price: float,
    initial_stop: float,
    tp_pct: float,
    max_hold_days: int,
    all_trading_days: list[date],
    cfg: dict | None = None,
) -> dict:
    """
    Walk 15-min bars forward from entry, applying the live bot's ratcheting exit.

    Phase 1 — before TP hit:
        Exit immediately if candle low <= current_stop (stop-loss).

    Phase 2 — after TP hit (candle high >= entry * (1 + tp_pct)):
        Stop jumps to tp_trigger price (profit locked).
        Every ~4 candles (≈1 hour, matching the live decision cycle cadence)
        the stop ratchets: new_stop = stop + 0.5 * (close - stop).
        Stop never moves down.
        Exit when candle low <= current_stop.

    Falls back to closing at last available price if max_hold_days is reached.
    """
    tp_trigger    = entry_price * (1.0 + tp_pct)
    current_stop  = initial_stop
    locked_profit = False
    candle_count  = 0
    last_close    = entry_price
    last_date     = entry_date

    try:
        entry_day_idx = all_trading_days.index(entry_date)
    except ValueError:
        return {"exit_price": entry_price, "exit_reason": "no_data",
                "exit_date": str(entry_date), "days_held": 0, "pnl_pct": 0.0}

    days_held = 0

    for day_offset in range(max_hold_days + 1):
        day_idx  = entry_day_idx + day_offset
        if day_idx >= len(all_trading_days):
            break
        sim_date = all_trading_days[day_idx]

        try:
            intraday = cache.intraday_bars(ticker, sim_date)
            if intraday is not None and not intraday.empty:
                intraday.columns = intraday.columns.str.lower()
        except Exception:
            intraday = None
        if intraday is None or intraday.empty:
            continue

        # On entry day skip the entry candle itself and all prior candles
        start = (entry_candle_pos + 1) if day_offset == 0 else 0
        bars  = intraday.iloc[start:]
        if bars.empty:
            continue

        days_held = day_offset

        for _, candle in bars.iterrows():
            candle_count += 1
            high  = float(candle["high"])
            low   = float(candle["low"])
            close = float(candle["close"])
            last_close = close
            last_date  = sim_date

            # ── stop check (conservative: use candle low)
            if low <= current_stop:
                pnl = (current_stop - entry_price) / entry_price
                return {
                    "exit_price":  round(current_stop, 4),
                    "exit_reason": "locked_profit_stop" if locked_profit else "stop_loss",
                    "exit_date":   str(sim_date),
                    "days_held":   days_held,
                    "pnl_pct":     round(pnl, 5),
                }

            # ── TP hit → lock profit, move stop to TP price
            if not locked_profit and high >= tp_trigger:
                locked_profit = True
                current_stop  = tp_trigger

            # ── ratchet every ~4 candles (≈1 hour) after locking,
            # but only if price has moved at least 2.5% above the current stop.
            # Prevents micro-ratchets on sideways chop after TP is hit.
            if locked_profit and candle_count % 4 == 0:
                _t = (cfg or {}).get("trading", {})
                ratchet_min = float(_t.get("ratchet_min_move_pct", 0.025))
                if close >= current_stop * (1.0 + ratchet_min):
                    ratchet_step = float(_t.get("ratchet_step_pct", 0.30))
                    new_stop = current_stop + ratchet_step * (close - current_stop)
                    if new_stop > current_stop:
                        current_stop = new_stop

    # max_hold_days exhausted — close at last available price
    pnl = (last_close - entry_price) / entry_price
    return {
        "exit_price":  round(last_close, 4),
        "exit_reason": "max_hold_reached",
        "exit_date":   str(last_date),
        "days_held":   days_held,
        "pnl_pct":     round(pnl, 5),
    }


# ══════════════════════════════════════════════════════ context helpers

def _signal_context(daily_bars: pd.DataFrame) -> dict:
    """RSI and ADX at signal time — logged for later slicing."""
    close = daily_bars["close"].astype(float)
    try:
        rsi_val = float(_rsi(close, 14).iloc[-1]) if len(close) >= 15 else 0.0
    except Exception:
        rsi_val = 0.0
    try:
        adx_series = _adx(daily_bars, 14)
        adx_val = float(adx_series.iloc[-1]) if np.isfinite(adx_series.iloc[-1]) else 0.0
    except Exception:
        adx_val = 0.0
    return {"rsi": round(rsi_val, 1), "adx": round(adx_val, 1)}


def _regime(cache: DataCache, sim_date: date) -> str:
    try:
        from src.backtester.signals import backtest_regime
        return backtest_regime(cache, sim_date).get("label", "unknown")
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════ fib test runner

def run_fib_test(
    cache: DataCache,
    tickers: list[str],
    trading_days: list[date],
    cfg: dict,
    tp_pct: float,
    max_hold_days: int,
) -> list[dict]:
    """
    For each ticker × trading day:
      1. Compute fib levels from daily bars.
      2. Check if price is near a support or resistance level.
      3. Walk the day's 15-min candles; fire signal when bounce/breakout rules hit.
      4. Track exit with ratcheting stop.
      5. Append one record per fired signal.
    """
    fib_cfg  = cfg["signals"]["technicals"]
    eq_cfg   = cfg.get("entry_queue", {}) or {}

    lookback = int(fib_cfg.get("fib_lookback", 60))
    near_pct = float(eq_cfg.get("near_level_pct", 0.05))
    touch_pct = float(eq_cfg.get("bounce_touch_pct", 0.01))

    signals: list[dict] = []

    for ticker in tickers:
        print(f"  {ticker}...", end=" ", flush=True)
        ticker_fires = 0

        for sim_date in trading_days:
            # ── daily bars for fib level computation (no lookahead)
            try:
                daily = cache.daily_bars(ticker, sim_date)
                if daily is not None and not daily.empty:
                    daily.columns = daily.columns.str.lower()
            except Exception:
                daily = None
            if daily is None or len(daily) < lookback + 5:
                continue

            fib_info = _fib_levels(daily, lookback)
            if not fib_info:
                continue

            last_price = float(daily["close"].iloc[-1])
            level = _nearest_level(last_price, fib_info, near_pct)
            if level is None:
                continue   # price not near any fib level — null signal, skip

            # ── today's 15-min bars for intraday signal detection
            try:
                intraday = cache.intraday_bars(ticker, sim_date)
                if intraday is not None and not intraday.empty:
                    intraday.columns = intraday.columns.str.lower()
            except Exception:
                intraday = None
            if intraday is None or intraday.empty:
                continue

            trigger   = level["price"]
            direction = level["direction"]
            signal_candle_idx = None
            signal_type       = None

            # Walk candles in order, simulating the 5-min monitoring loop.
            # window = last 10 candles available at each checkpoint (mirrors live code).
            for candle_idx in range(1, len(intraday)):
                window = intraday.iloc[max(0, candle_idx - 9): candle_idx + 1]

                if direction == "support":
                    if _check_bounce(window, trigger, touch_pct):
                        signal_candle_idx = candle_idx
                        signal_type = "support_bounce"
                        break
                else:   # resistance
                    if _check_breakout(window.tail(1), trigger):
                        signal_candle_idx = candle_idx
                        signal_type = "resistance_breakout"
                        break

            if signal_candle_idx is None:
                continue   # no signal fired today for this ticker

            entry_candle = intraday.iloc[signal_candle_idx]
            entry_price  = float(entry_candle["close"])

            # Entry time — handle both DatetimeIndex and plain index
            try:
                entry_time = str(entry_candle.name.time())
            except Exception:
                entry_time = "?"

            # Dynamic stop from candles before the signal
            pre_entry = intraday.iloc[:signal_candle_idx]
            stop      = _dynamic_stop(pre_entry, entry_price, cfg)

            # Track exit
            exit_info = _track_exit(
                cache, ticker, sim_date, signal_candle_idx,
                entry_price, stop, tp_pct, max_hold_days, trading_days,
                cfg=cfg,
            )

            ctx    = _signal_context(daily)
            regime = _regime(cache, sim_date)

            record: dict[str, Any] = {
                "date":            str(sim_date),
                "entry_time":      entry_time,
                "ticker":          ticker,
                "signal_type":     signal_type,
                "fib_ratio":       level["ratio"],
                "fib_price":       round(trigger, 4),
                "swing_high":      round(fib_info["swing_high"], 4),
                "swing_low":       round(fib_info["swing_low"], 4),
                "proximity_pct":   round(level["proximity_pct"], 4),
                "entry_price":     round(entry_price, 4),
                "initial_stop":    round(stop, 4),
                "tp_trigger":      round(entry_price * (1.0 + tp_pct), 4),
                **exit_info,
                "adx_at_signal":   ctx["adx"],
                "rsi_at_signal":   ctx["rsi"],
                "regime_at_signal": regime,
            }
            signals.append(record)
            ticker_fires += 1

        print(f"{ticker_fires} fires")

    return signals


# ══════════════════════════════════════════════════════ summary

def _print_summary(signals: list[dict], days: int, tickers: list[str],
                   tp_pct: float) -> None:
    if not signals:
        print("\nNo signals fired.")
        return

    total    = len(signals)
    by_type: dict[str, list[float]] = {}
    by_exit: dict[str, int]         = {}

    for s in signals:
        by_type.setdefault(s["signal_type"], []).append(s["pnl_pct"])
        by_exit[s["exit_reason"]] = by_exit.get(s["exit_reason"], 0) + 1

    print(f"\n{'─'*62}")
    print(f"FIB INDICATOR TEST  |  {days} trading days  |  {len(tickers)} tickers")
    print(f"TP trigger: {tp_pct:.1%}  (ratcheting stop — no fixed exit)")
    print(f"{'─'*62}")
    print(f"Total fires: {total}\n")

    for stype, pnls in sorted(by_type.items()):
        wins    = sum(1 for p in pnls if p > 0)
        avg_pnl = sum(pnls) / len(pnls)
        best    = max(pnls)
        worst   = min(pnls)
        print(f"  {stype:<25}  {len(pnls):>4} fires"
              f"  |  avg {avg_pnl:+.2%}"
              f"  |  win {wins/len(pnls):.0%}"
              f"  |  best {best:+.2%}  worst {worst:+.2%}")

    print(f"\nExit reasons:")
    for reason, count in sorted(by_exit.items(), key=lambda x: -x[1]):
        print(f"  {reason:<25}  {count:>4}  ({count/total:.0%})")
    print(f"{'─'*62}\n")


# ══════════════════════════════════════════════════════ CLI

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone indicator backtester",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--indicator", default="fib", choices=["fib"],
                        help="Indicator to test (default: fib)")
    parser.add_argument("--days", type=int, default=180,
                        help="Trading days to backtest (default: 180)")
    parser.add_argument("--tickers", nargs="*",
                        help="Specific tickers — default: full watchlist from settings.yaml")
    parser.add_argument("--tp", type=float, default=None,
                        help="Take-profit %% trigger, e.g. 0.05 (default: from settings.yaml)")
    parser.add_argument("--sl", type=float, default=None,
                        help="Min stop-loss %% distance, e.g. 0.02 (default: from settings.yaml)")
    parser.add_argument("--sl-max", type=float, default=None,
                        help="Max stop-loss %% distance, e.g. 0.05 (default: from settings.yaml)")
    parser.add_argument("--max-hold", type=int, default=5,
                        help="Max holding period in trading days (default: 5)")
    parser.add_argument("--out-dir", default="data/indicator_tests",
                        help="Output directory (default: data/indicator_tests)")
    args = parser.parse_args()

    cfg = load_config()

    # ── tickers
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        tickers = [t for t in cfg["screener"]["watchlist"]
                   if not str(t).startswith("^")]

    # ── TP / SL overrides
    tp_pct = args.tp if args.tp is not None else float(cfg["trading"]["take_profit_pct"])
    if args.sl is not None:
        cfg["trading"]["stop_loss_pct"] = args.sl
    if args.sl_max is not None:
        cfg["trading"]["stop_loss_max_pct"] = args.sl_max

    sl_min = float(cfg["trading"]["stop_loss_pct"])
    sl_max = float(cfg["trading"]["stop_loss_max_pct"])

    print(f"\n{'═'*62}")
    print(f"  INDICATOR TEST — {args.indicator.upper()}")
    print(f"{'═'*62}")
    print(f"  Tickers  : {len(tickers)}  "
          f"({', '.join(tickers[:6])}{'...' if len(tickers) > 6 else ''})")
    print(f"  Days     : {args.days}")
    print(f"  TP       : {tp_pct:.1%}  (ratcheting stop after hit)")
    print(f"  SL range : {sl_min:.1%} – {sl_max:.1%}  (dynamic from swing lows)")
    print(f"  Max hold : {args.max_hold} trading days")
    print(f"{'═'*62}\n")

    # ── data
    cache        = _build_cache(tickers, args.days)
    trading_days = _trading_day_list(cache, args.days)
    print(f"Date range : {trading_days[0]} → {trading_days[-1]} "
          f"({len(trading_days)} trading days)\n")

    # ── run
    print("Scanning for signals...\n")
    if args.indicator == "fib":
        signals = run_fib_test(cache, tickers, trading_days, cfg, tp_pct, args.max_hold)

    # ── output
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_jsonl   = out_dir / f"{args.indicator}_{ts}.jsonl"
    out_summary = out_dir / f"{args.indicator}_{ts}_summary.txt"

    with open(out_jsonl, "w", encoding="utf-8") as fh:
        for record in signals:
            fh.write(json.dumps(record) + "\n")

    # Print summary to console AND save it
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_summary(signals, args.days, tickers, tp_pct)
    summary_text = buf.getvalue()
    print(summary_text)
    out_summary.write_text(summary_text, encoding="utf-8")

    print(f"Results written to:")
    print(f"  {out_jsonl}")
    print(f"  {out_summary}\n")


if __name__ == "__main__":
    main()
