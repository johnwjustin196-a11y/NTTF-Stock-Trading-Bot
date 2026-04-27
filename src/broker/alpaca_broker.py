"""Alpaca Markets broker — paper and live trading via REST API.

Uses the same ALPACA_API_KEY / ALPACA_SECRET_KEY from .env as the data cache.
Paper trading base URL: https://paper-api.alpaca.markets
Live trading base URL:  https://api.alpaca.markets

Persists per-position metadata (stop_loss, take_profit, tags) to the same
data/state.json file as SimBroker so the dashboard works without changes.
Alpaca is the source of truth for qty/price; state.json holds only metadata.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from ..utils.config import load_config
from ..utils.logger import get_logger
from .base import Account, Broker, Order, OrderSide, Position, Quote

log = get_logger(__name__)

_TRADE_URL = {
    True:  "https://paper-api.alpaca.markets",
    False: "https://api.alpaca.markets",
}
_DATA_URL = "https://data.alpaca.markets"

# Maps bot timeframe strings → (Alpaca timeframe string, minutes per bar)
_TF_MAP: dict[str, tuple[str, int]] = {
    "1m":  ("1Min",  1),
    "5m":  ("5Min",  5),
    "15m": ("15Min", 15),
    "1h":  ("1Hour", 60),
    "1d":  ("1Day",  1440),
}


class AlpacaBroker(Broker):
    """Live/paper broker backed by the Alpaca Markets REST API v2."""

    def __init__(self, paper: bool = True) -> None:
        self.mode = "alpaca_paper" if paper else "alpaca_live"
        self.broker_managed_stops = True
        self._paper = paper
        self._trade_base = _TRADE_URL[paper]

        key = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env"
            )
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }

        cfg = load_config()
        self._state_path = Path(cfg["paths"]["state_file"])
        self._load_state()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        if self._state_path.exists():
            try:
                with open(self._state_path) as f:
                    self._state = json.load(f)
                self._state.setdefault("cash", 0.0)
                self._state.setdefault("positions", {})
                self._state.setdefault("orders", [])
            except Exception:
                self._state = {"cash": 0.0, "positions": {}, "orders": []}
        else:
            self._state = {"cash": 0.0, "positions": {}, "orders": []}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._state_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------ HTTP

    def _get(self, base: str, path: str, params: dict | None = None) -> dict | list:
        resp = requests.get(
            f"{base}{path}",
            headers=self._headers,
            params=params or {},
            timeout=15,
        )
        if resp.status_code not in (200, 207):
            raise RuntimeError(
                f"Alpaca GET {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def _post(self, base: str, path: str, body: dict) -> dict:
        resp = requests.post(
            f"{base}{path}",
            headers={**self._headers, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Alpaca POST {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def _delete(self, base: str, path: str) -> None:
        resp = requests.delete(
            f"{base}{path}",
            headers=self._headers,
            timeout=15,
        )
        if resp.status_code not in (200, 204, 207):
            log.warning(f"[alpaca] DELETE {path} -> {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------ Broker API

    def get_account(self) -> Account:
        data = self._get(self._trade_base, "/v2/account")
        cash = float(data.get("cash", 0))
        equity = float(data.get("equity", cash))
        buying_power = float(data.get("buying_power", cash))
        # Update cash before get_positions() saves state
        self._state["cash"] = cash
        positions = self.get_positions()
        return Account(cash=cash, equity=equity, buying_power=buying_power, positions=positions)

    def get_positions(self) -> list[Position]:
        try:
            data = self._get(self._trade_base, "/v2/positions")
        except Exception as e:
            log.warning(f"[alpaca] get_positions failed: {e}")
            return []

        alpaca_syms: set[str] = set()
        out: list[Position] = []

        for item in (data if isinstance(data, list) else []):
            sym = item.get("symbol", "").upper()
            if not sym:
                continue
            qty = float(item.get("qty", 0))
            if qty == 0:
                continue
            alpaca_syms.add(sym)
            avg_entry = float(item.get("avg_entry_price", 0))
            market_value = float(item.get("market_value", 0))
            unrealized_pl = float(item.get("unrealized_pl", 0))

            local = self._state["positions"].get(sym, {})
            out.append(Position(
                symbol=sym,
                quantity=qty,
                avg_entry=avg_entry,
                market_value=market_value,
                unrealized_pl=unrealized_pl,
                stop_loss=local.get("stop_loss"),
                take_profit=local.get("take_profit"),
                tags=local.get("tags") or {},
            ))
            # Keep local state qty/entry in sync with Alpaca
            self._state["positions"].setdefault(sym, {})
            self._state["positions"][sym]["qty"] = qty
            self._state["positions"][sym]["avg_entry"] = avg_entry

        # Drop local positions no longer held at Alpaca
        for sym in list(self._state["positions"].keys()):
            if sym not in alpaca_syms:
                del self._state["positions"][sym]

        self._save_state()
        return out

    def get_quote(self, symbol: str) -> Quote:
        sym = symbol.upper()
        # Try quotes endpoint first (gives real bid/ask)
        try:
            data = self._get(_DATA_URL, "/v2/stocks/quotes/latest",
                             {"symbols": sym, "feed": "iex"})
            q = (data if isinstance(data, dict) else {}).get("quotes", {}).get(sym)
            if q:
                bp = float(q.get("bp", 0))
                ap = float(q.get("ap", 0))
                if bp > 0 and ap > 0:
                    return Quote(
                        symbol=sym,
                        last=round((bp + ap) / 2, 4),
                        bid=bp,
                        ask=ap,
                        volume=int(q.get("bs", 0)) + int(q.get("as", 0)),
                        timestamp=datetime.now(timezone.utc),
                    )
        except Exception as e:
            log.debug(f"[alpaca] quotes/latest {sym}: {e}")

        # Fallback: latest bar close
        try:
            data = self._get(_DATA_URL, "/v2/stocks/bars/latest",
                             {"symbols": sym, "feed": "iex"})
            bar = (data if isinstance(data, dict) else {}).get("bars", {}).get(sym)
            if bar:
                price = float(bar.get("c", 0))
                if price > 0:
                    return Quote(
                        symbol=sym,
                        last=price,
                        bid=round(price * 0.9995, 4),
                        ask=round(price * 1.0005, 4),
                        volume=int(bar.get("v", 0)),
                        timestamp=datetime.now(timezone.utc),
                    )
        except Exception as e:
            log.debug(f"[alpaca] bars/latest fallback {sym}: {e}")

        raise RuntimeError(f"[alpaca] no price data for {sym}")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> pd.DataFrame:
        sym = symbol.upper()
        tf_str, tf_minutes = _TF_MAP.get(timeframe, ("1Day", 1440))
        now = datetime.now(timezone.utc)
        # 1.6x buffer covers weekends and holidays
        start_dt = now - timedelta(minutes=int(tf_minutes * limit * 1.6) + 60)
        params = {
            "symbols": sym,
            "timeframe": tf_str,
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit * 2,
            "adjustment": "all",
            "feed": "iex",
        }
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                data = self._get(_DATA_URL, "/v2/stocks/bars", params)
                bars = (data if isinstance(data, dict) else {}).get("bars", {}).get(sym, [])
                if not bars:
                    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                df = pd.DataFrame(bars)
                df = df.rename(columns={
                    "t": "Datetime", "o": "open", "h": "high",
                    "l": "low", "c": "close", "v": "volume",
                })
                df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)
                df = df.set_index("Datetime").sort_index()
                keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
                return df[keep].tail(limit)
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    log.debug(f"[alpaca] get_bars {sym} {timeframe} failed, retrying in 1s: {e}")
                    time.sleep(1)
        log.warning(f"[alpaca] get_bars {sym} {timeframe}: {last_exc}")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def _cancel_stop_order(self, sym: str) -> None:
        """Cancel any standing Alpaca stop/trailing-stop order for sym and remove from state."""
        order_id = self._state.get("positions", {}).get(sym, {}).get("stop_order_id")
        if order_id:
            try:
                self._delete(self._trade_base, f"/v2/orders/{order_id}")
                log.debug(f"[alpaca] cancelled stop order {order_id} for {sym}")
            except Exception as e:
                log.debug(f"[alpaca] cancel stop order {sym}: {e}")
            pos = self._state.get("positions", {}).get(sym)
            if pos:
                pos.pop("stop_order_id", None)
                pos.pop("stop_order_type", None)
                self._save_state()
        else:
            # No local order_id — query Alpaca for any open stop/trailing-stop orders
            # on this symbol (handles state drift after mode switches or restarts).
            try:
                open_orders = self._get(self._trade_base, "/v2/orders",
                                        {"status": "open", "symbols": sym, "limit": 10})
                for o in (open_orders if isinstance(open_orders, list) else []):
                    otype = o.get("type", "")
                    if otype in ("stop", "trailing_stop", "stop_limit"):
                        oid = o.get("id")
                        try:
                            self._delete(self._trade_base, f"/v2/orders/{oid}")
                            log.info(f"[alpaca] cancelled stale {otype} order {oid} for {sym}")
                        except Exception as e:
                            log.debug(f"[alpaca] cancel stale order {oid} {sym}: {e}")
            except Exception as e:
                log.debug(f"[alpaca] open-order lookup for {sym}: {e}")

    @staticmethod
    def _float_or_none(value) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _order_fill_snapshot(cls, data: dict | None) -> tuple[str, float, float | None]:
        """Return Alpaca status, confirmed filled quantity, and average fill price."""
        if not isinstance(data, dict):
            return "unknown", 0.0, None
        status = str(data.get("status") or "unknown").lower()
        filled_qty = cls._float_or_none(data.get("filled_qty")) or 0.0
        fill_price = cls._float_or_none(data.get("filled_avg_price"))
        return status, filled_qty, fill_price

    def _place_stop_order(self, sym: str, stop_price: float,
                          trail_pct: float | None = None) -> bool:
        """Place a GTC stop or trailing-stop order on Alpaca as a safety net.

        When trail_pct is given (e.g. 0.10 for 10%), places a native trailing_stop
        order so Alpaca auto-trails the high-water-mark in real time. Otherwise
        places a plain stop order at stop_price.
        """
        qty = int(self._state.get("positions", {}).get(sym, {}).get("qty", 0))
        if qty <= 0:
            return False

        if trail_pct:
            # Alpaca REST expects trail_percent as a percentage number (10.0 = 10%)
            body = {
                "symbol": sym,
                "qty": str(qty),
                "side": "sell",
                "type": "trailing_stop",
                "time_in_force": "gtc",
                "trail_percent": str(round(trail_pct * 100, 2)),
            }
            order_type = "trailing_stop"
            desc = f"trail {trail_pct:.1%}"
        else:
            body = {
                "symbol": sym,
                "qty": str(qty),
                "side": "sell",
                "type": "stop",
                "time_in_force": "gtc",
                "stop_price": str(round(stop_price, 2)),
            }
            order_type = "stop"
            desc = f"stop {stop_price:.2f}"

        try:
            resp = self._post(self._trade_base, "/v2/orders", body)
            order_id = resp.get("id")
            if order_id:
                pos = self._state["positions"].setdefault(sym, {})
                pos["stop_order_id"] = order_id
                pos["stop_order_type"] = order_type
                self._save_state()
                log.info(
                    f"[alpaca] {order_type} order placed: SELL {qty} {sym} "
                    f"@ {desc} (id={order_id})"
                )
                return True
            log.warning(f"[alpaca] {order_type} order for {sym} returned no id")
            return False
        except Exception as e:
            # 403 "insufficient qty" means Alpaca's available count lags the fill —
            # parse the actual available qty from the error body and retry once.
            err_str = str(e)
            if "insufficient qty" in err_str:
                try:
                    avail = int(json.loads(err_str[err_str.index("{"):]).get("available", 0))
                    if 0 < avail < qty:
                        body["qty"] = str(avail)
                        resp2 = self._post(self._trade_base, "/v2/orders", body)
                        order_id2 = resp2.get("id")
                        if order_id2:
                            pos = self._state["positions"].setdefault(sym, {})
                            pos["stop_order_id"] = order_id2
                            pos["stop_order_type"] = order_type
                            self._save_state()
                            log.info(
                                f"[alpaca] {order_type} order placed: SELL {avail} {sym} "
                                f"@ {desc} (id={order_id2}) [retried: avail={avail} of {qty}]"
                            )
                            return True
                        return False
                except Exception:
                    pass
            log.warning(f"[alpaca] place_stop_order {sym} @ {desc}: {e}")
            return False

    def place_order(self, order: Order) -> Order:
        sym = order.symbol.upper()
        side = "buy" if order.side == OrderSide.BUY else "sell"
        submitted_qty = int(order.quantity)
        if submitted_qty <= 0:
            log.warning(f"[alpaca] place_order {side} {order.quantity} {sym}: non-positive qty")
            order.status = "rejected"
            return order

        # Cancel any standing stop order before a market sell to avoid double-fill
        if order.side == OrderSide.SELL:
            self._cancel_stop_order(sym)

        body = {
            "symbol": sym,
            "qty": str(submitted_qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        try:
            resp = self._post(self._trade_base, "/v2/orders", body)
        except Exception as e:
            log.warning(f"[alpaca] place_order {side} {order.quantity} {sym}: {e}")
            order.status = "rejected"
            return order

        order_id = resp.get("id", str(uuid.uuid4()))
        latest = resp if isinstance(resp, dict) else {}
        status, filled_qty, fill_price = self._order_fill_snapshot(latest)
        terminal = {"filled", "canceled", "expired", "rejected", "done_for_day"}

        # Paper market orders typically fill within seconds; poll for confirmation.
        for _ in range(5):
            if status in terminal or (filled_qty > 0 and fill_price is not None):
                break
            time.sleep(1)
            try:
                latest = self._get(self._trade_base, f"/v2/orders/{order_id}")
                status, filled_qty, fill_price = self._order_fill_snapshot(
                    latest if isinstance(latest, dict) else {}
                )
            except Exception as poll_exc:
                log.debug(f"[alpaca] order poll {order_id} failed: {poll_exc}")
                break

        order.order_id = order_id
        order.status = status

        if filled_qty > 0 and fill_price is not None:
            order.quantity = filled_qty
            order.filled_price = fill_price
            order.filled_at = datetime.now(timezone.utc)
            self._record_fill(sym, order.side, filled_qty, fill_price, order.notes)
            log.info(
                f"[alpaca] {'PAPER ' if self._paper else ''}confirmed fill: "
                f"{side.upper()} {filled_qty} {sym} @ {fill_price:.2f} "
                f"(status={status}, id={order_id})"
            )
        elif filled_qty > 0:
            log.warning(
                f"[alpaca] {side.upper()} {sym} reports filled_qty={filled_qty} "
                f"but no avg fill price; local state not updated (id={order_id})"
            )
        else:
            log.warning(
                f"[alpaca] {side.upper()} {submitted_qty} {sym} not confirmed filled "
                f"(status={status}, id={order_id}); local state not updated"
            )
        return order

    def _record_fill(self, sym: str, side: OrderSide,
                     qty: float, fill_price: float, notes: str) -> None:
        """Update state.json positions and orders after a confirmed fill."""
        positions = self._state.setdefault("positions", {})
        orders = self._state.setdefault("orders", [])

        if side == OrderSide.BUY:
            pos = positions.get(sym, {"qty": 0.0, "avg_entry": 0.0})
            new_qty = pos["qty"] + qty
            pos["avg_entry"] = (
                (pos["avg_entry"] * pos["qty"] + fill_price * qty) / new_qty
                if new_qty > 0 else fill_price
            )
            pos["qty"] = new_qty
            positions[sym] = pos
        else:
            pos = positions.get(sym, {"qty": qty, "avg_entry": fill_price})
            pos["qty"] = max(0.0, pos["qty"] - qty)
            if pos["qty"] < 1e-9:
                positions.pop(sym, None)
            else:
                pos.pop("stop_order_id", None)    # stale after partial sell
                pos.pop("stop_order_type", None)
                positions[sym] = pos

        orders.append({
            "id": str(uuid.uuid4()),
            "symbol": sym,
            "side": side.value,
            "qty": qty,
            "price": round(fill_price, 4),
            "at": str(datetime.now(timezone.utc)),
            "notes": notes,
        })
        self._save_state()

    def cancel_all(self) -> None:
        try:
            self._delete(self._trade_base, "/v2/orders")
            log.info("[alpaca] cancel_all: open orders cancelled")
        except Exception as e:
            log.warning(f"[alpaca] cancel_all failed: {e}")

    def set_position_stop(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tags: dict | None = None,
    ) -> None:
        sym = symbol.upper()
        pos = self._state["positions"].get(sym)
        if not pos:
            return

        # Apply all metadata updates first so trail_pct is readable below
        if stop_loss is not None:
            pos["stop_loss"] = float(stop_loss)
        if take_profit is not None:
            pos["take_profit"] = float(take_profit)
        if tags is not None:
            existing = pos.get("tags") or {}
            existing.update(tags)
            pos["tags"] = existing
        self._state["positions"][sym] = pos
        self._save_state()

        # Sync the real Alpaca stop order when stop_loss changes
        if stop_loss is not None:
            pos_tags = pos.get("tags") or {}
            self._cancel_stop_order(sym)
            trail_pct = pos_tags.get("trail_pct") if pos_tags.get("trailing") else None
            placed = self._place_stop_order(sym, float(stop_loss), trail_pct=trail_pct)
            pos = self._state["positions"].setdefault(sym, {})
            pos_tags = pos.get("tags") or {}
            if placed:
                pos_tags["stop_order_status"] = "placed"
                pos_tags.pop("stop_order_error", None)
            else:
                pos_tags["stop_order_status"] = "failed"
                pos_tags["stop_order_error"] = "Alpaca stop order was not accepted"
            pos["tags"] = pos_tags
            self._state["positions"][sym] = pos
            self._save_state()
            if not placed:
                raise RuntimeError(f"[alpaca] stop order not placed for {sym}")
