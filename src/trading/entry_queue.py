"""Intraday entry queue — deferred BUY orders waiting for S/R confirmation.

When the main decision cycle identifies a good setup but price is not yet at
a Fibonacci support/resistance level, the ticker is placed in this queue with
entry conditions (bounce off support or candle close above resistance).

A lightweight 5-minute scheduler job calls check_and_fire() between main cycles.
When triggered it fast-rescores (technicals + news only, no LLM) and executes
if the combined score still clears the buy threshold. All entries expire at EOD.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..analysis.news_sentiment import news_signal
from ..analysis.technicals import technical_signal
from ..broker.base import Broker, Order, OrderSide
from ..utils.config import load_config, project_root
from ..utils.logger import get_logger

log = get_logger(__name__)

_QUEUE_FILE = project_root() / "data" / "queue_cache" / "entry_queue.json"
_HISTORY_FILE = _QUEUE_FILE.parent / "queue_history.jsonl"
_ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------------ persistence

def _load() -> list[dict]:
    try:
        if _QUEUE_FILE.exists():
            return json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save(entries: list[dict]) -> None:
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_FILE.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def _append_history(event: str, entry: dict, extra: dict | None = None) -> None:
    """Append one queue lifecycle event for later dashboard/EOD analysis."""
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        row = {
            "event": event,
            "outcome": event,
            "logged_at": now.isoformat(),
            "date": (entry.get("queued_at") or now.isoformat())[:10],
            "symbol": entry.get("symbol"),
            "entry_type": entry.get("entry_type"),
            "queued_at": entry.get("queued_at"),
            "queued_cycle": entry.get("queued_cycle"),
            "price_at_queue": entry.get("price_at_queue"),
            "trigger_price": entry.get("trigger_price"),
            "fib_ratio": entry.get("fib_ratio"),
            "fib_direction": entry.get("fib_direction"),
            "combined_score_at_queue": entry.get("combined_score_at_queue"),
            "deep_size_mult": entry.get("deep_size_mult"),
            "check_count": entry.get("check_count", 0),
        }
        if extra:
            row.update(extra)
        with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as exc:
        log.debug(f"[entry_queue] history write failed: {exc}")


# ------------------------------------------------------------------ public API

def add_entry(
    symbol: str,
    entry_type: str,
    trigger_price: float,
    fib_ratio: float,
    fib_direction: str,
    combined_score: float,
    price_at_queue: float = 0.0,
    deep_size_mult: float = 1.0,
) -> None:
    """Queue a deferred entry.  Replaces any existing entry for the symbol."""
    entries: list[dict] = []
    for old_entry in _load():
        if old_entry.get("symbol") == symbol:
            _append_history("replaced", old_entry)
        else:
            entries.append(old_entry)
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(_ET)
    # Expire at the New York market close; DST makes hardcoded UTC unsafe.
    expires_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    expires = expires_et.astimezone(timezone.utc)
    if expires <= now:                          # already past 4 PM — next day (edge case)
        expires_et += timedelta(days=1)
    expires = expires_et.astimezone(timezone.utc)
    entry = {
        "symbol":                  symbol,
        "queued_at":               now.isoformat(),
        "queued_cycle":            now_et.strftime("%H:%M"),
        "entry_type":              entry_type,    # "bounce_support" | "breakout_resistance"
        "price_at_queue":          round(price_at_queue, 4),
        "trigger_price":           round(trigger_price, 4),
        "fib_ratio":               fib_ratio,
        "fib_direction":           fib_direction,
        "combined_score_at_queue": round(combined_score, 4),
        "deep_size_mult":          round(float(deep_size_mult or 1.0), 4),
        "expires_at":              expires.isoformat(),
        "check_count":             0,
    }
    entries.append(entry)
    _save(entries)
    _append_history("queued", entry)
    log.info(
        f"[entry_queue] queued {symbol} {entry_type} "
        f"@ trigger={trigger_price:.2f} current={price_at_queue:.2f} "
        f"(Fib {fib_ratio*100:.1f}%, score={combined_score:+.3f})"
    )


def remove_entry(
    symbol: str,
    reason: str = "removed",
    extra: dict | None = None,
    log_history: bool = True,
) -> list[dict]:
    kept: list[dict] = []
    removed: list[dict] = []
    for entry in _load():
        if entry.get("symbol") == symbol:
            removed.append(entry)
        else:
            kept.append(entry)
    _save(kept)
    if log_history:
        for entry in removed:
            _append_history(reason, entry, extra)
    return removed


def has_entry(symbol: str) -> bool:
    return any(e["symbol"] == symbol for e in _load())


def get_entry(symbol: str) -> dict | None:
    return next((e for e in _load() if e["symbol"] == symbol), None)


def get_entries() -> list[dict]:
    """Return all non-expired entries."""
    now = datetime.now(timezone.utc)
    return [
        e for e in _load()
        if datetime.fromisoformat(e["expires_at"]) > now
    ]


def _trading_value(name: str, default: float) -> float:
    cfg = load_config()
    trading = cfg.get("trading", {}) or {}
    legacy_thresholds = trading.get("thresholds", {}) or {}
    return float(trading.get(name, legacy_thresholds.get(name, default)))


def expire_entries() -> int:
    """Remove expired entries.  Returns count removed."""
    all_entries = _load()
    now = datetime.now(timezone.utc)
    live = [e for e in all_entries if datetime.fromisoformat(e["expires_at"]) > now]
    removed = len(all_entries) - len(live)
    if removed:
        for entry in all_entries:
            if datetime.fromisoformat(entry["expires_at"]) <= now:
                _append_history("expired", entry)
        _save(live)
        log.info(f"[entry_queue] expired {removed} entries")
    return removed


def get_queue_summary() -> str:
    """One-liner per queued entry, suitable for injecting into an LLM prompt."""
    entries = get_entries()
    if not entries:
        return "No pending entry queue entries."
    lines = []
    for e in entries:
        lines.append(
            f"  {e['symbol']}: waiting for {e['entry_type'].replace('_', ' ')} "
            f"@ ${e['trigger_price']:.2f} "
            f"(Fib {e['fib_ratio']*100:.1f}%, queued {e['queued_cycle']}, "
            f"score was {e['combined_score_at_queue']:+.3f})"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ trigger detection

def _check_bounce(broker: Broker, entry: dict) -> bool:
    """True when price tested the support level (within 1%) and the latest 15m
    candle closed above both the support level and the previous candle's close.
    """
    symbol  = entry["symbol"]
    trigger = float(entry["trigger_price"])

    cfg = load_config()
    touch_pct = float(
        cfg.get("entry_queue", {}).get("bounce_touch_pct", 0.01)
    )
    touch_band = trigger * (1.0 + touch_pct)   # e.g. trigger * 1.01

    try:
        bars = broker.get_bars(symbol, "15m", limit=10)
        if bars.empty or len(bars) < 2:
            return False
        lows  = bars["low"].astype(float)
        # Low came within touch_pct of the support level
        tested    = bool((lows <= touch_band).any())
        last      = bars.iloc[-1]
        prev      = bars.iloc[-2]
        recovered = (
            float(last["close"]) > trigger                   # above support
            and float(last["close"]) > float(prev["close"])  # closed above prior candle
        )
        return tested and recovered
    except Exception:
        return False


def _check_breakout(broker: Broker, entry: dict) -> bool:
    """True when the most recent candle body closed fully above the resistance level."""
    symbol  = entry["symbol"]
    trigger = float(entry["trigger_price"])
    for tf in ("15m", "1h"):
        try:
            bars = broker.get_bars(symbol, tf, limit=5)
            if bars.empty:
                continue
            last = bars.iloc[-1]
            # Both open and close must be above trigger (full body above resistance)
            return (
                float(last["close"]) > trigger
                and float(last["open"]) > trigger
            )
        except Exception:
            continue
    return False


# ------------------------------------------------------------------ re-score

def fast_rescore(broker: Broker, symbol: str) -> float:
    """Technicals + news only re-score (no LLM, no deep scorer).

    Weights are renormalised so technicals + news sum to 1.0, preserving
    their relative importance from the full 4-signal combined score.
    """
    cfg = load_config()
    weights = cfg["signals"]["weights"]
    w_tech = weights.get("technicals", 0.35)
    w_news = weights.get("news", 0.15)
    total  = w_tech + w_news or 1.0

    try:
        tech_score = float(technical_signal(broker, symbol).get("score", 0.0))
    except Exception:
        tech_score = 0.0
    try:
        news_score = float(news_signal(symbol).get("score", 0.0))
    except Exception:
        news_score = 0.0

    return (w_tech * tech_score + w_news * news_score) / total


def full_rescore(broker: Broker, symbol: str) -> float:
    """Full 4-signal rescore — tech + news + breadth + LLM, same weights as
    decide_for_ticker().  Used when a queued trigger fires so the execution
    decision is made with the same information the original queue decision used.
    """
    from ..analysis import breadth_signal, llm_signal

    cfg = load_config()
    weights = cfg["signals"]["weights"]

    try:
        tech = technical_signal(broker, symbol)
    except Exception:
        tech = {"score": 0.0}
    try:
        news = news_signal(symbol)
    except Exception:
        news = {"score": 0.0}
    try:
        breadth = breadth_signal()
    except Exception:
        breadth = {"score": 0.0}
    try:
        llm = llm_signal(symbol, tech, news, breadth, position_qty=0.0)
    except Exception:
        llm = {"score": 0.0}

    return (
        weights.get("technicals", 0.35) * float(tech.get("score", 0.0))
        + weights.get("news",       0.15) * float(news.get("score", 0.0))
        + weights.get("breadth",    0.20) * float(breadth.get("score", 0.0))
        + weights.get("llm",        0.30) * float(llm.get("score", 0.0))
    )


# ------------------------------------------------------------------ main monitor

def _bump_check_counts(entries: list[dict]) -> None:
    """Increment check_count on each entry in-place and persist."""
    for e in entries:
        e["check_count"] = e.get("check_count", 0) + 1
    _save(entries)


def check_and_fire(broker: Broker, execute_fn: Any) -> list[str]:
    """Check every queued entry; fire when trigger conditions are met.

    `execute_fn(broker, symbol, tags)` is called with the broker and symbol
    to place the actual order.  Returns a list of symbols that triggered.
    """
    expire_entries()
    entries = get_entries()
    if not entries:
        return []

    # Bump counters once per cycle so we can throttle LLM to every 3rd check
    _bump_check_counts(entries)

    buy_thresh = _trading_value("buy_threshold", 0.35)
    fired: list[str] = []

    for entry in entries:
        symbol     = entry["symbol"]
        entry_type = entry["entry_type"]
        try:
            if entry_type == "bounce_support":
                triggered = _check_bounce(broker, entry)
            elif entry_type == "breakout_resistance":
                triggered = _check_breakout(broker, entry)
            else:
                triggered = False

            if not triggered:
                continue  # leave in cache — try again next 5-min cycle

            log.info(f"[entry_queue] {symbol} trigger confirmed ({entry_type})")
            # Use full rescore (LLM) on cycles 1, 4, 7 … ; fast rescore otherwise
            use_llm = (entry.get("check_count", 1) % 3 == 1)
            if use_llm:
                score = full_rescore(broker, symbol)
                log.info(
                    f"[entry_queue] {symbol} full re-score (LLM, check #{entry['check_count']}): "
                    f"{score:+.3f} (threshold {buy_thresh:+.3f})"
                )
            else:
                score = fast_rescore(broker, symbol)
                log.info(
                    f"[entry_queue] {symbol} fast re-score (no LLM, check #{entry['check_count']}): "
                    f"{score:+.3f} (threshold {buy_thresh:+.3f})"
                )

            rescore_type = "full_llm" if use_llm else "fast_no_llm"
            _append_history(
                "triggered",
                entry,
                {
                    "rescore": round(score, 4),
                    "buy_threshold": buy_thresh,
                    "rescore_type": rescore_type,
                    "passed_threshold": bool(score >= buy_thresh),
                },
            )

            if score >= buy_thresh:
                # Executed — remove (decision logged to decisions_log.jsonl at EOD)
                remove_entry(symbol, log_history=False)
                tags = {
                    "entry_type":    entry_type,
                    "fib_ratio":     entry["fib_ratio"],
                    "trigger_price": entry["trigger_price"],
                    "queue_score":   entry["combined_score_at_queue"],
                    "deep_size_mult": entry.get("deep_size_mult", 1.0),
                }
                placed = execute_fn(broker, symbol, tags)
                _append_history(
                    "fired" if placed is not None else "fire_skipped",
                    entry,
                    {
                        "rescore": round(score, 4),
                        "buy_threshold": buy_thresh,
                        "rescore_type": rescore_type,
                        "execution_result": "placed" if placed is not None else "skipped",
                        "order_status": getattr(placed, "status", None),
                        "filled_price": getattr(placed, "filled_price", None),
                        "qty": getattr(placed, "quantity", None),
                    },
                )
                if placed is not None:
                    fired.append(symbol)
            else:
                # Score too low this time — leave in queue, try again next cycle
                log.info(
                    f"[entry_queue] {symbol} trigger fired but re-score {score:+.3f} "
                    f"below threshold {buy_thresh:+.3f} — staying in queue"
                )

        except Exception as exc:
            _append_history("error", entry, {"error": str(exc)[:200]})
            log.warning(f"[entry_queue] error processing {symbol}: {exc}")
            # Don't remove on exception — try again next cycle

    return fired


# ------------------------------------------------------------------ EOD history log

def log_eod_outcomes() -> None:
    """Fetch the day's close price for every entry still in the queue and append
    a row to queue_history.jsonl before the EOD expire clears the cache.

    Each row records: symbol, entry_type, price_at_queue, trigger_price,
    close_price, pct_from_queue_to_close, pct_from_trigger_to_close,
    queued_at, queued_cycle — enough to verify the queue is surfacing good
    setups and not blocking valid orders.
    """
    import yfinance as yf

    entries = get_entries()
    if not entries:
        return

    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    rows: list[str] = []
    for entry in entries:
        symbol = entry["symbol"]
        try:
            ticker = yf.Ticker(symbol)
            hist   = ticker.history(period="1d", interval="1d")
            close  = float(hist["Close"].iloc[-1]) if not hist.empty else None
        except Exception:
            close = None

        price_q   = float(entry.get("price_at_queue", 0) or 0)
        trigger   = float(entry.get("trigger_price",  0) or 0)

        pct_from_queue   = round((close - price_q)   / price_q   * 100, 3) if close and price_q   else None
        pct_from_trigger = round((close - trigger)   / trigger   * 100, 3) if close and trigger   else None

        row = {
            "date":                  entry.get("queued_at", "")[:10],
            "symbol":                symbol,
            "entry_type":            entry.get("entry_type"),
            "queued_cycle":          entry.get("queued_cycle"),
            "price_at_queue":        price_q,
            "trigger_price":         trigger,
            "fib_ratio":             entry.get("fib_ratio"),
            "combined_score_at_queue": entry.get("combined_score_at_queue"),
            "close_price":           close,
            "pct_queue_to_close":    pct_from_queue,
            "pct_trigger_to_close":  pct_from_trigger,
            "event":                 "never_triggered",
            "outcome":               "never_triggered",
        }
        rows.append(json.dumps(row))
        close_s = f"{close:.2f}" if close is not None else "n/a"
        trig_pct_s = (
            "%+.2f%%" % pct_from_trigger
            if pct_from_trigger is not None else "n/a"
        )
        log.info(
            f"[entry_queue] EOD {symbol}: queued={price_q:.2f} "
            f"trigger={trigger:.2f} close={close_s} "
            f"({trig_pct_s} from trigger)"
        )

    if rows:
        with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        log.info(f"[entry_queue] wrote {len(rows)} EOD outcomes to {_HISTORY_FILE.name}")


# ------------------------------------------------------------------ startup validation

def validate_on_restart(broker: Broker) -> None:
    """On bot startup: fast-rescore every cached entry and drop any that no longer
    have positive signal.  Entries that still look good stay in the cache untouched
    so the 5-minute monitor picks them up without re-running the full decision cycle.
    """
    entries = get_entries()
    if not entries:
        return

    cfg = load_config()
    legacy_thresholds = (cfg.get("trading", {}) or {}).get("thresholds", {}) or {}
    min_score = float(
        (cfg.get("entry_queue", {}) or {}).get(
            "queue_rescore_min",
            legacy_thresholds.get("queue_rescore_min", 0.0),
        )
    )

    kept: list[str] = []
    dropped: list[str] = []
    for entry in entries:
        symbol = entry["symbol"]
        try:
            score = fast_rescore(broker, symbol)
            if score >= min_score:
                kept.append(symbol)
                log.info(
                    f"[entry_queue] restart: kept {symbol} "
                    f"{entry['entry_type']} @ {entry['trigger_price']:.2f} "
                    f"(rescore={score:+.3f})"
                )
            else:
                dropped.append(symbol)
                remove_entry(
                    symbol,
                    reason="restart_rescore_drop",
                    extra={"rescore": round(score, 4), "min_score": min_score},
                )
                log.info(
                    f"[entry_queue] restart: dropped {symbol} — "
                    f"rescore={score:+.3f} < min {min_score:+.3f}"
                )
        except Exception as exc:
            log.warning(f"[entry_queue] restart validation error for {symbol}: {exc}")

    log.info(
        f"[entry_queue] restart validation complete: "
        f"{len(kept)} kept, {len(dropped)} dropped"
    )
