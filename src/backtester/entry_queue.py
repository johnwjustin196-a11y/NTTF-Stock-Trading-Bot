"""Backtest-only deferred entry queue.

Mirrors the live entry_queue trigger rules without reading or writing the live
data/queue_cache/entry_queue.json file. The backtester drives this queue from
simulated intraday timestamps so queued support/resistance entries can be
measured in research runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ..analysis import technical_signal
from ..utils.logger import get_logger
from .signals import backtest_llm_signal, backtest_news_signal

log = get_logger(__name__)


class BacktestEntryQueue:
    """In-memory queue for one backtest run."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.entries: list[dict] = []
        self.history: list[dict] = []

    def add_entry(
        self,
        *,
        symbol: str,
        entry_type: str,
        trigger_price: float,
        fib_ratio: float,
        fib_direction: str,
        combined_score: float,
        price_at_queue: float,
        deep_size_mult: float,
        sim_dt: datetime,
    ) -> None:
        if not self.enabled:
            return
        sym = symbol.upper()
        self.entries = [e for e in self.entries if e["symbol"] != sym]
        expires = sim_dt.replace(hour=16, minute=0, second=0, microsecond=0)
        if expires <= sim_dt:
            expires += timedelta(days=1)
        entry = {
            "symbol": sym,
            "queued_at": sim_dt.isoformat(),
            "queued_cycle": sim_dt.strftime("%H:%M"),
            "entry_type": entry_type,
            "price_at_queue": round(float(price_at_queue or 0.0), 4),
            "trigger_price": round(float(trigger_price), 4),
            "fib_ratio": float(fib_ratio or 0.0),
            "fib_direction": fib_direction,
            "combined_score_at_queue": round(float(combined_score), 4),
            "deep_size_mult": round(float(deep_size_mult or 1.0), 4),
            "expires_at": expires.isoformat(),
            "check_count": 0,
        }
        self.entries.append(entry)
        self.history.append({"event": "queued", **entry})
        log.info(
            "[backtest-queue] queued %s %s @ %.2f current=%.2f score=%+.3f",
            sym,
            entry_type,
            float(trigger_price),
            float(price_at_queue or 0.0),
            float(combined_score),
        )

    def remove_entry(self, symbol: str, *, reason: str = "removed") -> None:
        sym = symbol.upper()
        kept = []
        for entry in self.entries:
            if entry["symbol"] == sym:
                self.history.append({"event": reason, **entry})
            else:
                kept.append(entry)
        self.entries = kept

    def has_entry(self, symbol: str) -> bool:
        sym = symbol.upper()
        return any(e["symbol"] == sym for e in self.entries)

    def expire(self, sim_dt: datetime) -> int:
        live: list[dict] = []
        removed = 0
        for entry in self.entries:
            try:
                expired = datetime.fromisoformat(entry["expires_at"]) <= sim_dt
            except Exception:
                expired = True
            if expired:
                removed += 1
                self.history.append({"event": "expired", "expired_at": sim_dt.isoformat(), **entry})
            else:
                live.append(entry)
        self.entries = live
        return removed

    def check_and_fire(
        self,
        *,
        broker,
        sim_dt: datetime,
        breadth: dict,
        regime: dict,
        newsapi_key: str,
        weights: dict,
        buy_threshold: float,
        use_llm: bool,
        execute_fn: Callable[[str, dict, float, dict], Any],
        news_cache: dict | None = None,
    ) -> list[dict]:
        """Check queued entries at sim_dt and execute any confirmed triggers."""
        if not self.enabled or not self.entries:
            return []

        self.expire(sim_dt)
        fired: list[dict] = []

        for entry in list(self.entries):
            entry["check_count"] = int(entry.get("check_count", 0)) + 1
            symbol = entry["symbol"]
            try:
                if entry["entry_type"] == "bounce_support":
                    triggered = self._check_bounce(broker, entry)
                elif entry["entry_type"] == "breakout_resistance":
                    triggered = self._check_breakout(broker, entry)
                else:
                    triggered = False

                if not triggered:
                    continue

                score, signals, used_llm = self._rescore(
                    broker=broker,
                    symbol=symbol,
                    sim_dt=sim_dt,
                    breadth=breadth,
                    regime=regime,
                    newsapi_key=newsapi_key,
                    weights=weights,
                    use_llm=use_llm and entry["check_count"] % 3 == 1,
                    news_cache=news_cache,
                )
                if score < buy_threshold:
                    self.history.append({
                        "event": "trigger_below_threshold",
                        "symbol": symbol,
                        "checked_at": sim_dt.isoformat(),
                        "entry_type": entry["entry_type"],
                        "score": round(float(score), 4),
                        "buy_threshold": buy_threshold,
                    })
                    continue

                tags = {
                    "entry_type": entry["entry_type"],
                    "fib_ratio": entry["fib_ratio"],
                    "trigger_price": entry["trigger_price"],
                    "queue_score": entry["combined_score_at_queue"],
                    "deep_size_mult": entry.get("deep_size_mult", 1.0),
                    "queued_at": entry.get("queued_at"),
                    "queued_cycle": entry.get("queued_cycle"),
                    "used_llm_rescore": used_llm,
                }
                self.remove_entry(symbol, reason="triggered")
                placed = execute_fn(symbol, tags, score, signals)
                event = {
                    "event": "fired",
                    "symbol": symbol,
                    "checked_at": sim_dt.isoformat(),
                    "entry_type": entry["entry_type"],
                    "score": round(float(score), 4),
                    "placed_status": getattr(placed, "status", None),
                    "qty": getattr(placed, "quantity", None),
                    "price": getattr(placed, "filled_price", None),
                }
                self.history.append(event)
                fired.append(event)
            except Exception as exc:
                self.history.append({
                    "event": "error",
                    "symbol": symbol,
                    "checked_at": sim_dt.isoformat(),
                    "entry_type": entry.get("entry_type"),
                    "error": str(exc)[:200],
                })
                log.debug("[backtest-queue] %s check failed at %s: %s", symbol, sim_dt, exc)

        return fired

    def _check_bounce(self, broker, entry: dict) -> bool:
        trigger = float(entry["trigger_price"])
        touch_pct = float(self.cfg.get("bounce_touch_pct", 0.01))
        touch_band = trigger * (1.0 + touch_pct)
        bars = broker.get_bars(entry["symbol"], "15m", limit=10)
        if bars.empty or len(bars) < 2:
            return False
        lows = bars["low"].astype(float)
        tested = bool((lows <= touch_band).any())
        last = bars.iloc[-1]
        prev = bars.iloc[-2]
        recovered = (
            float(last["close"]) > trigger
            and float(last["close"]) > float(prev["close"])
        )
        return tested and recovered

    def _check_breakout(self, broker, entry: dict) -> bool:
        trigger = float(entry["trigger_price"])
        for tf in ("15m", "1h"):
            bars = broker.get_bars(entry["symbol"], tf, limit=5)
            if bars.empty:
                continue
            last = bars.iloc[-1]
            if float(last["close"]) > trigger and float(last["open"]) > trigger:
                return True
        return False

    def _rescore(
        self,
        *,
        broker,
        symbol: str,
        sim_dt: datetime,
        breadth: dict,
        regime: dict,
        newsapi_key: str,
        weights: dict,
        use_llm: bool,
        news_cache: dict | None,
    ) -> tuple[float, dict, bool]:
        try:
            tech = technical_signal(broker, symbol, regime=str(regime.get("label") or "neutral"))
        except Exception:
            tech = {"symbol": symbol, "source": "technicals", "score": 0.0, "reason": "rescore failed", "details": {}}

        nkey = (symbol.upper(), sim_dt.isoformat(timespec="minutes"))
        if news_cache is not None and nkey in news_cache:
            news = news_cache[nkey]
        else:
            news = backtest_news_signal(symbol, sim_dt, newsapi_key)
            if news_cache is not None:
                news_cache[nkey] = news

        llm = {"symbol": symbol, "source": "llm", "score": 0.0,
               "action": "HOLD", "confidence": 0.0, "reason": "skipped (queue fast rescore)"}
        used_llm = False
        if use_llm:
            try:
                llm = backtest_llm_signal(
                    symbol=symbol,
                    tech=tech,
                    news=news,
                    breadth=breadth,
                    position_qty=0.0,
                    regime=regime,
                    as_of_date=sim_dt.date(),
                    deep_score=None,
                    similarity_line="",
                )
                used_llm = True
            except Exception:
                pass

        if used_llm:
            score = (
                weights.get("technicals", 0.35) * float(tech.get("score", 0.0))
                + weights.get("news", 0.15) * float(news.get("score", 0.0))
                + weights.get("breadth", 0.20) * float(breadth.get("score", 0.0))
                + weights.get("llm", 0.30) * float(llm.get("score", 0.0))
            )
        else:
            w_tech = weights.get("technicals", 0.35)
            w_news = weights.get("news", 0.15)
            total = w_tech + w_news or 1.0
            score = (
                w_tech * float(tech.get("score", 0.0))
                + w_news * float(news.get("score", 0.0))
            ) / total

        return float(score), {
            "technicals": tech,
            "news": news,
            "breadth": breadth,
            "llm": llm,
        }, used_llm
