"""BacktestBroker — implements the Broker interface using DataCache for prices.

Fills at last-known close ±0.05% spread (same as SimBroker).
Tracks positions and completed trades in memory — no disk I/O during the loop.
Call `set_sim_dt()` before each decision cycle to advance the clock.
Call `check_stops()` at end of each trading day to apply stop/TP logic using
the day's high/low bar.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, date
from typing import Optional

import pandas as pd

from ..broker.base import Account, Broker, Order, OrderSide, Position, Quote
from ..utils.logger import get_logger
from .data_cache import DataCache

log = get_logger(__name__)


class BacktestBroker(Broker):
    """In-memory broker backed by DataCache.

    Compatible with the full Broker interface so existing functions like
    `technical_signal(broker, symbol)` and `compute_dynamic_stop(broker, ...)`
    work without modification.
    """

    mode = "backtest"

    def __init__(self, cache: DataCache, starting_cash: float = 100_000.0) -> None:
        self._cache = cache
        self._starting_cash = float(starting_cash)
        self._cash = float(starting_cash)
        # symbol -> {qty, avg_entry, stop_loss, take_profit, tags}
        self._positions: dict[str, dict] = {}
        # All completed trades
        self.trades: list[dict] = []
        # All fills (open + close) for audit trail
        self._all_fills: list[dict] = []
        # Current simulation timestamp
        self._sim_dt: datetime = datetime.now()
        # Per-day stop-out counter — reset each new trading day
        self._day_stop_counts: dict[str, int] = {}
        self._stops_day: "date | None" = None
        # Slippage loaded once from config (avoids repeated file reads)
        try:
            from ..utils.config import load_config
            self._slippage = float(load_config().get("broker", {}).get("sim_slippage_pct", 0.001))
        except Exception:
            self._slippage = 0.001

    # ------------------------------------------------------------------ clock

    def set_sim_dt(self, dt: datetime) -> None:
        if self._stops_day is None or dt.date() != self._stops_day:
            self._day_stop_counts = {}
            self._stops_day = dt.date()
        self._sim_dt = dt

    def record_stop(self, symbol: str) -> None:
        """Increment today's stop-out count for this symbol."""
        key = symbol.upper()
        self._day_stop_counts[key] = self._day_stop_counts.get(key, 0) + 1

    def get_stop_count(self, symbol: str) -> int:
        """Return how many times this symbol was stopped out today."""
        return self._day_stop_counts.get(symbol.upper(), 0)

    # ------------------------------------------------------------------ price helper

    def _intraday_price(self, symbol: str) -> "float | None":
        """Return the intraday price at the current sim clock.

        During decision cycles (sim_dt has a time component) this uses the last
        hourly bar at or before sim_dt so each cycle sees the price it would
        have seen at that moment. Falls back to the daily close when hourly
        data is unavailable.
        """
        if self._sim_dt.hour != 0 or self._sim_dt.minute != 0:
            return self._cache.intraday_price_at(symbol, self._sim_dt)
        return self._cache.price_at(symbol, self._sim_dt.date())

    # ------------------------------------------------------------------ Broker API

    def get_account(self) -> Account:
        positions = self.get_positions()
        equity = self._cash + sum(p.market_value for p in positions)
        return Account(
            cash=self._cash,
            equity=equity,
            buying_power=self._cash,
            positions=positions,
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for sym, pos in self._positions.items():
            if pos["qty"] == 0:
                continue
            price = self._intraday_price(sym) or pos["avg_entry"]
            mv = pos["qty"] * price
            pl = (price - pos["avg_entry"]) * pos["qty"]
            out.append(Position(
                symbol=sym,
                quantity=pos["qty"],
                avg_entry=pos["avg_entry"],
                market_value=mv,
                unrealized_pl=pl,
                stop_loss=pos.get("stop_loss"),
                take_profit=pos.get("take_profit"),
                tags=pos.get("tags", {}),
            ))
        return out

    def get_quote(self, symbol: str) -> Quote:
        price = self._intraday_price(symbol)
        if price is None:
            raise RuntimeError(f"No price data for {symbol} at {self._sim_dt}")
        return Quote(
            symbol=symbol,
            last=price,
            bid=round(price * 0.9995, 4),
            ask=round(price * 1.0005, 4),
            volume=0,
            timestamp=self._sim_dt,
        )

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1d",
        limit: int = 100,
    ) -> pd.DataFrame:
        from datetime import timedelta
        sym = symbol.upper()
        _tf = timeframe.lower()

        # Intraday timeframes (15m, 1h, 5m, 30m, etc.) → 15-min cache, cut at sim_dt.
        # Daily indicators (RSI, MACD, SMA, BB, OBV, Fib) all explicitly request "1d"
        # so they are unaffected by this routing.
        is_daily = _tf in ("1d", "1day", "day", "daily") or _tf.endswith("day")
        if not is_daily:
            df = self._cache.intraday_bars_up_to(sym, self._sim_dt, limit)
            if df.empty:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
            return df[keep].tail(limit)

        # Daily bars: cut off at yesterday during intraday cycles so indicators
        # never see today's close before the market has actually closed.
        if self._sim_dt.hour != 0 or self._sim_dt.minute != 0:
            cutoff = self._sim_dt.date() - timedelta(days=1)
        else:
            cutoff = self._sim_dt
        df = self._cache.daily_bars(sym, cutoff)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep].tail(limit)

    def place_order(self, order: Order) -> Order:
        price = self._intraday_price(order.symbol)
        if price is None:
            log.debug(f"backtest: no price for {order.symbol} at {self._sim_dt} — reject")
            order.status = "rejected"
            return order

        fill_price = price * (1 + self._slippage) if order.side == OrderSide.BUY else price * (1 - self._slippage)
        cost = fill_price * order.quantity

        if order.side == OrderSide.BUY:
            if cost > self._cash:
                log.debug(f"backtest: insufficient cash for {order.symbol} (need ${cost:.0f}, have ${self._cash:.0f})")
                order.status = "rejected"
                return order
            self._cash -= cost
            pos = self._positions.get(order.symbol, {"qty": 0.0, "avg_entry": 0.0, "tags": {}})
            new_qty = pos["qty"] + order.quantity
            pos["avg_entry"] = (
                (pos["avg_entry"] * pos["qty"] + fill_price * order.quantity) / new_qty
                if new_qty > 0 else fill_price
            )
            pos["qty"] = new_qty
            self._positions[order.symbol] = pos

        else:  # SELL / CLOSE
            pos = self._positions.get(order.symbol, {"qty": 0.0, "avg_entry": fill_price})
            entry = pos.get("avg_entry", fill_price)
            pnl = (fill_price - entry) * order.quantity
            pnl_pct = pnl / (entry * order.quantity) if (entry * order.quantity) > 0 else 0.0
            self._cash += fill_price * order.quantity
            pos["qty"] = pos["qty"] - order.quantity
            if abs(pos["qty"]) < 1e-9:
                pos["qty"] = 0.0
            self._positions[order.symbol] = pos
            _tags = pos.get("tags") or {}
            _opened_at = ""
            _entry_dt_str = _tags.get("entry_datetime", "")
            if _entry_dt_str:
                try:
                    _opened_at = str(_entry_dt_str)[:10]
                except Exception:
                    pass
            _trade = {
                "symbol": order.symbol,
                "side": "SELL",
                "qty": order.quantity,
                "entry": round(entry, 4),
                "exit": round(fill_price, 4),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
                "reason": order.notes or "signal",
                "opened_at": _opened_at,
                "closed_at": str(self._sim_dt.date()),
            }
            for _k in ("entry_tech", "entry_news", "entry_breadth", "entry_llm",
                        "entry_combined", "entry_quality", "entry_regime", "entry_trend"):
                if _k in _tags:
                    _trade[_k] = _tags[_k]
            self.trades.append(_trade)

        order.order_id = str(uuid.uuid4())
        order.status = "filled"
        order.filled_price = fill_price
        order.filled_at = self._sim_dt
        self._all_fills.append({
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": order.quantity,
            "price": round(fill_price, 4),
            "at": str(self._sim_dt.date()),
        })
        log.debug(
            f"backtest fill: {order.side.value} {order.quantity} {order.symbol} "
            f"@ {fill_price:.2f} on {self._sim_dt.date()}"
        )
        return order

    def cancel_all(self) -> None:
        pass  # no resting orders in backtest

    def set_position_stop(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tags: dict | None = None,
    ) -> None:
        pos = self._positions.get(symbol)
        if not pos:
            return
        if stop_loss is not None:
            pos["stop_loss"] = float(stop_loss)
        if take_profit is not None:
            pos["take_profit"] = float(take_profit)
        if tags is not None:
            existing = pos.get("tags") or {}
            existing.update(tags)
            pos["tags"] = existing
        self._positions[symbol] = pos

    # ------------------------------------------------------------------ stop check

    def check_stops(self) -> list[dict]:
        """Check whether any open position hit its stop or take-profit today.

        Uses the day's daily bar high/low as a proxy for intraday price range.
        This is conservative: it assumes the worst case (stop fills at stop
        price, TP fills at TP price rather than through-price).
        Returns a list of triggered event dicts.
        """
        triggered: list[dict] = []
        today = self._sim_dt.date()

        for sym in list(self._positions.keys()):
            pos = self._positions[sym]
            if pos["qty"] <= 0:
                continue
            stop = pos.get("stop_loss")
            tp = pos.get("take_profit")
            if stop is None and tp is None:
                continue

            bars = self._cache.daily_bars(sym, today)
            if bars.empty:
                continue
            bar = bars.iloc[-1]
            # Check most recent bar is actually today
            bar_date = bar.name.date() if hasattr(bar.name, "date") else pd.Timestamp(bar.name).date()
            if bar_date != today:
                continue

            low = float(bar.get("Low", bar.get("low", 0)))
            high = float(bar.get("High", bar.get("high", 0)))
            open_price = float(bar.get("Open", bar.get("open", high)))

            # Stop loss — gap-aware fill:
            # overnight gap below stop → fill at open (best available price)
            # intraday slide through stop → fill at stop price
            # slippage applied in both cases
            if stop and low <= stop:
                is_locked = bool((pos.get("tags") or {}).get("locked_profit"))
                reason = "locked_profit_stop" if is_locked else "stop_loss"
                fill = (open_price if open_price <= stop else stop) * (1 - self._slippage)
                self._force_close(sym, pos, fill, reason)
                triggered.append({"symbol": sym, "type": reason, "price": fill, "date": str(today)})
                continue

            # Take profit hit — lock in profit by moving stop to TP level and letting winner run
            if tp and high >= tp:
                pos["stop_loss"] = float(tp)
                pos["take_profit"] = None
                existing_tags = pos.get("tags") or {}
                existing_tags["locked_profit"] = True
                existing_tags["locked_at"] = float(tp)
                existing_tags["trailing"] = False  # 50% daily ratchet only; not trailing-stop loop
                pos["tags"] = existing_tags
                self._positions[sym] = pos
                triggered.append({"symbol": sym, "type": "tp_lock", "price": float(tp), "date": str(today)})

        return triggered

    def close_position_stop(self, symbol: str) -> float:
        """Close a position that hit its stop using gap-aware fill logic.

        Overnight gap below stop → fills at the day's open (best available).
        Intraday slide through stop → fills at the stop price.
        Slippage applied in both cases. Returns the actual fill price.
        """
        pos = self._positions.get(symbol)
        if not pos or pos["qty"] <= 0:
            return 0.0
        stop = pos.get("stop_loss")
        if not stop:
            return 0.0
        today = self._sim_dt.date()
        fill = float(stop)
        bars = self._cache.daily_bars(symbol, today)
        if not bars.empty:
            bar = bars.iloc[-1]
            bar_date = bar.name.date() if hasattr(bar.name, "date") else pd.Timestamp(bar.name).date()
            if bar_date == today:
                open_price = float(bar.get("Open", bar.get("open", stop)))
                if open_price <= float(stop):
                    fill = open_price  # gapped overnight → open is the fill
        fill *= (1 - self._slippage)
        self._force_close(symbol, pos, fill, "stop_loss")
        return fill

    def _force_close(self, sym: str, pos: dict, fill_price: float, reason: str) -> None:
        qty = pos["qty"]
        entry = pos.get("avg_entry", fill_price)
        pnl = (fill_price - entry) * qty
        pnl_pct = pnl / (entry * qty) if (entry * qty) > 0 else 0.0
        self._cash += fill_price * qty
        _tags = pos.get("tags") or {}
        _opened_at = ""
        _entry_dt_str = _tags.get("entry_datetime", "")
        if _entry_dt_str:
            try:
                _opened_at = str(_entry_dt_str)[:10]
            except Exception:
                pass
        trade: dict = {
            "symbol": sym,
            "side": "SELL",
            "qty": qty,
            "entry": round(entry, 4),
            "exit": round(fill_price, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "reason": reason,
            "opened_at": _opened_at,
            "closed_at": str(self._sim_dt.date()),
        }
        if reason == "locked_profit_stop":
            locked_at = _tags.get("locked_at")
            if locked_at:
                trade["locked_at"] = round(float(locked_at), 4)
        for _k in ("entry_tech", "entry_news", "entry_breadth", "entry_llm",
                    "entry_combined", "entry_quality", "entry_regime", "entry_trend"):
            if _k in _tags:
                trade[_k] = _tags[_k]
        self.trades.append(trade)
        self._all_fills.append({
            "symbol": sym, "side": "SELL", "qty": qty,
            "price": round(fill_price, 4),
            "at": str(self._sim_dt.date()),
            "reason": reason,
        })
        del self._positions[sym]
        if reason == "stop_loss":
            self.record_stop(sym)
        log.debug(f"backtest {reason}: {sym} {qty} @ {fill_price:.2f} pnl={pnl:+.2f}")
