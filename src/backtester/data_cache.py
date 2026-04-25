"""Pre-fetch and cache all market history needed for a backtest run.

Primary source: Alpaca Markets API (5yr daily, 2yr 15min).
Fallback: yfinance (2yr daily, 60d hourly) for any symbol Alpaca doesn't return.

Market data is persisted to disk (data/market_cache/) so subsequent runs only
fetch new bars since the last run. Gap detection handles three cases per symbol:
  - New symbol (not in cache): fetch full needed range
  - Before-gap (backtest extends earlier than cached start): fetch the earlier slice
  - After-gap (cache is stale): fetch from last cached date to today

Cull thresholds keep cache size bounded:
  - Daily:    550 calendar days (~18 months) — enough for a 250-day backtest
              plus 60+ day indicator warm-up (SMA50, MACD, Fib, etc.) plus buffer
  - Intraday: 400 calendar days (~13 months) — covers the full 250-trading-day window

All access methods accept an `as_of_date` argument and never return any bar
whose timestamp is after that date — no lookahead bias.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from ..utils.logger import get_logger

log = get_logger(__name__)

_BENCH = ["SPY", "QQQ", "IWM"]

_DAILY_YEARS  = 5
_HOURLY_YEARS = 2

_PROJECT_ROOT      = Path(__file__).resolve().parents[2]
_CACHE_DIR_DEFAULT = _PROJECT_ROOT / "data" / "market_cache"

# How many calendar days to retain in the disk cache after each run
_DAILY_CULL_DAYS    = 550   # 250 trading days ≈ 362 cal + 60-day indicator warmup + buffer
_INTRADAY_CULL_DAYS = 400   # 250 trading days ≈ 362 cal days + small buffer


class DataCache:
    """Pre-fetches daily + 15-min bars for all symbols at startup.

    On first run: downloads from Alpaca (or yfinance fallback).
    On subsequent runs: loads from disk and only fetches bars that are newer
    than the cache or cover a date range the cache doesn't have.

    Intraday resolution: 15-minute bars (stored in self._hourly for API compat).
    """

    def __init__(
        self,
        symbols: list[str],
        cache_dir: str | Path | None = None,
    ) -> None:
        self.symbols   = sorted(set(s.upper() for s in symbols) | set(_BENCH))
        self._daily:  dict[str, pd.DataFrame] = {}
        self._hourly: dict[str, pd.DataFrame] = {}
        self._fetched = False
        self._cache_dir = Path(cache_dir) if cache_dir else _CACHE_DIR_DEFAULT

    # ------------------------------------------------------------------ fetch

    def fetch_all(
        self,
        daily_days: int | None = None,
        intraday_days: int | None = None,
    ) -> None:
        """Load from disk cache then fetch only the missing date ranges.

        daily_days:    calendar days of daily bars needed
        intraday_days: calendar days of 15-min bars needed
        """
        today         = date.today()
        _daily_cal    = daily_days    if daily_days    is not None else _DAILY_YEARS  * 365
        _intraday_cal = intraday_days if intraday_days is not None else _HOURLY_YEARS * 365

        needed_daily_start    = today - timedelta(days=_daily_cal)
        needed_intraday_start = today - timedelta(days=_intraday_cal)

        has_alpaca = bool(os.getenv("ALPACA_API_KEY")) and bool(os.getenv("ALPACA_SECRET_KEY"))

        # --- Load disk cache ---
        meta, disk_daily, disk_intraday = self._load_disk_cache()
        sym_set      = set(self.symbols)
        self._daily  = {s: df for s, df in disk_daily.items()   if s in sym_set}
        self._hourly = {s: df for s, df in disk_intraday.items() if s in sym_set}

        n_d, n_i = len(self._daily), len(self._hourly)
        if n_d or n_i:
            log.info(
                f"[data_cache] disk cache: {n_d} daily, {n_i} intraday symbol files loaded"
            )

        # --- Compute fetch gaps ---
        daily_gaps    = self._compute_gaps(self.symbols, "daily",    needed_daily_start,    today, meta)
        intraday_gaps = self._compute_gaps(self.symbols, "intraday", needed_intraday_start, today, meta)

        total_d = sum(len(v) for v in daily_gaps.values())
        total_i = sum(len(v) for v in intraday_gaps.values())

        if not daily_gaps and not intraday_gaps:
            log.info("[data_cache] disk cache fully covers needed range — no fetch required")
        else:
            log.info(
                f"[data_cache] fetching gaps: {total_d} daily-symbol ranges, "
                f"{total_i} intraday-symbol ranges"
            )

        # --- Fetch from Alpaca ---
        if has_alpaca:
            for (fetch_start, fetch_end), syms in daily_gaps.items():
                log.info(
                    f"[data_cache] Alpaca 1Day  {fetch_start} -> {fetch_end}: {len(syms)} symbols"
                )
                fetched = self._fetch_alpaca_bars(syms, "1Day", fetch_start, end=fetch_end)
                _merge(self._daily, fetched)

            for (fetch_start, fetch_end), syms in intraday_gaps.items():
                log.info(
                    f"[data_cache] Alpaca 15Min {fetch_start} -> {fetch_end}: {len(syms)} symbols"
                )
                fetched = self._fetch_alpaca_bars(syms, "15Min", fetch_start, end=fetch_end)
                _merge(self._hourly, fetched)

            # yfinance fallback for still-missing symbols
            missing_d = [s for s in self.symbols if s not in self._daily]
            missing_i = [s for s in self.symbols if s not in self._hourly]
            if missing_d or missing_i:
                log.info(
                    f"[data_cache] yfinance fallback — "
                    f"{len(missing_d)} daily, {len(missing_i)} intraday missing from Alpaca"
                )
                self._yfinance_fallback(daily_syms=missing_d, hourly_syms=missing_i)
        else:
            log.info(
                f"[data_cache] no Alpaca keys — yfinance for {len(self.symbols)} symbols"
            )
            missing_d = [s for s in self.symbols if s not in self._daily]
            missing_i = [s for s in self.symbols if s not in self._hourly]
            self._yfinance_fallback(daily_syms=missing_d, hourly_syms=missing_i)

        # --- Cull and persist ---
        _cull(self._daily,  _DAILY_CULL_DAYS)
        _cull(self._hourly, _INTRADAY_CULL_DAYS)
        self._save_disk_cache()

        log.info(
            f"[data_cache] ready — {len(self._daily)}/{len(self.symbols)} daily, "
            f"{len(self._hourly)}/{len(self.symbols)} intraday"
        )
        self._fetched = True

    # ------------------------------------------------------------------ disk cache

    def _load_disk_cache(
        self,
    ) -> tuple[dict, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
        meta: dict = {"daily": {}, "intraday": {}}
        daily:    dict[str, pd.DataFrame] = {}
        intraday: dict[str, pd.DataFrame] = {}

        meta_file = self._cache_dir / "meta.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                log.warning(f"[data_cache] meta.json load failed ({e}) — starting fresh")
                meta = {"daily": {}, "intraday": {}}

        for timeframe, store in [("daily", daily), ("intraday", intraday)]:
            tf_dir = self._cache_dir / timeframe
            if not tf_dir.exists():
                continue
            for pf in tf_dir.glob("*.parquet"):
                sym = pf.stem
                try:
                    df = pd.read_parquet(pf)
                    if not df.empty:
                        store[sym] = df
                except Exception as e:
                    log.debug(f"[data_cache] load {sym}/{timeframe}: {e}")

        return meta, daily, intraday

    def _save_disk_cache(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        meta: dict = {"daily": {}, "intraday": {}}

        for timeframe, store, meta_tf in [
            ("daily",    self._daily,  meta["daily"]),
            ("intraday", self._hourly, meta["intraday"]),
        ]:
            tf_dir = self._cache_dir / timeframe
            tf_dir.mkdir(exist_ok=True)

            for sym, df in store.items():
                if df.empty:
                    continue
                try:
                    df.to_parquet(tf_dir / f"{sym}.parquet")
                    idx = df.index
                    if hasattr(idx, "tz") and idx.tz is not None:
                        start = idx.min().tz_convert(None).date()
                        end   = idx.max().tz_convert(None).date()
                    else:
                        start = pd.Timestamp(idx.min()).date()
                        end   = pd.Timestamp(idx.max()).date()
                    meta_tf[sym] = {"start": start.isoformat(), "end": end.isoformat()}
                except Exception as e:
                    log.warning(f"[data_cache] save {sym}/{timeframe}: {e}")

        try:
            with open(self._cache_dir / "meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            log.warning(f"[data_cache] meta.json save failed: {e}")

        total_d = sum(len(df) for df in self._daily.values())
        total_i = sum(len(df) for df in self._hourly.values())
        log.info(
            f"[data_cache] saved — {total_d:,} daily bars, {total_i:,} intraday bars "
            f"({len(self._daily)} daily / {len(self._hourly)} intraday symbol files)"
        )

    def _compute_gaps(
        self,
        symbols: list[str],
        timeframe: str,
        needed_start: date,
        needed_end: date,
        meta: dict,
    ) -> dict[tuple[str, str], list[str]]:
        """Return {(fetch_start, fetch_end): [symbols]} for any missing date ranges.

        Three gap types per symbol:
          - Not cached at all: fetch full [needed_start, needed_end]
          - Before-gap: cache starts after needed_start
          - After-gap:  cache ends more than 1 day before needed_end
        """
        meta_tf:    dict = meta.get(timeframe, {})
        gap_groups: dict[tuple[str, str], list[str]] = {}

        for sym in symbols:
            m = meta_tf.get(sym, {})
            if not m or not m.get("start") or not m.get("end"):
                key = (needed_start.isoformat(), needed_end.isoformat())
                gap_groups.setdefault(key, []).append(sym)
                continue

            cached_start = date.fromisoformat(m["start"])
            cached_end   = date.fromisoformat(m["end"])

            # Before-gap
            if needed_start < cached_start - timedelta(days=1):
                gap_end = min(cached_start - timedelta(days=1), needed_end)
                key = (needed_start.isoformat(), gap_end.isoformat())
                gap_groups.setdefault(key, []).append(sym)

            # After-gap (only if cache is more than 1 day stale)
            if cached_end < needed_end - timedelta(days=1):
                gap_start = cached_end + timedelta(days=1)
                key = (gap_start.isoformat(), needed_end.isoformat())
                gap_groups.setdefault(key, []).append(sym)

        return gap_groups

    # ------------------------------------------------------------------ Alpaca

    def _fetch_alpaca_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV bars from Alpaca for multiple symbols, handling pagination."""
        key    = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return {}

        headers = {
            "APCA-API-KEY-ID":     key,
            "APCA-API-SECRET-KEY": secret,
        }

        raw: dict[str, list] = {}
        chunk_size = 100

        for i in range(0, len(symbols), chunk_size):
            chunk  = symbols[i : i + chunk_size]
            params: dict = {
                "symbols":    ",".join(chunk),
                "timeframe":  timeframe,
                "start":      start,
                "limit":      10000,
                "adjustment": "all",
                "feed":       "iex",
            }
            if end:
                params["end"] = end

            page_token: str | None = None
            while True:
                if page_token:
                    params["page_token"] = page_token
                try:
                    resp = requests.get(
                        "https://data.alpaca.markets/v2/stocks/bars",
                        headers=headers,
                        params=params,
                        timeout=60,
                    )
                    if resp.status_code != 200:
                        log.warning(
                            f"[data_cache] Alpaca {timeframe} error "
                            f"{resp.status_code}: {resp.text[:200]}"
                        )
                        break
                    data = resp.json()
                    for sym, bars in data.get("bars", {}).items():
                        raw.setdefault(sym, []).extend(bars)
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
                except Exception as e:
                    log.warning(f"[data_cache] Alpaca {timeframe} request failed: {e}")
                    break

        dfs: dict[str, pd.DataFrame] = {}
        for sym, bars in raw.items():
            if not bars:
                continue
            df = pd.DataFrame(bars)
            df = df.rename(columns={
                "t": "Datetime", "o": "Open", "h": "High",
                "l": "Low",      "c": "Close", "v": "Volume",
            })
            df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)
            df = df.set_index("Datetime").sort_index()
            keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            dfs[sym] = df[keep]
            log.debug(f"[data_cache] Alpaca {timeframe} {sym}: {len(df)} bars")

        log.info(f"[data_cache] Alpaca {timeframe}: {len(dfs)}/{len(symbols)} symbols fetched")
        return dfs

    # ------------------------------------------------------------------ yfinance

    def _yfinance_fallback(
        self,
        daily_syms: list[str],
        hourly_syms: list[str],
    ) -> None:
        """Fill missing symbols using yfinance (2yr daily, 60d hourly)."""
        for sym in daily_syms:
            try:
                df = yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=True)
                if not df.empty:
                    self._daily[sym] = df
                    log.debug(f"[data_cache] yfinance daily {sym}: {len(df)} bars")
                else:
                    log.debug(f"[data_cache] {sym}: no daily bars returned")
            except Exception as e:
                log.warning(f"[data_cache] yfinance daily failed for {sym}: {e}")

        for sym in hourly_syms:
            try:
                df = yf.Ticker(sym).history(period="60d", interval="1h", auto_adjust=True)
                if not df.empty:
                    self._hourly[sym] = df
            except Exception as e:
                log.debug(f"[data_cache] yfinance hourly skipped for {sym}: {e}")

    # ------------------------------------------------------------------ query helpers

    def trading_days(self, start: date, end: date) -> list[date]:
        """Return sorted list of US trading days in [start, end] using SPY as calendar."""
        spy = self._daily.get("SPY", pd.DataFrame())
        if spy.empty:
            return []
        days: list[date] = []
        for ts in spy.index:
            d = ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()
            if start <= d <= end:
                days.append(d)
        return sorted(set(days))

    def daily_bars(self, symbol: str, as_of_date) -> pd.DataFrame:
        """Return daily OHLCV bars up to and including as_of_date (no lookahead)."""
        df = self._daily.get(symbol.upper(), pd.DataFrame())
        if df.empty:
            return df

        cutoff = _to_date(as_of_date)
        idx    = df.index
        if idx.tz is not None:
            mask = idx.normalize().tz_convert(None).date <= cutoff  # type: ignore[attr-defined]
        else:
            mask = pd.DatetimeIndex(idx).normalize().date <= cutoff  # type: ignore[attr-defined]
        return df[mask]

    def price_at(self, symbol: str, as_of_date) -> float | None:
        """Return the closing price on the most recent trading day on or before as_of_date."""
        bars = self.daily_bars(symbol, as_of_date)
        if bars.empty:
            return None
        return float(bars["Close"].iloc[-1])

    def intraday_price_at(self, symbol: str, sim_dt: datetime) -> "float | None":
        """Close of the last 15-min bar at or before sim_dt.

        Falls back to the daily close when intraday data is unavailable.
        """
        import pytz

        df = self._hourly.get(symbol.upper(), pd.DataFrame())
        if df.empty:
            return self.price_at(symbol, sim_dt.date())

        ET = pytz.timezone("America/New_York")
        if sim_dt.tzinfo is None:
            sim_dt_utc = ET.localize(sim_dt).astimezone(pytz.UTC)
        else:
            sim_dt_utc = sim_dt.astimezone(pytz.UTC)

        idx = df.index
        if idx.tz is None:
            idx_utc = pd.DatetimeIndex(idx).tz_localize("UTC")
        else:
            idx_utc = idx.tz_convert("UTC")

        mask     = idx_utc <= pd.Timestamp(sim_dt_utc)
        filtered = df[mask]
        if filtered.empty:
            return self.price_at(symbol, sim_dt.date())

        col = "Close" if "Close" in filtered.columns else "close"
        return float(filtered[col].iloc[-1])

    def intraday_bars(self, symbol: str, sim_date: date) -> pd.DataFrame:
        """Return all 15-min bars for sim_date (used for intraday stop checking)."""
        df = self._hourly.get(symbol.upper(), pd.DataFrame())
        if df.empty:
            return df
        idx = df.index
        if idx.tz is not None:
            day_dates = idx.normalize().tz_convert(None).date  # type: ignore[attr-defined]
        else:
            day_dates = pd.DatetimeIndex(idx).normalize().date  # type: ignore[attr-defined]
        return df[day_dates == sim_date]

    def intraday_bars_up_to(
        self, symbol: str, sim_dt: datetime, limit: int = 400
    ) -> pd.DataFrame:
        """Return the last `limit` 15-min bars with timestamps <= sim_dt (no lookahead)."""
        import pytz

        df = self._hourly.get(symbol.upper(), pd.DataFrame())
        if df.empty:
            return df

        ET = pytz.timezone("America/New_York")
        if sim_dt.tzinfo is None:
            sim_dt_utc = ET.localize(sim_dt).astimezone(pytz.UTC)
        else:
            sim_dt_utc = sim_dt.astimezone(pytz.UTC)

        idx = df.index
        if idx.tz is None:
            idx_utc = pd.DatetimeIndex(idx).tz_localize("UTC")
        else:
            idx_utc = idx.tz_convert("UTC")

        mask = idx_utc <= pd.Timestamp(sim_dt_utc)
        return df[mask].tail(limit)


# ------------------------------------------------------------------ module helpers

def _merge(store: dict[str, pd.DataFrame], fetched: dict[str, pd.DataFrame]) -> None:
    """Merge newly fetched bars into store, deduplicating by index timestamp."""
    for sym, new_df in fetched.items():
        if new_df.empty:
            continue
        if sym in store:
            combined = pd.concat([store[sym], new_df])
            store[sym] = combined[~combined.index.duplicated(keep="last")].sort_index()
        else:
            store[sym] = new_df


def _cull(store: dict[str, pd.DataFrame], cull_days: int) -> None:
    """Remove bars older than cull_days calendar days from every symbol in store."""
    cutoff_naive = pd.Timestamp(date.today() - timedelta(days=cull_days))
    for sym in list(store.keys()):
        df = store[sym]
        if df.empty:
            del store[sym]
            continue
        idx = df.index
        if hasattr(idx, "tz") and idx.tz is not None:
            cutoff = cutoff_naive.tz_localize("UTC")
        else:
            cutoff = cutoff_naive
        store[sym] = df[idx >= cutoff]
        if store[sym].empty:
            del store[sym]


def _to_date(val) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return pd.Timestamp(val).date()
