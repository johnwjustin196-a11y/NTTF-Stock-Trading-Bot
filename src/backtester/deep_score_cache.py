"""Persistent cache for deep score results.

Stores one scored entry per (symbol, as_of_date). On lookup the cache finds
the most recent entry where entry.as_of_date <= sim_date and the gap between
them is within MAX_GAP_DAYS (40). If the gap exceeds 40 days the result is
treated as missing and the caller must re-score.

Cull: entries older than 365 calendar days from today are removed on save
so the file never grows unboundedly.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..utils.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PATH = Path("data/deep_score_cache.json")
_MAX_GAP_DAYS = 40
_CULL_DAYS    = 365


class DeepScoreCache:
    """In-process cache backed by a single JSON file.

    All callers in the backtest engine run sequentially so no locking is needed.
    """

    def __init__(self, cache_file: str | Path | None = None) -> None:
        self._path  = Path(cache_file) if cache_file else _DEFAULT_PATH
        # symbol_upper -> list of entries sorted ascending by as_of_date
        self._data: dict[str, list[dict]] = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            total = sum(len(v) for v in self._data.values())
            log.info(
                f"[deep_score_cache] loaded {total} entries "
                f"for {len(self._data)} symbols from {self._path}"
            )
        except Exception as e:
            log.warning(f"[deep_score_cache] load failed ({e}) -- starting fresh")
            self._data = {}

    def save(self) -> None:
        """Cull stale entries then persist to disk."""
        self._cull()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, separators=(",", ":"))
            total = sum(len(v) for v in self._data.values())
            log.info(
                f"[deep_score_cache] saved {total} entries "
                f"for {len(self._data)} symbols"
            )
            self._dirty = False
        except Exception as e:
            log.warning(f"[deep_score_cache] save failed: {e}")

    # ------------------------------------------------------------------ access

    def get(self, symbol: str, sim_date: date) -> dict | None:
        """Return the best cached score for symbol as of sim_date.

        Returns None when:
          - no entry exists for the symbol, or
          - all entries are after sim_date, or
          - the most recent valid entry is more than MAX_GAP_DAYS (31) old.
        """
        entries = self._data.get(symbol.upper(), [])
        if not entries:
            return None

        iso   = sim_date.isoformat()
        valid = [e for e in entries if e.get("as_of_date", "") <= iso]
        if not valid:
            return None

        best = max(valid, key=lambda e: e["as_of_date"])
        gap  = (sim_date - date.fromisoformat(best["as_of_date"])).days
        if gap > _MAX_GAP_DAYS:
            return None

        return best

    def miss_reason(self, symbol: str, sim_date: date) -> str:
        """Return a human-readable string explaining why get() returns None."""
        entries = self._data.get(symbol.upper(), [])
        if not entries:
            return "no cache entries for this symbol"
        iso   = sim_date.isoformat()
        valid = [e for e in entries if e.get("as_of_date", "") <= iso]
        if not valid:
            oldest = min(entries, key=lambda e: e["as_of_date"])["as_of_date"]
            return f"all entries are after sim_date (oldest cached={oldest})"
        best = max(valid, key=lambda e: e["as_of_date"])
        gap  = (sim_date - date.fromisoformat(best["as_of_date"])).days
        return f"gap {gap}d > {_MAX_GAP_DAYS}d limit (best entry={best['as_of_date']})"

    def put(self, symbol: str, as_of_date: date, result: dict) -> None:
        """Store a deep score result. Overwrites any existing entry for this date."""
        sym   = symbol.upper()
        entry = {"as_of_date": as_of_date.isoformat(), **result}
        rows  = self._data.setdefault(sym, [])
        self._data[sym] = [e for e in rows if e.get("as_of_date") != entry["as_of_date"]]
        self._data[sym].append(entry)
        self._data[sym].sort(key=lambda e: e["as_of_date"])
        self._dirty = True

    def has_any(self, symbol: str) -> bool:
        """True if the cache holds at least one entry for this symbol."""
        return bool(self._data.get(symbol.upper()))

    def has_near(self, symbol: str, target_date: date, tolerance_days: int = 7) -> bool:
        """True if there is a stored entry within tolerance_days of target_date.

        Used by the backfill script so each interval date is only skipped when
        an entry actually close to that date exists — not just any entry that
        happens to fall within MAX_GAP_DAYS of the target.
        """
        entries = self._data.get(symbol.upper(), [])
        for e in entries:
            try:
                if abs((date.fromisoformat(e["as_of_date"]) - target_date).days) <= tolerance_days:
                    return True
            except (KeyError, ValueError):
                pass
        return False

    def coverage(self, symbols: list[str], sim_date: date) -> tuple[list[str], list[str]]:
        """Return (hit_symbols, miss_symbols) for a given sim_date."""
        hits, misses = [], []
        for sym in symbols:
            (hits if self.get(sym, sim_date) is not None else misses).append(sym)
        return hits, misses

    # ------------------------------------------------------------------ cull

    def _cull(self) -> None:
        """Remove entries older than CULL_DAYS from today (called inside save)."""
        cutoff = (date.today() - timedelta(days=_CULL_DAYS)).isoformat()
        removed = 0
        for sym in list(self._data.keys()):
            before = len(self._data[sym])
            self._data[sym] = [
                e for e in self._data[sym]
                if e.get("as_of_date", "") >= cutoff
            ]
            removed += before - len(self._data[sym])
            if not self._data[sym]:
                del self._data[sym]
        if removed:
            log.info(
                f"[deep_score_cache] culled {removed} entries "
                f"older than {_CULL_DAYS} days"
            )
