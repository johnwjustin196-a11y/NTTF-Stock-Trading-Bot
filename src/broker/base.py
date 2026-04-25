"""Broker interface and shared dataclasses."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable

import pandas as pd


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float
    volume: int
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_entry: float
    market_value: float
    unrealized_pl: float
    # Per-position hard stop price (set at entry by decision_engine). None means
    # fall back to the config-level % stop.
    stop_loss: float | None = None
    take_profit: float | None = None
    # Free-form tags attached at entry (e.g. quality="strong", trend="uptrend")
    tags: dict = field(default_factory=dict)


@dataclass
class Order:
    symbol: str
    side: OrderSide
    quantity: float
    order_id: str = ""
    status: str = "pending"
    filled_price: float | None = None
    filled_at: datetime | None = None
    # Bracket orders
    stop_loss: float | None = None
    take_profit: float | None = None
    notes: str = ""


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float
    positions: list[Position] = field(default_factory=list)


class Broker(ABC):
    """Minimal broker interface shared by AlpacaBroker and SimBroker."""

    mode: str  # "sim" | "alpaca_paper" | "alpaca_live"
    broker_managed_stops: bool = False  # True → Alpaca holds real stop orders; skip in-process enforcement

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1d",
        limit: int = 100,
    ) -> pd.DataFrame:
        """Return OHLCV bars. Columns: open, high, low, close, volume. Index: timestamp."""

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_all(self) -> None: ...

    def set_position_stop(
        self, symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tags: dict | None = None,
    ) -> None:
        """Attach or update per-position risk metadata. Default: no-op.

        SimBroker overrides this to persist in its state JSON. Live brokers
        can either ignore it (we'll still enforce stop in-process) or plumb
        it through a native bracket order.
        """
        return None

    def close_position(self, symbol: str) -> Order | None:
        pos = next((p for p in self.get_positions() if p.symbol == symbol), None)
        if not pos or pos.quantity == 0:
            return None
        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return self.place_order(
            Order(symbol=symbol, side=side, quantity=abs(pos.quantity), notes="close")
        )

    def flatten_all(self) -> list[Order]:
        return [o for o in (self.close_position(p.symbol) for p in self.get_positions()) if o]
