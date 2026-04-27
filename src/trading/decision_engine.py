"""Decision engine — combines signals into trade actions, then executes."""
from __future__ import annotations

import time
from datetime import datetime
import math
from typing import Any

from ..analysis import (
    breadth_signal,
    classify_market_regime,
    classify_trade_quality,
    llm_signal,
    news_signal,
    technical_signal,
    trend_classification,
)
from ..broker import Broker, Order, OrderSide
from ..learning.journal import append_entry
from . import entry_queue
from ..screener import load_shortlist
from ..utils.config import load_config
from ..utils.logger import get_logger
from .position_manager import (
    compute_dynamic_stop,
    compute_size,
    compute_take_profit,
    compute_trailing_stop,
    should_flatten_for_risk,
)

log = get_logger(__name__)


def _valid_quote_price(quote) -> float | None:
    try:
        price = float(getattr(quote, "last", 0.0))
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def run_decision_cycle(broker: Broker, cycle_label: str) -> dict[str, Any]:
    """Run one scheduled decision pass over the shortlist + existing positions.

    cycle_label: "09:30" | "12:00" | "14:00" (used in journal entries)
    """
    cfg = load_config()
    symbols = set(load_shortlist())

    # Always include anything we already hold — we must decide HOLD/CLOSE for it
    for p in broker.get_positions():
        symbols.add(p.symbol)

    log.info(f"[{cycle_label}] decision cycle over {len(symbols)} tickers")

    # Record start-of-day equity at the opening bell; check circuit breaker every cycle
    from ..analysis.position_reviewer import (
        record_start_equity, check_circuit_breaker, tighten_all_stops, llm_position_review,
    )
    if cycle_label == "09:30":
        record_start_equity(broker)
    circuit_broken, loss_pct = check_circuit_breaker(broker)

    # Market-wide context, computed once per cycle
    breadth = breadth_signal()
    regime = classify_market_regime()
    log.info(f"Breadth: score={breadth['score']:+.2f} — {breadth['reason'][:120]}")
    log.info(f"Regime: {regime['label'].upper()} — {regime['reason'][:160]}")

    # Trailing-stop pass — only for brokers that don't manage stops natively.
    # AlpacaBroker places real GTC trailing-stop orders so Alpaca auto-trails;
    # running this updater for Alpaca would stomp the live order unnecessarily.
    if not getattr(broker, "broker_managed_stops", False):
        try:
            _update_trailing_stops(broker, cycle_label)
        except Exception as e:
            log.exception(f"trailing-stop update failed: {e}")

    results: list[dict] = []
    account = broker.get_account()
    held = {p.symbol: p for p in account.positions}

    # Circuit breaker: tighten stops + run LLM reviews on fresh trigger; block BUYs always
    if circuit_broken:
        log.warning("[circuit-breaker] ACTIVE — no new entries this cycle")
        if loss_pct > 0:  # fresh trigger (not a carry-over from earlier cycle)
            tighten_all_stops(broker)
            for pos in broker.get_positions():
                try:
                    tech = technical_signal(broker, pos.symbol)
                    news_s = news_signal(pos.symbol)
                    rev = llm_position_review(
                        pos.symbol, tech, news_s, regime, pos,
                        context=f"Circuit breaker triggered — portfolio down {loss_pct:.1%} today",
                    )
                    log.warning(
                        f"[circuit-breaker] {pos.symbol}: "
                        f"{rev['recommendation']} (confidence={rev.get('confidence', 0):.0%}) "
                        f"— {rev['reason']}"
                    )
                except Exception as _cbe:
                    log.debug(f"[circuit-breaker] review {pos.symbol} failed: {_cbe}")

    # Safety: if regime reads bearish/volatile AND breadth is weak in the last
    # intraday cycle (15:30), optionally flatten everything before close.
    if (
        cfg["trading"]["flatten_on_weak_close"]
        and cycle_label == "15:30"
        and (breadth["score"] <= -0.6 or regime["label"] in ("bearish", "volatile"))
    ):
        log.warning(
            f"Weak close detected (regime={regime['label']}, "
            f"breadth={breadth['score']:+.2f}) — flattening all positions"
        )
        orders = broker.flatten_all()
        for o in orders:
            append_entry({"cycle": cycle_label, "type": "flatten",
                          "order": _order_dict(o),
                          "breadth": breadth, "regime": regime})
            if o and getattr(o, "status", "") == "filled":
                _fire_postmortem(
                    o.symbol,
                    {"reason": f"flatten_on_weak_close | breadth={breadth['score']:+.2f} regime={regime['label']}"},
                    held.get(o.symbol), o,
                )
        return {"cycle": cycle_label, "flattened": True,
                "orders": [_order_dict(o) for o in orders]}

    # Phase 1: gather every decision up front (no execution yet) so we can
    # prioritise across the whole cycle rather than first-come-first-served.
    # Per-ticker timing helps spot when the LLM or a data fetch is dragging
    # the cycle; summary at the end prints total/avg/slowest so we can compare
    # across model swaps (DeepSeek R1 vs Qwen Instruct vs whatever comes next).
    decisions: dict[str, dict] = {}
    per_ticker_secs: list[tuple[str, float]] = []
    phase1_started = time.perf_counter()
    for sym in sorted(symbols):
        t0 = time.perf_counter()
        try:
            decisions[sym] = decide_for_ticker(
                broker, sym, breadth, regime, held.get(sym),
                circuit_broken=circuit_broken,
            )
        except Exception as e:
            log.exception(f"{sym}: decision failed: {e}")
        finally:
            dt = time.perf_counter() - t0
            per_ticker_secs.append((sym, dt))
            log.debug(f"{sym}: decision took {dt:.1f}s")
    phase1_total = time.perf_counter() - phase1_started
    if per_ticker_secs:
        avg = sum(d for _, d in per_ticker_secs) / len(per_ticker_secs)
        slowest = sorted(per_ticker_secs, key=lambda x: -x[1])[:3]
        slow_str = ", ".join(f"{s}={d:.1f}s" for s, d in slowest)
        log.info(
            f"[timing] signal gather: {len(per_ticker_secs)} tickers in "
            f"{phase1_total:.1f}s (avg {avg:.1f}s/ticker, slowest: {slow_str})"
        )

    def _record(sym: str, decision: dict, executed) -> None:
        entry = {
            "cycle": cycle_label,
            "symbol": sym,
            "decision": decision,
            "executed": _order_dict(executed) if executed else None,
            "regime": {"label": regime.get("label") or "neutral", "score": regime.get("score")},
        }
        append_entry(entry)
        results.append(entry)

    # Phase 2: CLOSEs first — frees up position slots for priority BUYs.
    for sym, decision in decisions.items():
        if decision["action"] != "CLOSE":
            continue
        try:
            executed = _execute(broker, decision, held.get(sym), account)
            _record(sym, decision, executed)
            # Per-trade post-mortem fires after close, never before
            if executed and getattr(executed, "status", "") == "filled":
                _fire_postmortem(sym, decision, held.get(sym), executed)
        except Exception as e:
            log.exception(f"{sym}: close exec failed: {e}")

    # Refresh account after closes so BUY sizing sees fresh buying power
    # and position count.
    account = broker.get_account()
    held = {p.symbol: p for p in account.positions}

    # Phase 3: BUYs in quality-priority order (strong > normal > weak,
    # tiebreak on combined_score). When max_positions is tight, this means
    # the best trades get the slots instead of whichever ticker sorted first
    # alphabetically.
    max_pos = _resolve_max_positions(cfg["trading"], regime["label"])
    log.info(f"[positions] regime={regime['label']} max_positions={max_pos}")
    buys = sort_buys_by_quality(
        [(s, d) for s, d in decisions.items() if d["action"] == "BUY"]
    )
    if buys:
        log.info(
            f"BUY queue ({len(buys)}): "
            + ", ".join(
                f"{s}[{(d.get('quality') or {}).get('label','?')}/"
                f"{d.get('combined_score',0):+.2f}]"
                for s, d in buys[:8]
            )
        )
    for sym, decision in buys:
        try:
            executed = _execute(broker, decision, held.get(sym), account, max_positions=max_pos)
            _record(sym, decision, executed)
            if _has_confirmed_entry_fill(executed):
                # Record setup fingerprint for similarity-based pattern recall
                try:
                    from ..learning.setup_memory import record_entry_fingerprint
                    record_entry_fingerprint(
                        sym, cycle_label, decision,
                        float(executed.filled_price or 0),
                    )
                except Exception as _fpe:
                    log.debug(f"{sym}: fingerprint record failed: {_fpe}")
                # Refresh so the next BUY sees the updated slot count / BP
                account = broker.get_account()
                held = {p.symbol: p for p in account.positions}
        except Exception as e:
            log.exception(f"{sym}: buy exec failed: {e}")

    # Phase 4: record HOLDs (they're useful in the journal & reflection)
    for sym, decision in decisions.items():
        if decision["action"] == "HOLD":
            _record(sym, decision, None)

    try:
        _ratchet_locked_profit_stops(broker, cycle_label)
    except Exception as e:
        log.exception(f"ratchet locked profit stops failed: {e}")

    placed = sum(1 for r in results if r["executed"])
    log.info(f"[{cycle_label}] done. {placed} orders placed.")
    return {"cycle": cycle_label, "count": len(results),
            "orders_placed": placed,
            "regime": regime["label"]}


# Mapping used for BUY-queue priority. Exposed as a module constant so the
# sort helper is easy to test in isolation.
QUALITY_RANK = {"strong": 3, "normal": 2, "weak": 1, "unknown": 0}
_CONFIRMED_ENTRY_STATUSES = {"filled", "partially_filled"}


def _has_confirmed_entry_fill(order: Order | None) -> bool:
    return bool(
        order
        and str(getattr(order, "status", "")).lower() in _CONFIRMED_ENTRY_STATUSES
        and getattr(order, "filled_price", None) is not None
        and float(getattr(order, "quantity", 0) or 0) > 0
    )


def sort_buys_by_quality(buys: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Sort (symbol, decision) pairs so strong trades come first.

    Primary key: quality rank (strong > normal > weak > unknown).
    Tiebreak: combined_score desc — if two trades tie on quality, the one
    with the higher conviction wins the slot.
    """
    return sorted(
        buys,
        key=lambda x: (
            -QUALITY_RANK.get((x[1].get("quality") or {}).get("label", "unknown"), 0),
            -float(x[1].get("combined_score", 0.0)),
        ),
    )


def decide_for_ticker(
    broker: Broker,
    symbol: str,
    breadth: dict,
    regime: dict,
    position=None,
    circuit_broken: bool = False,
) -> dict[str, Any]:
    cfg = load_config()

    # Change #37: cooldown check - earliest gate before any heavy computation
    from ..learning.track_record import symbol_on_cooldown
    if symbol_on_cooldown(symbol):
        return {
            "symbol": symbol,
            "action": "HOLD",
            "combined_score": 0.0,
            "deep_size_mult": 1.0,
            "gap_up": False,
            "reason": "cooldown: 3+ stops in 15 days",
            "trend": {"label": None, "short": None, "long": None},
                        "regime": {"label": regime.get("label") or "neutral", "score": regime.get("score")},
            "quality": {"label": "unknown"},
            "signals": {},
        }

    # Prefer auto-tuned weights from data/signal_weights.json if present; fall
    # back to settings.yaml defaults if the overlay doesn't exist yet or the
    # learning package isn't importable for any reason.
    try:
        from ..learning.signal_weights import effective_weights
        weights = effective_weights()
    except Exception:
        weights = cfg["signals"]["weights"]

    # 1. Stop/TP check — in-process for sim/backtest; TP-lock only for Alpaca
    #    (Alpaca fires real GTC stop orders so we never fire a duplicate market sell)
    if position:
        try:
            q = broker.get_quote(symbol)
            if not getattr(broker, "broker_managed_stops", False):
                flatten, why = should_flatten_for_risk(position, q.last)
                if flatten:
                    return {
                        "symbol": symbol,
                        "action": "CLOSE",
                        "combined_score": -1.0 if position.quantity > 0 else +1.0,
                        "reason": why,
                        "signals": {},
                    }
            else:
                _check_tp_lock(broker, position, q.last)
        except Exception as e:
            log.debug(f"{symbol}: quote/stop check failed: {e}")

    # 2. Gather per-ticker signals
    tech = technical_signal(
        broker,
        symbol,
        regime=str(regime.get("label") or "neutral") if isinstance(regime, dict) else None,
    )
    news = news_signal(symbol)
    trend = trend_classification(symbol)
    # Partial snapshot passed to advisor so it can query the setup fingerprint DB
    _partial_snap = {
        "regime": regime,
        "trend": trend,
        "signals": {"technicals": tech, "news": news, "breadth": breadth},
    }
    llm = llm_signal(
        symbol, tech, news, breadth,
        position_qty=position.quantity if position else 0.0,
        regime=regime,
        decision_snapshot=_partial_snap,
    )

    _trend_label = str(trend.get("label", "")).lower()
    downtrend_entry_blocked = (not position) and ("downtrend" in _trend_label)

    # 3. Weighted combination
    combined = (
        weights["technicals"] * tech["score"]
        + weights["news"] * news["score"]
        + weights["breadth"] * breadth["score"]
        + weights["llm"] * llm["score"]
    )

    # 3b. Deep score gate — reads pre-computed trade_scores.json (no LLM call here)
    deep_allow = True
    deep_size_mult = 1.0
    deep_note = ""
    deep_score_val: float | None = None
    deep_grade_val: str | None = None
    try:
        from ..analysis.deep_scorer import deep_score_gate, get_score as _get_deep_score
        deep_allow, deep_size_mult, deep_note = deep_score_gate(symbol)
        _ds = _get_deep_score(symbol) or {}
        deep_score_val = _ds.get("score")
        deep_grade_val = _ds.get("grade")
    except Exception as _dse:
        log.debug(f"{symbol}: deep_score_gate error (skipping): {_dse}")

    extra_notes: list[str] = []
    _gate_blocks: list[str] = []  # explicit filter blocks (subset of extra_notes)
    gap_up = False

    # 3c. Promoted hard rules — high-confidence rules enforced before action logic.
    # Runs after signals are gathered but before action is decided. Never delays
    # the LLM call; the LLM already ran above and its result is in `llm`.
    promoted_blocks: list[str] = []
    try:
        from ..learning.rules import check_promoted_rules
        _snap_for_rules = {
            "action": "BUY",  # check as if we're about to BUY
            "regime": (regime.get("label") or "neutral") if isinstance(regime, dict) else "neutral",
            "trend": trend.get("label") if isinstance(trend, dict) else "",
            "signals": {"technicals": tech.get("score", 0.0), "news": news.get("score", 0.0), "breadth": breadth.get("score", 0.0)},
        }
        promoted_blocks = check_promoted_rules(
            _snap_for_rules,
            regime=(regime.get("label") or "neutral") if isinstance(regime, dict) else None,
        )
    except Exception as _pre:
        log.debug(f"{symbol}: promoted rule check failed: {_pre}")

    # 4. Action logic (regime no longer hard-blocks BUYs; it shrinks size
    #    via compute_size and — below — filters out weak-quality trades.)
    t = cfg["trading"]
    llm_action = str(llm.get("action", "HOLD")).upper()

    # Changes #26, #27, #28: dynamic threshold multipliers
    effective_threshold = float(t["buy_threshold"])
    if "pullback" in _trend_label or _trend_label == "weak_uptrend":
        effective_threshold *= 1.15
    elif "downtrend" in _trend_label:
        effective_threshold *= 1.40
    _llm_confidence = llm.get("confidence", 0.0) if llm else 0.0
    if llm_action == "HOLD" and _llm_confidence >= 0.75:
        effective_threshold *= 1.30
    try:
        _atr_pct = tech.get("details", {}).get("atr_pct", None)
        _intraday_move = tech.get("details", {}).get("intraday_move_pct", None)
        if _atr_pct is None or _intraday_move is None:
            _open_px = tech.get("details", {}).get("open", None)
            _cur_px = tech.get("details", {}).get("last_price", None)
            if _open_px and _cur_px and _open_px > 0:
                _intraday_move = abs((_cur_px - _open_px) / _open_px)
        if _atr_pct and _intraday_move and _intraday_move > 2 * _atr_pct:
            effective_threshold *= 1.10
    except Exception:
        pass

    if llm_action == "CLOSE" and position:
        # Guard: don't let the LLM close a fresh position just because RSI looks
        # overbought. Alpaca already has real stop/TP orders on the position —
        # closing after one cycle defeats the entire risk plan and causes the
        # buy-sell-rebuy churn seen in live trading.
        # Allow immediate close only when confidence is very high (>= 0.85).
        # After min_hold_hours_before_llm_close the LLM can close at any confidence.
        _min_hold_h = float(t.get("min_hold_hours_before_llm_close", 3.0))
        _allow_llm_close = True
        if _min_hold_h > 0:
            _tags = getattr(position, "tags", {}) or {}
            _entry_str = _tags.get("entry_datetime", "")
            if _entry_str:
                try:
                    _entry_dt = datetime.fromisoformat(str(_entry_str))
                    _hold_h = (datetime.utcnow() - _entry_dt.replace(tzinfo=None)).total_seconds() / 3600
                    _llm_conf = float(llm.get("confidence", 0.0))
                    if _hold_h < _min_hold_h and _llm_conf < 0.85:
                        _allow_llm_close = False
                        log.info(
                            f"{symbol}: LLM CLOSE deferred — "
                            f"held {_hold_h:.1f}h < {_min_hold_h:.1f}h min, "
                            f"conf={_llm_conf:.0%} < 85%"
                        )
                        extra_notes.append(
                            f"LLM CLOSE deferred ({_hold_h:.1f}h < {_min_hold_h:.1f}h min, "
                            f"conf={_llm_conf:.0%})"
                        )
                except Exception:
                    pass  # can't determine age — allow close
        if _allow_llm_close:
            action = "CLOSE"
    elif not deep_allow and not position:
        action = "HOLD"  # deep score veto — do not enter
        _gate_blocks.append(f"deep_score_veto: {deep_note}" if deep_note else "deep_score_veto")
    elif combined >= effective_threshold and not position:
        action = "BUY"
    elif combined <= t["sell_threshold"] and position and position.quantity > 0:
        action = "CLOSE"
    elif (llm_action == "BUY" and not position
          and combined >= effective_threshold * 0.7):
        action = "BUY"  # LLM-initiated entry, slightly looser threshold
    else:
        action = "HOLD"

    # Change #35: minimum tech score gate
    _tech_score = tech.get("score", 0.0)
    if action == "BUY" and _tech_score < 0.10:
        action = "HOLD"
        _gate_blocks.append(f"tech_score_below_min: {_tech_score:.2f} < 0.10")
        extra_notes.append("[blocked: tech_score below 0.10]")

    # 5. Trade-quality tag (only meaningful on BUYs, but we compute it always
    #    so the dashboard can show the "would-have" quality for HOLDs too)
    quality = classify_trade_quality(
        combined_score=combined,
        tech=tech, news=news, breadth=breadth, llm=llm,
        trend=trend, regime=regime,
    )

    # Change #34: hard-block new downtrend entries, but still let held
    # positions continue through close/review/urgent-news logic.
    if action == "BUY" and downtrend_entry_blocked:
        action = "HOLD"
        _gate_blocks.append("downtrend_entry_blocked")
        extra_notes.append("blocked: downtrend entry")

    # Change #24: hard-block weak quality in any downtrend-adjacent condition
    if action == "BUY" and quality.get("label") == "weak" and "downtrend" in _trend_label:
        action = "HOLD"
        _gate_blocks.append("weak_quality_in_downtrend")
        extra_notes.append("blocked: weak quality in downtrend")

    # 5b. Promoted hard-rule enforcement (BUY only)
    if action == "BUY" and promoted_blocks:
        action = "HOLD"
        _gate_blocks.extend(promoted_blocks)
        extra_notes.extend(promoted_blocks)

    # 6. Adverse-regime quality gate: if the tape is bearish/volatile and the
    #    best we can tag this trade is "weak", don't take it. Strong/normal
    #    still go through (but will be sized smaller by compute_size).
    regime_label = str(regime.get("label") or "neutral").lower()
    adverse = set(str(x).lower() for x in t.get("adverse_regimes", ["bearish", "volatile"]))
    filter_note = None
    if (action == "BUY"
            and t.get("skip_weak_in_adverse_regimes", True)
            and regime_label in adverse
            and quality.get("label") == "weak"):
        action = "HOLD"
        filter_note = f"BUY skipped: weak quality in {regime_label} regime"
        _gate_blocks.append(filter_note)

    # 7. Additional pre-BUY filters: tech score floor, circuit breaker, volume, earnings, gap-up
    if action == "BUY":
        # Min tech score gate — blocks entries driven purely by news/LLM with flat technicals
        _min_tech = float(t.get("min_entry_tech_score", 0.0))
        if _min_tech > 0 and tech.get("score", 0) < _min_tech:
            action = "HOLD"
            _gate_blocks.append(f"tech_score_too_low({tech.get('score', 0):.3f})")
            extra_notes.append(f"blocked: tech score {tech.get('score', 0):.3f} < min {_min_tech:.3f}")

        # Same-day re-entry block — never re-buy a ticker we already closed today.
        # Prevents the buy→LLM-close→re-buy-higher churn seen in live trading.
        if bool(t.get("same_day_reentry_blocked", True)) and not position:
            try:
                from ..analysis.position_reviewer import symbol_closed_today
                if symbol_closed_today(symbol):
                    action = "HOLD"
                    _gate_blocks.append("same_day_reentry_blocked")
                    extra_notes.append("blocked: same-day re-entry (closed earlier today)")
                    log.info(f"{symbol}: BUY blocked — already closed this ticker today")
            except Exception as _sdr:
                log.debug(f"{symbol}: same-day re-entry check failed: {_sdr}")

        if circuit_broken:
            action = "HOLD"
            _gate_blocks.append("circuit_breaker_active")
            extra_notes.append("circuit breaker active — no new entries")
        else:
            # Volume confirmation
            try:
                from ..analysis.position_reviewer import check_volume_confirmation
                vol_ok, vol_ratio = check_volume_confirmation(symbol)
                if not vol_ok:
                    action = "HOLD"
                    _gate_blocks.append(f"low_volume: ratio={vol_ratio:.2f}")
                    extra_notes.append(f"low volume (ratio={vol_ratio:.2f}) — skipping")
            except Exception as _ve:
                log.debug(f"{symbol}: volume check error: {_ve}")

            # Earnings blackout
            if action == "BUY":
                try:
                    from ..analysis.position_reviewer import check_earnings_blackout
                    earn_blocked, earn_reason = check_earnings_blackout(symbol, news["score"])
                    if earn_blocked:
                        action = "HOLD"
                        _gate_blocks.append(f"earnings_blackout: {earn_reason[:80]}")
                        extra_notes.append(earn_reason)
                except Exception as _ee:
                    log.debug(f"{symbol}: earnings check error: {_ee}")

            # Gap-up check
            if action == "BUY":
                try:
                    from ..analysis.position_reviewer import check_gap_up
                    is_gap, gap_pct = check_gap_up(symbol)
                    if is_gap:
                        gap_min_news = float(cfg.get("trading", {}).get("gap", {}).get("min_news_score", 0.40))
                        if news["score"] < gap_min_news:
                            action = "HOLD"
                            _gate_blocks.append(
                                f"gap_up_no_news: {gap_pct:.1%} (news={news['score']:+.2f})"
                            )
                            extra_notes.append(
                                f"gap-up {gap_pct:.1%} without news support "
                                f"(news={news['score']:+.2f}) — skipping"
                            )
                        else:
                            gap_up = True
                            extra_notes.append(
                                f"gap-up {gap_pct:.1%} with news support — dynamic trailing stop"
                            )
                except Exception as _ge:
                    log.debug(f"{symbol}: gap check error: {_ge}")

    # Change #40: Fibonacci proximity gate - re-route to queue if at fib resistance
    _fib_dir = tech.get("details", {}).get("fib_direction", "")
    _fib_d_for_prox = tech.get("details", {}) or {}
    _fib_prox_pct = _fib_d_for_prox.get("fib_proximity_pct")
    try:
        _fib_prox = (
            float(_fib_prox_pct) / 100.0
            if _fib_prox_pct is not None
            else float(_fib_d_for_prox.get("fib_proximity", 1.0))
        )
    except Exception:
        _fib_prox = 1.0
    if action == "BUY" and str(_fib_dir).lower() == "resistance" and _fib_prox < 0.03:
        action = "HOLD"
        reason_fib = "fib resistance within 3pct - routed to queue"
        _gate_blocks.append(f"fib_resistance: {_fib_prox:.2%} within 3pct")
        extra_notes.append(reason_fib)
        if not position:
            fib_d_40     = tech.get("details", {}) or {}
            fib_price_40 = fib_d_40.get("fib_nearest_price")
            fib_ratio_40 = fib_d_40.get("fib_nearest_ratio")
            q_cfg_40     = cfg.get("entry_queue", {}) or {}
            if q_cfg_40.get("enabled", False) and fib_price_40 is not None and fib_price_40 > 0:
                try:
                    _last_40 = float(fib_d_40.get("last", 0) or 0)
                    entry_queue.add_entry(
                        symbol=symbol,
                        entry_type="breakout_resistance",
                        trigger_price=fib_price_40,
                        fib_ratio=float(fib_ratio_40 or 0),
                        fib_direction="resistance",
                        combined_score=combined,
                        price_at_queue=_last_40,
                        deep_size_mult=deep_size_mult,
                    )
                    extra_notes.append(
                        f"QUEUED: waiting for breakout @ {fib_price_40:.2f} "
                        f"(Fib {float(fib_ratio_40 or 0)*100:.1f}%)"
                    )
                except Exception as _fqe:
                    log.debug(f"{symbol}: fib resistance queue add failed: {_fqe}")

    # 8. Position age re-evaluation (5-day check)
    if position and action != "CLOSE":
        try:
            from ..analysis.position_reviewer import check_position_age, llm_position_review
            from ..utils.market_time import today_str
            needs_review, age_days = check_position_age(position, broker)
            last_rev = (position.tags or {}).get("last_review_date", "")
            if needs_review and last_rev != today_str():
                log.info(f"{symbol}: position age {age_days}d — running LLM re-evaluation")
                rev = llm_position_review(
                    symbol, tech, news, regime, position,
                    context=f"Position held {age_days} days without hitting TP or SL.",
                )
                try:
                    broker.set_position_stop(symbol, tags={"last_review_date": today_str()})
                except Exception:
                    pass
                if rev["recommendation"] == "CLOSE" and rev.get("confidence", 0) >= 0.6:
                    action = "CLOSE"
                    extra_notes.append(
                        f"{age_days}d re-eval: CLOSE "
                        f"(conf={rev.get('confidence', 0):.0%}) — {rev['reason']}"
                    )
                else:
                    extra_notes.append(f"{age_days}d re-eval: keep — {rev['reason']}")
        except Exception as _are:
            log.debug(f"{symbol}: age re-eval error: {_are}")

    # 9. Urgent news urgency check (2-hour window)
    try:
        from ..analysis.position_reviewer import urgent_news_signal
        urgency_cfg = cfg.get("trading", {}).get("news_urgency", {}) or {}
        min_flag = float(urgency_cfg.get("min_score_to_flag", -0.5))
        urg = urgent_news_signal(symbol)
        urg_score = float(urg.get("score", 0.0))
        if urg_score < min_flag:
            combined = combined + urg_score * 0.10
            extra_notes.append(f"urgent news ({urg_score:+.2f}): {urg.get('reason', '')[:60]}")
            if position and combined <= t["sell_threshold"] and action != "CLOSE":
                action = "CLOSE"
                extra_notes.append("urgent news tipped score below sell threshold")
    except Exception as _ue:
        log.debug(f"{symbol}: urgency check error: {_ue}")

    # 10a. LLM CLOSE veto on queued entries — cancel without open position
    if llm_action == "CLOSE" and not position and entry_queue.has_entry(symbol):
        entry_queue.remove_entry(symbol, reason="llm_close_cancel")
        extra_notes.append("LLM CLOSE cancelled queued entry")
        log.info(f"{symbol}: LLM CLOSE cancelled queued entry")

    # 10b. Entry queue path — if HOLD but setup is close, queue for S/R trigger
    if action == "HOLD" and not position:
        fib_d     = tech.get("details", {}) or {}
        fib_dir   = fib_d.get("fib_direction")
        fib_price = fib_d.get("fib_nearest_price")
        fib_ratio = fib_d.get("fib_nearest_ratio")
        fib_prox  = fib_d.get("fib_proximity_pct") or 100.0
        q_cfg     = cfg.get("entry_queue", {}) or {}
        score_min = float(q_cfg.get("queue_score_min", 0.28))
        near_pct  = float(q_cfg.get("near_level_pct", 0.05))
        tol_pct   = float(cfg.get("signals", {}).get("technicals", {}).get("fib_tolerance", 0.02)) * 100

        if (
            q_cfg.get("enabled", False)
            and combined >= score_min
            and fib_dir == "support"
            and fib_price is not None
            and fib_prox > tol_pct                    # price not already at the level
            and fib_price > 0
        ):
            try:
                last_price = float(tech.get("details", {}).get("last", 0) or 0)
                if last_price > 0 and (last_price - fib_price) / last_price <= near_pct:
                    entry_queue.add_entry(
                        symbol=symbol,
                        entry_type="bounce_support",
                        trigger_price=fib_price,
                        fib_ratio=float(fib_ratio or 0),
                        fib_direction="support",
                        combined_score=combined,
                        price_at_queue=last_price,
                        deep_size_mult=deep_size_mult,
                    )
                    extra_notes.append(
                        f"QUEUED: waiting for bounce @ {fib_price:.2f} "
                        f"(Fib {float(fib_ratio or 0)*100:.1f}%)"
                    )
            except Exception as _qe:
                log.debug(f"{symbol}: queue add failed: {_qe}")

    reason_parts = [
        llm.get("reason", "") or
        f"tech={tech['score']:+.2f}, news={news['score']:+.2f}, "
        f"breadth={breadth['score']:+.2f}, llm={llm['score']:+.2f} -> {combined:+.2f}",
        f"trend={trend.get('label','?')}",
        f"regime={regime.get('label') or 'neutral'}",
        f"quality={quality['label']}",
    ]
    if filter_note:
        reason_parts.append(filter_note)
    if deep_note:
        reason_parts.append(deep_note)
    reason_parts.extend(extra_notes)

    # 10. Signal disagreement logging — track when signals strongly disagree.
    # Logged to journal; the EOD reflection and signal-weight tuner can read
    # disagreement patterns to understand which signal is actually more reliable.
    _log_signal_disagreement(symbol, tech, llm, action, combined)

    return {
        "symbol": symbol,
        "action": action,
        "combined_score": float(combined),
        "deep_size_mult": deep_size_mult,
        "deep_score": deep_score_val,
        "deep_grade": deep_grade_val,
        "had_position": bool(position),
        "gate_notes": " | ".join(_gate_blocks) if _gate_blocks else "",
        "gap_up": gap_up,
        "reason": " | ".join(p for p in reason_parts if p),
        "trend": {"label": trend.get("label"),
                  "short": trend.get("short", {}).get("label"),
                  "long": trend.get("long", {}).get("label")},
        "regime": {"label": regime.get("label") or "neutral", "score": regime.get("score")},
        "quality": quality,
        "signals": {
            "technicals": tech,
            "news": news,
            "breadth": {k: breadth[k] for k in ("score", "reason")},
            "llm": llm,
        },
    }


# -------------------------------------------------------------- TP lock + ratchet

def _check_tp_lock(broker: Broker, position, last_price: float) -> None:
    """When price reaches the take-profit target, lock in profit by moving the
    stop to the TP price. Alpaca holds the stop from there; the ratchet nudges
    it up each subsequent cycle. Only called when broker_managed_stops=True."""
    tags = dict(getattr(position, "tags", None) or {})
    if tags.get("locked_profit"):
        return
    tp = getattr(position, "take_profit", None)
    if not tp or last_price < float(tp):
        return
    log.info(
        f"{position.symbol}: TP {float(tp):.2f} hit @ {last_price:.2f} — locking profit"
    )
    try:
        broker.set_position_stop(
            position.symbol,
            stop_loss=float(tp),
            tags={**tags, "locked_profit": True, "locked_price": float(tp), "trailing": False},
        )
        append_entry({
            "type": "profit_locked",
            "symbol": position.symbol,
            "tp_price": float(tp),
            "last_price": last_price,
        })
    except Exception as e:
        log.warning(f"{position.symbol}: tp_lock failed: {e}")


def _ratchet_locked_profit_stops(broker: Broker, cycle_label: str) -> None:
    """For profit-locked positions, move the stop 50% toward current price each
    cycle. Ported from backtester engine. Only runs when broker_managed_stops=True."""
    if not getattr(broker, "broker_managed_stops", False):
        return
    _t_cfg = load_config().get("trading", {})
    ratchet_min = float(_t_cfg.get("ratchet_min_move_pct", 0.025))
    ratchet_step = float(_t_cfg.get("ratchet_step_pct", 0.30))
    for p in broker.get_positions():
        tags = dict(getattr(p, "tags", None) or {})
        if not tags.get("locked_profit"):
            continue
        old_stop = getattr(p, "stop_loss", None)
        if not old_stop:
            continue
        try:
            price = float(broker.get_quote(p.symbol).last)
        except Exception:
            continue
        if price <= old_stop:
            continue
        if price < old_stop * (1.0 + ratchet_min):
            continue
        new_stop = old_stop + ratchet_step * (price - old_stop)
        if new_stop <= old_stop * 1.001:
            continue
        try:
            broker.set_position_stop(p.symbol, stop_loss=new_stop, tags=tags)
            log.info(
                f"{p.symbol}: ratchet stop {old_stop:.2f} -> {new_stop:.2f} "
                f"(price={price:.2f})"
            )
            append_entry({
                "cycle": cycle_label,
                "type": "ratchet_stop",
                "symbol": p.symbol,
                "old_stop": old_stop,
                "new_stop": new_stop,
                "price": price,
            })
        except Exception as e:
            log.warning(f"{p.symbol}: ratchet failed: {e}")


# -------------------------------------------------------------- trailing stops

def _update_trailing_stops(broker: Broker, cycle_label: str) -> None:
    """For each open position tagged as "trailing", recompute the trail stop
    and raise it if price has marched up. We never lower the stop.

    Trail width = the percentage captured at entry (entry - initial_stop) /
    entry. So a ticker whose initial candle-low stop was 8% below entry will
    trail by 8% forever; one whose initial stop was 12% will trail by 12%.
    This keeps the trail calibrated to the stock's actual volatility at the
    time of entry — tighter names get a tighter trail, messier ones get more
    room.

    Positions are tagged at entry time (see ``_execute``): small-caps get
    ``small_cap=True`` + ``trailing=True`` + ``trail_pct=<float>`` when
    trailing is enabled. Large-caps keep their initial fixed stop by default.
    """
    cfg = load_config()
    ts_cfg = cfg.get("trading", {}).get("trailing_stop", {}) or {}
    enabled_sc = bool(ts_cfg.get("enabled_for_small_caps", True))
    enabled_lc = bool(ts_cfg.get("enabled_for_large_caps", False))
    if not (enabled_sc or enabled_lc):
        return

    positions = broker.get_positions()
    for p in positions:
        if p.quantity <= 0:
            continue  # skip shorts; trailing logic here is long-only
        tags = getattr(p, "tags", {}) or {}
        # Respect explicit per-position trailing flag if present
        is_small_cap = bool(tags.get("small_cap", False))
        wants_trail = tags.get("trailing")
        if wants_trail is None:
            # Infer from config if not explicitly tagged (older positions)
            wants_trail = enabled_sc if is_small_cap else enabled_lc
        if not wants_trail:
            continue

        current_stop = getattr(p, "stop_loss", None)
        if not current_stop:
            continue  # nothing to trail from

        trail_pct = tags.get("trail_pct")
        if trail_pct is None:
            # Back-fill trail_pct from the entry price + stop if it wasn't
            # recorded (older positions).
            entry_px = tags.get("entry_price") or p.avg_entry
            if entry_px and entry_px > 0:
                trail_pct = max(0.0, (entry_px - float(current_stop)) / entry_px)
            else:
                continue
        trail_pct = float(trail_pct)
        if trail_pct <= 0:
            continue

        try:
            q = broker.get_quote(p.symbol)
            current_price = float(q.last)
        except Exception as e:
            log.debug(f"{p.symbol}: trailing stop quote failed: {e}")
            continue

        try:
            result = compute_trailing_stop(
                current_stop=float(current_stop),
                current_price=current_price,
                trail_pct=trail_pct,
            )
        except Exception as e:
            log.debug(f"{p.symbol}: trailing stop calc failed: {e}")
            continue

        if result.get("raised"):
            new_stop = float(result["new_stop"])
            try:
                broker.set_position_stop(
                    p.symbol,
                    stop_loss=new_stop,
                    take_profit=getattr(p, "take_profit", None),
                    tags=tags,
                )
            except Exception as e:
                log.debug(f"{p.symbol}: set_position_stop (trail) failed: {e}")
                continue
            log.info(
                f"{p.symbol}: trailing stop raised "
                f"{float(current_stop):.2f} -> {new_stop:.2f} "
                f"(price={current_price:.2f}, trail={trail_pct:.2%})"
            )
            append_entry({
                "cycle": cycle_label,
                "type": "trailing_stop_update",
                "symbol": p.symbol,
                "old_stop": float(current_stop),
                "new_stop": new_stop,
                "current_price": current_price,
                "trail_pct": trail_pct,
                "reason": result.get("reason", ""),
            })


# -------------------------------------------------------------- execution

def _resolve_max_positions(t_cfg: dict, regime_label: str) -> int:
    """Return the position cap for the current regime."""
    mp = t_cfg.get("max_positions", {})
    if isinstance(mp, dict):
        return int(mp.get(regime_label, mp.get("neutral", 10)))
    return int(mp)  # backwards-compat if someone passes a plain int


def _execute(broker: Broker, decision: dict, position, account,
             max_positions: int = 10) -> Order | None:
    sym = decision["symbol"]
    action = decision["action"]

    if action == "HOLD":
        return None

    if action == "CLOSE":
        return broker.close_position(sym)

    if action == "BUY":
        cfg = load_config()["trading"]
        # Respect regime-aware max_positions
        current_open = len([p for p in account.positions if p.quantity != 0])
        if current_open >= max_positions and not position:
            log.info(f"{sym}: skipping BUY — already at max_positions ({current_open}/{max_positions})")
            return None

        q = broker.get_quote(sym)
        price = _valid_quote_price(q)
        if price is None:
            log.warning(f"{sym}: BUY skipped - invalid quote price")
            return None
        min_price = float(cfg.get("min_price_for_buy", 0.0))
        if min_price > 0 and price < min_price:
            log.info(f"{sym}: BUY skipped - price ${price:.2f} below min_price_for_buy ${min_price:.2f}")
            return None
        trend = decision.get("trend") or {}
        # compute_size needs the full trend object to decide downtrend haircut.
        # The decision carries only the labels; re-assemble a minimal trend
        # dict from decision.trend.short/long.
        trend_full = {
            "label": trend.get("label"),
            "short": {"label": trend.get("short")},
            "long": {"label": trend.get("long")},
        }

        # Compute the stop FIRST so we can feed its distance into sizing.
        stop_info = compute_dynamic_stop(broker, sym, price)
        tp_price = compute_take_profit(price)

        # Risk-aware + regime-aware sizing: wider stop -> smaller position;
        # bearish/volatile regime -> smaller position again.
        # deep_size_mult further scales down D/C-grade stocks.
        qty, size_details = compute_size(
            account, price,
            trend=trend_full,
            stop_price=stop_info.get("stop"),
            regime=decision.get("regime") or {},
        )
        deep_mult = float(decision.get("deep_size_mult", 1.0))
        if deep_mult < 1.0 and qty > 0:
            import math as _math
            qty = max(1, _math.floor(qty * deep_mult))
        if qty <= 0:
            log.info(f"{sym}: computed size is 0 — {size_details.get('reason','')}")
            return None

        order = Order(
            symbol=sym, side=OrderSide.BUY, quantity=qty,
            stop_loss=stop_info["stop"],
            take_profit=tp_price,
            notes=decision["reason"][:200],
        )
        placed = broker.place_order(order)

        # Persist per-position risk metadata so the next cycle's stop check sees it
        if _has_confirmed_entry_fill(placed):
            entry_px = placed.filled_price or price
            filled_qty = float(placed.quantity or qty)
            # Small-cap detection + trailing-stop tagging
            ts_cfg = cfg.get("trailing_stop", {}) or {}
            sc_threshold = float(ts_cfg.get("small_cap_price_threshold", 15.0))
            is_small_cap = entry_px <= sc_threshold
            trailing_enabled = (
                ts_cfg.get("enabled_for_small_caps", True) if is_small_cap
                else ts_cfg.get("enabled_for_large_caps", False)
            )
            initial_stop = float(stop_info["stop"])
            is_gap_up_trade = bool(decision.get("gap_up", False))
            if not trailing_enabled:
                trail_pct = None
            elif is_small_cap or (is_gap_up_trade and entry_px > 0 and initial_stop < entry_px):
                # Small caps AND gap-up trades: dynamic trail = initial stop distance as % of entry
                trail_pct = (entry_px - initial_stop) / entry_px if (entry_px > 0 and initial_stop < entry_px) else float(ts_cfg.get("large_cap_trail_pct", 0.10))
            else:
                # Large caps (non-gap): flat configurable trailing stop (default 10%)
                trail_pct = float(ts_cfg.get("large_cap_trail_pct", 0.10))
            stop_synced = False
            try:
                tags = {
                    "quality": decision.get("quality", {}).get("label", "normal"),
                    "trend": trend.get("label", "unknown"),
                    "regime": decision.get("regime", {}).get("label", "neutral"),
                    "sizing_mode": size_details.get("sizing_mode", "normal"),
                    "entry_price": entry_px,
                    "entry_datetime": datetime.utcnow().isoformat(),
                    "stop_reason": stop_info.get("reason", ""),
                    "small_cap": is_small_cap,
                    "gap_up": is_gap_up_trade,
                    "trailing": bool(trailing_enabled),
                }
                if trail_pct is not None:
                    tags["trail_pct"] = float(trail_pct)
                broker.set_position_stop(
                    sym,
                    stop_loss=stop_info["stop"],
                    take_profit=tp_price,
                    tags=tags,
                )
                stop_synced = True
            except Exception as e:
                log.warning(f"{sym}: live stop placement failed after BUY; position may be unprotected: {e}")

            # $ at risk = (entry - stop) * qty — useful to print & journal
            dollar_risk = (entry_px - stop_info["stop"]) * filled_qty
            trail_note = (
                f", trailing {trail_pct:.2%}"
                if (trailing_enabled and trail_pct is not None) else ""
            )
            stop_status = (
                "live" if getattr(broker, "broker_managed_stops", False) else "stored"
            ) if stop_synced else "UNPROTECTED"
            log.info(
                f"{sym}: BUY {filled_qty:g} @ {entry_px:.2f} | "
                f"stop={stop_info['stop']:.2f} ({stop_info['source']}, "
                f"{stop_info.get('pct',0):.2%}{trail_note}, {stop_status}) | "
                f"tp={tp_price:.2f} | quality={decision.get('quality',{}).get('label')} | "
                f"sizing={size_details.get('sizing_mode')} "
                f"(binding={size_details.get('binding_constraint')}) | "
                f"$risk={dollar_risk:.0f}"
            )
        return placed

    return None


def _place_queued_buy(broker: Broker, symbol: str, tags: dict) -> Order | None:
    """Execute a BUY for a queue-triggered entry.

    Called by the 5-minute monitor after bounce/breakout confirmation and
    a passing fast re-score. Mirrors _execute(BUY) but skips the full
    decide_for_ticker() overhead — signal quality was already checked.
    """
    sym = symbol.upper()
    cfg = load_config()["trading"]
    try:
        account = broker.get_account()
        positions = {p.symbol: p for p in account.positions}
        if sym in positions:
            log.info(f"[entry_queue] {sym}: already have position — skipping queued buy")
            return
        regime_label = (
            str(tags.get("regime") or tags.get("entry_regime") or "neutral").lower()
        )
        max_pos = _resolve_max_positions(cfg, regime_label)
        current_open = sum(1 for p in account.positions if p.quantity != 0)
        if current_open >= max_pos:
            log.info(f"[entry_queue] {sym}: at max_positions ({current_open}) — skipping")
            return

        q = broker.get_quote(sym)
        price = _valid_quote_price(q)
        if price is None:
            log.warning(f"[entry_queue] {sym}: invalid quote price - skipping queued buy")
            return
        stop_info = compute_dynamic_stop(broker, sym, price)
        tp_price  = compute_take_profit(price)
        qty, size_details = compute_size(
            account, price,
            trend={"label": "unknown", "short": {"label": None}, "long": {"label": None}},
            stop_price=stop_info.get("stop"),
            regime={},
        )
        deep_mult = float(tags.get("deep_size_mult", 1.0) or 1.0)
        if deep_mult < 1.0 and qty > 0:
            qty = max(1, math.floor(qty * deep_mult))
        if qty <= 0:
            log.info(f"[entry_queue] {sym}: computed size 0 — skipping")
            return

        order = Order(
            symbol=sym, side=OrderSide.BUY, quantity=qty,
            stop_loss=stop_info["stop"],
            take_profit=tp_price,
            notes=f"queue-trigger: {tags.get('entry_type','bounce')} @ {tags.get('trigger_price',0):.2f}",
        )
        placed = broker.place_order(order)
        if _has_confirmed_entry_fill(placed):
            entry_px = placed.filled_price or price
            filled_qty = float(placed.quantity or qty)
            ts_cfg   = cfg.get("trailing_stop", {}) or {}
            trail_pct = float(ts_cfg.get("large_cap_trail_pct", 0.10))
            full_tags = {
                **tags,
                "entry_price":    entry_px,
                "entry_datetime": datetime.utcnow().isoformat(),
                "stop_reason":    stop_info.get("reason", ""),
                "sizing_mode":    size_details.get("sizing_mode", "normal"),
                "trailing":       True,
                "trail_pct":      trail_pct,
            }
            broker.set_position_stop(
                sym, stop_loss=stop_info["stop"], take_profit=tp_price, tags=full_tags
            )
            log.info(
                f"[entry_queue] {sym}: BUY {filled_qty:g} @ {entry_px:.2f} "
                f"(queued trigger: {tags.get('entry_type')}, "
                f"fib {tags.get('fib_ratio', 0)*100:.1f}%)"
            )
        return placed
    except Exception as e:
        log.warning(f"[entry_queue] _place_queued_buy {sym}: {e}")
    return None


def _log_signal_disagreement(
    symbol: str, tech: dict, llm: dict, action: str, combined: float
) -> None:
    """Log strong signal disagreements to the journal for retrospective scoring.

    A disagreement is when technicals and LLM point in opposite directions
    with meaningful magnitude (tech score > 0.4 bullish, LLM score < -0.1, etc.).
    These rows feed the signal-weight tuner and EOD reflection.
    """
    try:
        tech_score = float(tech.get("score") or 0)
        llm_score = float(llm.get("score") or 0)
        llm_action = str(llm.get("action") or "HOLD").upper()
        # Only log meaningful disagreements — both sides must have some conviction
        threshold = 0.35
        if abs(tech_score) < threshold or abs(llm_score) < threshold:
            return
        disagree = (tech_score > 0 and llm_score < 0) or (tech_score < 0 and llm_score > 0)
        if not disagree:
            return
        log.debug(
            f"{symbol}: signal disagreement — tech={tech_score:+.2f} vs llm={llm_score:+.2f} "
            f"-> action={action}"
        )
        append_entry({
            "type": "signal_disagreement",
            "symbol": symbol,
            "tech_score": round(tech_score, 3),
            "llm_score": round(llm_score, 3),
            "llm_action": llm_action,
            "final_action": action,
            "combined_score": round(float(combined), 3),
            "tech_led": abs(tech_score) > abs(llm_score),
        })
    except Exception:
        pass


def _fire_postmortem(sym: str, decision: dict, position, executed) -> None:
    """Fire a per-trade post-mortem and close the setup fingerprint. Never raises."""
    close_price = float(getattr(executed, "filled_price", 0) or 0)
    tags = getattr(position, "tags", {}) or {} if position else {}
    entry_price = float(tags.get("entry_price") or getattr(position, "avg_entry", 0) or 0)
    entry_dt_str = tags.get("entry_datetime", "")
    close_reason = str(decision.get("reason") or "signal")[:200]

    # Mark this ticker as closed today so the BUY gate can block same-day re-entry
    try:
        from ..analysis.position_reviewer import record_today_close
        record_today_close(sym)
    except Exception:
        pass

    # Close the setup fingerprint
    try:
        from ..learning.setup_memory import record_close_outcome
        record_close_outcome(
            symbol=sym,
            close_price=close_price,
            close_reason=close_reason,
            entry_price=entry_price,
            entry_datetime_str=entry_dt_str,
        )
    except Exception as e:
        log.debug(f"[setup-memory] {sym}: close failed: {e}")

    # Per-trade post-mortem
    try:
        from ..learning.postmortem import run_trade_postmortem
        from ..learning.journal import load_today_journal
        entry_journal = None
        for e in load_today_journal():
            d = e.get("decision") or {}
            if d.get("symbol") == sym and d.get("action") == "BUY":
                entry_journal = e
        run_trade_postmortem(
            symbol=sym,
            close_reason=close_reason,
            close_price=close_price,
            position=position,
            entry_journal_entry=entry_journal,
        )
    except Exception as e:
        log.debug(f"[postmortem] {sym}: {e}")


def _update_session_context(broker, cycle_label: str) -> None:
    """Write today's session summary to today_context.md. Never raises."""
    try:
        from ..learning.session_context import update_session_context
        update_session_context(broker, cycle_label)
    except Exception as e:
        log.debug(f"[session-context] {cycle_label}: {e}")


def _order_dict(o) -> dict:
    if o is None:
        return None
    return {
        "id": o.order_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "qty": o.quantity,
        "status": o.status,
        "filled_price": o.filled_price,
        "stop_loss": o.stop_loss,
        "take_profit": o.take_profit,
        "notes": o.notes,
    }
