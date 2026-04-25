"""One-time script to pre-populate data/deep_score_cache.json.

Runs the deep scorer for every watchlist symbol at 31-day intervals going
back 365 days. Subsequent backtests will pull from this cache instead of
re-scoring live, cutting the deep-score phase from ~2 hours to near zero.

The script is fully resumable -- already-cached (symbol, date) pairs are
skipped unless --force is passed. Progress is flushed to disk every 10
tickers so a crash wastes at most a few minutes of work.

Usage:
    python scripts/backfill_deep_scores.py
    python scripts/backfill_deep_scores.py --days 365 --interval 31
    python scripts/backfill_deep_scores.py --force          # re-score everything
    python scripts/backfill_deep_scores.py AAPL MSFT NVDA  # specific tickers only
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Make sure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.config import load_config
from src.analysis.deep_scorer import score_ticker
from src.backtester.data_cache import DataCache
from src.backtester.deep_score_cache import DeepScoreCache


def _trading_day_on_or_before(target: date, spy_days: set[date]) -> date:
    """Walk backwards from target until we land on a known trading day."""
    d = target
    for _ in range(7):
        if d in spy_days:
            return d
        d -= timedelta(days=1)
    return target  # fallback if no trading day found nearby


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill deep score cache")
    parser.add_argument("tickers", nargs="*", help="Specific tickers (default: full watchlist)")
    parser.add_argument("--days",     type=int, default=365, help="History to cover in calendar days (default 365)")
    parser.add_argument("--interval", type=int, default=31,  help="Days between score dates (default 31)")
    parser.add_argument("--force",    action="store_true",   help="Re-score even if already cached")
    args = parser.parse_args()

    cfg     = load_config()
    symbols = (
        [s.upper() for s in args.tickers]
        if args.tickers
        else [str(s).upper() for s in cfg.get("screener", {}).get("watchlist", [])]
    )
    if not symbols:
        print("No symbols to score. Pass tickers or configure screener.watchlist.")
        sys.exit(1)

    ds_cache = DeepScoreCache()
    today    = date.today()

    # Build interval dates oldest-first so the cache fills chronologically
    interval_dates: list[date] = []
    d = today - timedelta(days=args.days)
    while d < today:
        interval_dates.append(d)
        d += timedelta(days=args.interval)
    # Always include a point near today (yesterday at most)
    yesterday = today - timedelta(days=1)
    if not interval_dates or interval_dates[-1] < yesterday - timedelta(days=args.interval // 2):
        interval_dates.append(yesterday)

    print(f"Symbols  : {len(symbols)}")
    print(f"Intervals: {len(interval_dates)}  ({interval_dates[0]} -> {interval_dates[-1]})")
    print(f"Total    : up to {len(symbols) * len(interval_dates)} score jobs")
    print()

    # Pre-fetch market data once -- deep_scorer needs daily bars for as_of slicing
    print("Loading market data cache (may fetch a few gaps)...")
    data_cache = DataCache(symbols)
    data_cache.fetch_all(daily_days=args.days + 60, intraday_days=1)

    # Build a set of known SPY trading days for snapping interval dates
    spy_bars   = data_cache.daily_bars("SPY", today)
    spy_days: set[date] = set()
    for ts in spy_bars.index:
        spy_days.add(ts.date() if hasattr(ts, "date") else __import__("pandas").Timestamp(ts).date())

    total   = len(symbols) * len(interval_dates)
    done    = 0
    skipped = 0
    failed  = 0
    t_start = time.time()

    for as_of in interval_dates:
        # Snap to the nearest real trading day so indicators have valid data
        snap = _trading_day_on_or_before(as_of, spy_days)
        spy_hist = data_cache.daily_bars("SPY", snap)
        spy_hist = spy_hist if not spy_hist.empty else None

        print(f"\n=== {as_of}  (snapped -> {snap}) ===")

        for sym in symbols:
            done += 1

            if not args.force and ds_cache.has_near(sym, snap, tolerance_days=args.interval // 2):
                skipped += 1
                continue

            t0 = time.time()
            try:
                result = score_ticker(sym, spy_hist=spy_hist, as_of_date=snap)
                ds_cache.put(sym, snap, result)
                elapsed = time.time() - t0
                grade   = result.get("grade", "?")
                score   = result.get("score", 0)
                print(f"  {sym:6s}: {score:5.1f} ({grade})  {elapsed:.1f}s  [{done}/{total}]")
            except Exception as e:
                failed += 1
                print(f"  {sym:6s}: FAILED -- {e}  [{done}/{total}]")

            # Flush progress every 10 tickers
            if done % 10 == 0:
                ds_cache.save()

    ds_cache.save()

    elapsed_total = time.time() - t_start
    print()
    print(f"Done in {elapsed_total / 60:.1f} min")
    print(f"  Scored : {done - skipped - failed}")
    print(f"  Skipped: {skipped} (already cached)")
    print(f"  Failed : {failed}")
    print(f"  Cache  : data/deep_score_cache.json")


if __name__ == "__main__":
    main()
