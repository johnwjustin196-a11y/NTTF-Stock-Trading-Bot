"""Position-level risk management utilities.

Covers: daily circuit breaker, earnings blackout, gap-up detection,
volume confirmation, stale position re-evaluation, and urgent news.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger
from ..utils.market_time import today_str

log = get_logger(__name__)


# ================================================================ daily state

def _daily_state_path() -> Path:
    cfg = load_config()
    return Path(cfg["paths"].get("data_dir", "data")) / "daily_state.json"


def _load_daily_state() -> dict:
    path = _daily_state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date") == today_str():
                return data
        except Exception:
            pass
    return {"date": today_str(), "start_equity": None, "circuit_breaker": False}


def _save_daily_state(state: dict) -> None:
    path = _daily_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_today_close(symbol: str) -> None:
    """Record that a position was closed today to block same-day re-entry."""
    state = _load_daily_state()
    closes = state.setdefault("today_closes", {})
    closes[symbol.upper()] = datetime.utcnow().isoformat()
    _save_daily_state(state)


def symbol_closed_today(symbol: str) -> bool:
    """Return True if this symbol had a position closed at any point today."""
    state = _load_daily_state()
    return symbol.upper() in state.get("today_closes", {})


def record_start_equity(broker) -> float:
    """Capture today's opening equity once (at 09:30). Returns the value."""
    state = _load_daily_state()
    if state.get("start_equity") is None:
        account = broker.get_account()
        state["start_equity"] = float(account.equity)
        _save_daily_state(state)
        log.info(f"[circuit-breaker] start-of-day equity: ${state['start_equity']:,.2f}")
    return float(state["start_equity"])


# ============================================================ circuit breaker

def check_circuit_breaker(broker) -> tuple[bool, float]:
    """Returns (triggered, loss_pct).

    loss_pct is 0.0 when the breaker was already set in a previous cycle
    (avoids re-tightening stops every cycle after the initial trigger).
    """
    cfg = load_config()
    threshold = float(cfg.get("trading", {}).get("circuit_breaker", {}).get("daily_loss_pct", 0.04))
    state = _load_daily_state()

    if state.get("circuit_breaker"):
        return True, 0.0  # already triggered — signal blocked but don't re-tighten

    start_equity = state.get("start_equity")
    if not start_equity:
        return False, 0.0

    account = broker.get_account()
    loss_pct = (start_equity - account.equity) / start_equity

    if loss_pct >= threshold:
        log.warning(
            f"[circuit-breaker] TRIGGERED — daily loss {loss_pct:.1%} >= {threshold:.1%} "
            f"(start=${start_equity:,.2f}, now=${account.equity:,.2f})"
        )
        state["circuit_breaker"] = True
        _save_daily_state(state)
        return True, float(loss_pct)

    return False, float(loss_pct)


def tighten_all_stops(broker) -> None:
    """Halve the gap between current price and each position's stop loss."""
    cfg = load_config()
    factor = float(cfg.get("trading", {}).get("circuit_breaker", {}).get("stop_tighten_factor", 0.5))

    positions = broker.get_positions()
    if not positions:
        return

    log.info(f"[circuit-breaker] tightening stops on {len(positions)} positions (factor={factor})")
    for pos in positions:
        if pos.stop_loss is None:
            continue
        try:
            q = broker.get_quote(pos.symbol)
            price = q.last
            old_stop = pos.stop_loss
            # Narrow the gap: new_stop = price - (price - old_stop) * factor
            new_stop = price - (price - old_stop) * factor
            new_stop = max(new_stop, old_stop)  # never loosen
            if new_stop > old_stop:
                broker.set_position_stop(pos.symbol, stop_loss=new_stop)
                log.info(
                    f"[circuit-breaker] {pos.symbol}: stop {old_stop:.2f} -> {new_stop:.2f} "
                    f"(price={price:.2f}, gap halved)"
                )
        except Exception as e:
            log.debug(f"[circuit-breaker] tighten {pos.symbol} failed: {e}")


def llm_position_review(
    symbol: str,
    tech: dict,
    news: dict,
    regime: dict,
    position,
    context: str = "",
) -> dict[str, Any]:
    """Ask the LLM whether to KEEP or CLOSE an open position.

    Returns dict with keys: recommendation ('KEEP'|'CLOSE'), reason, confidence.
    Falls back to KEEP with confidence=0 on any failure.
    """
    from ..utils.llm_client import chat

    cfg = load_config()
    max_tokens = int(cfg.get("llm", {}).get("max_tokens_advisor", 800))

    tags = getattr(position, "tags", {}) or {}
    entry = float(tags.get("entry_price", 0) or getattr(position, "avg_entry", 0) or 0)
    stop = getattr(position, "stop_loss", None)

    try:
        from ..broker import get_broker as _gb
        price = _gb().get_quote(symbol).last
    except Exception:
        price = entry

    pnl_pct = ((price - entry) / entry * 100) if entry else 0.0

    prompt = (
        f"Open position review: {symbol}\n"
        f"Entry ${entry:.2f} | Now ${price:.2f} | P&L {pnl_pct:+.1f}%"
        + (f" | Stop ${stop:.2f}" if stop else "") + "\n\n"
        f"Current signals:\n"
        f"  Technicals: {tech.get('score', 0):+.2f} — {tech.get('reason', '')[:100]}\n"
        f"  News:       {news.get('score', 0):+.2f} — {news.get('reason', '')[:100]}\n"
        f"  Regime:     {regime.get('label', 'unknown')}\n"
    )
    if context:
        prompt += f"\nContext: {context}\n"
    prompt += (
        "\nShould we KEEP or CLOSE this position? Reply exactly:\n"
        "RECOMMENDATION: KEEP or CLOSE\n"
        "REASON: one sentence\n"
        "CONFIDENCE: 0.0 to 1.0\n"
    )

    try:
        raw = chat(prompt, max_tokens=max_tokens, temperature=0.1, tag="position_reviewer")
        rec, reason, confidence = "KEEP", raw[:200], 0.5
        for line in raw.splitlines():
            u = line.strip().upper()
            if u.startswith("RECOMMENDATION:"):
                rec = "CLOSE" if "CLOSE" in u else "KEEP"
            elif line.strip().upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.strip().upper().startswith("CONFIDENCE:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return {"recommendation": rec, "reason": reason, "confidence": confidence}
    except Exception as e:
        log.warning(f"[position-review] LLM failed for {symbol}: {e}")
        return {"recommendation": "KEEP", "reason": f"LLM unavailable: {e}", "confidence": 0.0}


# ========================================================= earnings blackout

def check_earnings_blackout(symbol: str, news_score: float = 0.0) -> tuple[bool, str]:
    """Block new entry if earnings are within blackout_days AND no strong catalyst.

    Returns (block_trade, reason_string).
    """
    import yfinance as yf

    cfg = load_config()
    earn_cfg = cfg.get("trading", {}).get("earnings", {}) or {}
    blackout_days = int(earn_cfg.get("blackout_days", 3))
    min_news = float(earn_cfg.get("min_news_score_to_trade", 0.5))

    try:
        cal = yf.Ticker(symbol).calendar
        if cal is None:
            return False, ""

        earnings_date = None
        # yfinance returns a dict or DataFrame depending on version
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                earnings_date = dates[0]
        else:
            try:
                if "Earnings Date" in cal.index:
                    earnings_date = cal.loc["Earnings Date"].iloc[0]
            except Exception:
                pass

        if earnings_date is None:
            return False, ""

        edate = earnings_date.date() if hasattr(earnings_date, "date") else earnings_date
        days_away = (edate - datetime.utcnow().date()).days

        if 0 <= days_away <= blackout_days:
            if news_score >= min_news:
                return False, f"near earnings ({days_away}d) but strong news catalyst"
            return (
                True,
                f"earnings in {days_away}d — blackout "
                f"(news={news_score:.2f} < {min_news} threshold)",
            )
        return False, ""
    except Exception as e:
        log.debug(f"[earnings] {symbol} calendar check failed: {e}")
        return False, ""


# ============================================================== gap-up check

def check_gap_up(symbol: str) -> tuple[bool, float]:
    """Returns (is_gap_up, gap_pct). Caller decides if news supports the gap."""
    import yfinance as yf

    cfg = load_config()
    threshold = float(cfg.get("trading", {}).get("gap", {}).get("up_threshold_pct", 0.05))

    try:
        fi = yf.Ticker(symbol).fast_info
        current = float(getattr(fi, "last_price", 0) or 0)
        prev_close = float(getattr(fi, "previous_close", 0) or 0)
        if current <= 0 or prev_close <= 0:
            return False, 0.0
        gap_pct = (current - prev_close) / prev_close
        return gap_pct >= threshold, float(gap_pct)
    except Exception as e:
        log.debug(f"[gap] {symbol} check failed: {e}")
        return False, 0.0


# ========================================================= volume confirmation

def check_volume_confirmation(symbol: str) -> tuple[bool, float]:
    """Returns (volume_ok, vol_ratio). Fails open (True, 1.0) on data error."""
    import yfinance as yf

    cfg = load_config()
    min_ratio = float(cfg.get("trading", {}).get("volume", {}).get("min_ratio", 0.50))

    try:
        ticker = yf.Ticker(symbol)
        fi = ticker.fast_info
        avg_vol = int(
            getattr(fi, "three_month_average_volume", 0)
            or getattr(fi, "ten_day_average_volume", 0)
            or 0
        )
        bars = ticker.history(period="2d", interval="1d", auto_adjust=False)
        if bars.empty or avg_vol <= 0:
            return True, 1.0
        today_vol = int(bars["Volume"].iloc[-1])
        ratio = today_vol / avg_vol
        if ratio < min_ratio:
            log.debug(f"[volume] {symbol}: ratio={ratio:.2f} (today={today_vol:,}, avg={avg_vol:,})")
        return ratio >= min_ratio, float(ratio)
    except Exception as e:
        log.debug(f"[volume] {symbol} check failed: {e}")
        return True, 1.0


# ======================================================== position age review

def check_position_age(position, broker=None) -> tuple[bool, int]:
    """Returns (needs_review, age_days). needs_review when age >= max_age_days."""
    cfg = load_config()
    max_age = int(cfg.get("trading", {}).get("position_review", {}).get("max_age_days", 5))

    tags = getattr(position, "tags", {}) or {}

    # Prefer the entry_datetime tag stored at trade entry
    entry_str = tags.get("entry_datetime")
    if entry_str:
        try:
            entry_dt = datetime.fromisoformat(str(entry_str))
            age_days = (datetime.utcnow() - entry_dt.replace(tzinfo=None)).days
            return age_days >= max_age, age_days
        except Exception:
            pass

    # Fall back to order history (sim broker exposes .state)
    if broker and hasattr(broker, "state"):
        for o in reversed(broker.state.get("orders", [])):
            if o.get("symbol") == position.symbol and o.get("side") == "BUY":
                try:
                    raw = str(o.get("at", "")).split(".")[0]
                    entry_dt = datetime.fromisoformat(raw)
                    age_days = (datetime.utcnow() - entry_dt).days
                    return age_days >= max_age, age_days
                except Exception:
                    break

    return False, 0


# =========================================================== urgent news (2h)

def urgent_news_signal(symbol: str) -> dict[str, Any]:
    """Run news_signal with a short lookback window for intraday urgency detection."""
    from ..analysis.news_sentiment import news_signal
    cfg = load_config()
    hours = float(cfg.get("trading", {}).get("news_urgency", {}).get("lookback_hours", 2.0))
    return news_signal(symbol, lookback_hours=hours)
