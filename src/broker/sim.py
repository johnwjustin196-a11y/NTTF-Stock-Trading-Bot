"""Offline simulation broker. Uses yfinance for quotes/bars, fills at last price."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from ..utils.config import load_config
from ..utils.logger import get_logger
from ..utils.market_time import now_eastern
from .base import Account, Broker, Order, OrderSide, Position, Quote

log = get_logger(__name__)


class SimBroker(Broker):
    """Holds a JSON state file on disk; fills orders at the latest close from yfinance."""

    mode = "sim"

    def __init__(self) -> None:
        cfg = load_config()
        self.state_path = Path(cfg["paths"]["state_file"])
        self.starting_cash = 100_000.0
        self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        if self.state_path.exists():
            with open(self.state_path) as f:
                self.state = json.load(f)
        else:
            self.state = {
                "cash": self.starting_cash,
                "positions": {},   # symbol -> {qty, avg_entry}
                "orders": [],      # list of executed orders
            }
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    # ------------------------------------------------------------------ Broker API

    def get_quote(self, symbol: str) -> Quote:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if hist.empty:
            hist = ticker.history(period="5d", auto_adjust=False)
        if hist.empty:
            raise RuntimeError(f"No price data for {symbol}")
        last_row = hist.iloc[-1]
        price = float(last_row["Close"])
        vol = int(last_row["Volume"])
        return Quote(
            symbol=symbol,
            last=price,
            bid=price * 0.9995,
            ask=price * 1.0005,
            volume=vol,
            timestamp=now_eastern(),
        )

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> pd.DataFrame:
        # Map timeframe → yfinance args
        tf_map = {
            "1m": ("1d", "1m"),
            "5m": ("5d", "5m"),
            "15m": ("1mo", "15m"),
            "1h": ("3mo", "1h"),
            "1d": (f"{max(limit, 100)}d", "1d"),
        }
        period, interval = tf_map.get(timeframe, ("6mo", "1d"))
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
        )[["open", "high", "low", "close", "volume"]]
        return df.tail(limit)

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for sym, p in self.state["positions"].items():
            if p["qty"] == 0:
                continue
            try:
                q = self.get_quote(sym)
                mv = p["qty"] * q.last
                pl = (q.last - p["avg_entry"]) * p["qty"]
            except Exception:
                mv, pl = 0.0, 0.0
            out.append(Position(
                sym, p["qty"], p["avg_entry"], mv, pl,
                stop_loss=p.get("stop_loss"),
                take_profit=p.get("take_profit"),
                tags=p.get("tags", {}) or {},
            ))
        return out

    # ----------------------------- per-position metadata (stop, tp, tags)

    def set_position_stop(
        self, symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tags: dict | None = None,
    ) -> None:
        pos = self.state["positions"].get(symbol)
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
        self.state["positions"][symbol] = pos
        self._save()

    def get_account(self) -> Account:
        positions = self.get_positions()
        equity = self.state["cash"] + sum(p.market_value for p in positions)
        return Account(
            cash=self.state["cash"],
            equity=equity,
            buying_power=self.state["cash"],   # no margin in sim
            positions=positions,
        )

    def place_order(self, order: Order) -> Order:
        q = self.get_quote(order.symbol)
        cfg = load_config()
        slippage = float(cfg.get("broker", {}).get("sim_slippage_pct", 0.001))
        raw = q.ask if order.side == OrderSide.BUY else q.bid
        fill_price = raw * (1 + slippage) if order.side == OrderSide.BUY else raw * (1 - slippage)
        cost = fill_price * order.quantity

        if order.side == OrderSide.BUY:
            if cost > self.state["cash"]:
                log.warning(f"SIM: insufficient cash for {order.symbol} ${cost:.2f}")
                order.status = "rejected"
                return order
            self.state["cash"] -= cost
            pos = self.state["positions"].get(order.symbol, {"qty": 0, "avg_entry": 0})
            new_qty = pos["qty"] + order.quantity
            pos["avg_entry"] = (
                (pos["avg_entry"] * pos["qty"] + fill_price * order.quantity) / new_qty
                if new_qty else fill_price
            )
            pos["qty"] = new_qty
            self.state["positions"][order.symbol] = pos
        else:  # SELL
            pos = self.state["positions"].get(order.symbol, {"qty": 0, "avg_entry": 0})
            pos["qty"] -= order.quantity
            self.state["cash"] += cost
            if abs(pos["qty"]) < 1e-9:
                pos["qty"] = 0
            self.state["positions"][order.symbol] = pos

        order.order_id = str(uuid.uuid4())
        order.status = "filled"
        order.filled_price = fill_price
        order.filled_at = datetime.utcnow()
        self.state["orders"].append({
            "id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": order.quantity,
            "price": fill_price,
            "at": str(order.filled_at),
            "notes": order.notes,
        })
        self._save()
        log.info(f"SIM fill: {order.side.value} {order.quantity} {order.symbol} @ {fill_price:.2f}")
        return order

    def cancel_all(self) -> None:
        # sim has no resting orders
        pass
