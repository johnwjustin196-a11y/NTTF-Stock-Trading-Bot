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

from ..analysis.news_sentiment import news_signal
from ..analysis.technicals import technical_signal
from ..broker.base import Broker, Order, OrderSide
from ..utils.config import load_config, project_root
from ..utils.logger import get_logger

log = get_logger(__name__)

_QUEUE_FILE = project_root() / "data" / "queue_cache" / "entry_queue.json"


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


# ------------------------------------------------------------------ public API

def add_entry(
    symbol: str,
    entry_type: str,
    trigger_price: float,
    fib_ratio: float,
    fib_direction: str,
    combined_score: float,
    price_at_queue: float = 0.0,
) -> None:
    """Queue a deferred entry.  Replaces any existing entry for the symbol."""
    entries = [e for e in _load() if e["symbol"] != symbol]
    now = datetime.now(timezone.utc)
    # Expire at 20:00 UTC (4 PM ET) on the same calendar day
    expires = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if expires <= now:                          # already past 4 PM — next day (edge case)
        expires += timedelta(days=1)
    entries.append({
        "symbol":                  symbol,
        "queued_at":               now.isoformat(),
        "queued_cycle":            now.strftime("%H:%M"),
        "entry_type":              entry_type,    # "bounce_support" | "breakout_resistance"
        "price_at_queue":          round(price_at_queue, 4),
        "trigger_price":           round(trigger_price, 4),
        "fib_ratio":               fib_ratio,
        "fib_direction":           fib_direction,
        "combined_score_at_queue": round(combined_score, 4),
        "expires_at":              expires.isoformat(),
        "check_count":             0,
    })
    _save(entries)
    log.info(
        f"[entry_queue] queued {symbol} {entry_type} "
        f"@ trigger={trigger_price:.2f} current={price_at_queue:.2f} "
        f"(Fib {fib_ratio*100:.1f}%, score={combined_score:+.3f})"
    )


def remove_entry(symbol: str) -> None:
    entries = [e for e in _load() if e["symbol"] != symbol]
    _save(entries)


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


def expire_entries() -> int:
    """Remove expired entries.  Returns count removed."""
    all_entries = _load()
    now = datetime.now(timezone.utc)
    live = [e for e in all_entries if datetime.fromisoformat(e["expires_at"]) > now]
    removed = len(all_entries) - len(live)
    if removed:
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

    cfg        = load_config()
    buy_thresh = float(cfg.get("trading", {}).get("thresholds", {}).get("buy_threshold", 0.35))
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

            if score >= buy_thresh:
                # Executed — remove (decision logged to decisions_log.jsonl at EOD)
                remove_entry(symbol)
                tags = {
                    "entry_type":    entry_type,
                    "fib_ratio":     entry["fib_ratio"],
                    "trigger_price": entry["trigger_price"],
                    "queue_score":   entry["combined_score_at_queue"],
                }
                execute_fn(broker, symbol, tags)
                fired.append(symbol)
            else:
                # Score too low this time — leave in queue, try again next cycle
                log.info(
                    f"[entry_queue] {symbol} trigger fired but re-score {score:+.3f} "
                    f"below threshold {buy_thresh:+.3f} — staying in queue"
                )

        except Exception as exc:
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

    history_path = _QUEUE_FILE.parent / "queue_history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)

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
            "outcome":               "never_triggered",
        }
        rows.append(json.dumps(row))
        log.info(
            f"[entry_queue] EOD {symbol}: queued={price_q:.2f} "
            f"trigger={trigger:.2f} close={close:.2f if close else 'n/a'} "
            f"({'%+.2f%%' % pct_from_trigger if pct_from_trigger is not None else 'n/a'} from trigger)"
        )

    if rows:
        with open(history_path, "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        log.info(f"[entry_queue] wrote {len(rows)} EOD outcomes to {history_path.name}")


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
    min_score = float(
        cfg.get("trading", {}).get("thresholds", {}).get("queue_rescore_min", 0.0)
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
                remove_entry(symbol)
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
