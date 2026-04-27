"""Walk-forward backtest engine.

Iterates over the last N trading days in chronological order. For each day:
  1. If it's Monday (or the first day): run the deep scorer with as_of_date
     sliced to that date — simulates the Sunday evening pre-run.
  2. Three decision cycles: 09:30, 12:00, 14:00.
  3. End-of-day stop/TP check using the daily bar's high/low.
  4. Snapshot equity for the curve.

Decisions are made using the same logic as `decide_for_ticker` in
`decision_engine.py` but calling backtest-aware signal wrappers so that
no future data leaks in.

Usage (CLI):
  python -m src.backtester                            # 90 days, full shortlist
  python -m src.backtester --days 20 BBAI NVDA TSLA  # 20 days, specific tickers
  python -m src.backtester --no-llm                  # skip LLM (faster)
  python -m src.backtester --no-deep                 # skip deep scorer
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger
from .broker import BacktestBroker
from .data_cache import DataCache
from .deep_score_cache import DeepScoreCache
from .entry_queue import BacktestEntryQueue
from .signals import (
    backtest_breadth,
    backtest_llm_signal,
    backtest_news_signal,
    backtest_regime,
    backtest_trend,
    prefetch_alpaca_news_bulk,
)

log = get_logger(__name__)

# Backtest learning artifacts — kept separate from live files so the user can
# review and selectively promote entries to the live databases after a run.
_BT_FINGERPRINTS = "data/backtest_fingerprints.jsonl"
_BT_POSTMORTEMS  = "data/backtest_postmortems.jsonl"
_BT_LESSONS      = "data/backtest_lessons.md"

_CYCLE_TIMES = [
    ("09:30", False),
    ("11:30", False),
    ("12:30", False),
    ("13:30", False),
    ("14:30", False),
    ("15:30", False),   # last cycle — flatten_on_weak_close fires here
]


def _news_cache_key(symbol: str, as_of) -> tuple[str, str]:
    """Keep daily planning news separate from intraday cycle news."""
    if isinstance(as_of, datetime):
        return (symbol.upper(), as_of.isoformat(timespec="minutes"))
    return (symbol.upper(), str(as_of)[:10])


def _fifth_trading_day_after(day: date, all_days: list[date]) -> date | None:
    try:
        idx = all_days.index(day)
    except ValueError:
        return None
    target_idx = idx + 5
    return all_days[target_idx] if target_idx < len(all_days) else None


def run_backtest(
    symbols: list[str],
    days: int = 90,
    starting_cash: float = 100_000.0,
    use_deep_scorer: bool = True,
    use_llm: bool = True,
    verbose: bool = True,
    skip_days: int = 0,
    end_date: "date | None" = None,
    run_id: str | None = None,
) -> dict:
    """Run a walk-forward backtest and return results dict.

    Returns:
      trades         — list of completed trade dicts
      equity_curve   — list of {date, equity, cash, positions} snapshots
      cycle_log      — list of all BUY/CLOSE/stop actions taken
      deep_score_runs— list of dates when the deep scorer was run
      starting_cash  — float
      all_fills      — list of every fill (audit trail)
    """
    cfg = load_config()
    run_id = run_id or ""
    newsapi_key = cfg.get("secrets", {}).get("newsapi_key", "") or ""
    weights = cfg["signals"]["weights"]
    t_cfg = cfg["trading"]

    # Load learned signal weights from weekly tuner if available
    weights_path = Path("data/signal_weights.json")
    if weights_path.exists():
        try:
            learned = json.loads(weights_path.read_text(encoding="utf-8"))
            weights = {**weights, **learned}
            log.info(f"[backtest] loaded learned signal weights: {weights}")
        except Exception:
            pass

    # Renormalize weights when signals are disabled so remaining weights sum to 1.0
    if not use_llm:
        original_llm_weight = float(weights.get("llm", 0.35))
        active = {k: v for k, v in weights.items() if k != "llm"}
        total = sum(active.values())
        if total > 0:
            weights = {k: round(v / total, 4) for k, v in active.items()}
            weights["llm"] = 0.0
        # Scale buy_threshold down proportionally — without LLM the combined score
        # is structurally capped lower because LLM was the only signal that could
        # vote strongly positive against bearish breadth. Multiply threshold by
        # (1 - llm_weight) so a good tech signal still clears the bar.
        t_cfg = dict(t_cfg)
        scaled = round(float(t_cfg.get("buy_threshold", 0.35)) * (1 - original_llm_weight), 4)
        t_cfg["buy_threshold"] = max(0.15, scaled)
        log.info(
            f"[backtest] --no-llm: LLM weight redistributed to remaining signals: "
            f"tech={weights.get('technicals', 0):.3f} "
            f"news={weights.get('news', 0):.3f} "
            f"breadth={weights.get('breadth', 0):.3f} | "
            f"buy_threshold -> {t_cfg['buy_threshold']:.4f}"
        )

    # ------------------------------------------------------------------ setup
    # Calculate the minimum data window needed for this backtest.
    # Daily: test window + 120 calendar days for indicator warmup (SMA50, MACD, ATR),
    #        minimum 365 so regime/breadth calculations have enough history.
    # Intraday: test window only + 10 days buffer (no multi-day indicators on 15-min).
    import math as _math
    _cal_days = _math.ceil(days * 1.5)  # trading days → calendar days (accounts for weekends/holidays)
    _daily_days = max(_cal_days + 120, 365)
    _intraday_days = _cal_days + 10
    log.info(
        f"[backtest] data window: {_daily_days}d daily, {_intraday_days}d intraday "
        f"(for {days}-day backtest)"
    )
    cache = DataCache(symbols)
    log.info(f"[backtest] pre-fetching data for {len(symbols)} symbols...")
    cache.fetch_all(daily_days=_daily_days, intraday_days=_intraday_days)

    # Determine trading days
    if end_date is None:
        end_date = date.today() - timedelta(days=1)  # yesterday is last complete day
    search_start = end_date - timedelta(days=int(days * 1.8))  # cast wide for holidays
    all_days = cache.trading_days(search_start, end_date)
    trading_days = all_days[-days:]

    if not trading_days:
        log.error("[backtest] no trading days found in cached data")
        return {"error": "no trading days in cache"}

    if skip_days:
        if skip_days >= len(trading_days):
            log.error(f"[backtest] skip_days={skip_days} >= total days={len(trading_days)}")
            return {"error": "skip_days exceeds available trading days"}
        log.info(f"[backtest] resuming from day {skip_days + 1} ({trading_days[skip_days]})")
        trading_days = trading_days[skip_days:]

    log.info(
        f"[backtest] {len(trading_days)} trading days: "
        f"{trading_days[0]} to {trading_days[-1]}"
    )

    # Bulk-prefetch Alpaca news for the entire simulation window up front.
    # This replaces ~200 per-day API calls with a single paginated batch fetch,
    # eliminating the biggest per-day bottleneck. On failure it logs a warning
    # and the per-day fallback in _alpaca_news_fetch_day() takes over automatically.
    try:
        log.info(f"[backtest] bulk-fetching news for {len(symbols)} symbols...")
        _news_ok = prefetch_alpaca_news_bulk(
            symbols=symbols,
            start_date=trading_days[0],
            end_date=trading_days[-1],
        )
        if _news_ok:
            log.info("[backtest] news bulk pre-fetch complete — per-day API calls eliminated")
        else:
            log.warning("[backtest] news bulk pre-fetch returned nothing — falling back to per-day fetching")
    except Exception as _npe:
        log.warning(f"[backtest] news bulk pre-fetch failed ({_npe}) — falling back to per-day fetching")

    broker = BacktestBroker(cache, starting_cash=starting_cash)
    deep_scores: dict[str, dict] = {}
    deep_score_dates: dict[str, date] = {}   # sim_date each symbol was last scored
    deep_score_runs: list[str] = []
    equity_curve: list[dict] = []
    cycle_log: list[dict] = []
    decisions_log: list[dict] = []
    max_stale = int(cfg.get("deep_score", {}).get("backtest_stale_days", 14))
    # Persistent deep score cache — avoids re-running the scorer on every backtest
    ds_cache = DeepScoreCache()
    # ETFs/indices never produce deep scores — skip them permanently so they
    # never trigger a live score attempt even after the in-memory stale timeout.
    _brd_cfg = cfg.get("signals", {}).get("breadth", {})
    _perm_skip: set[str] = (
        {str(s).upper() for s in _brd_cfg.get("index_symbols", [])}
        | {str(s).upper() for s in _brd_cfg.get("sectors", [])}
    )
    # Per-day news cache: keyed (symbol_upper, date_str) — reset each morning so
    # _cull_symbols, _rank_symbols, and every _decide cycle share one lookup.
    _news_cache: dict = {}
    # Postmortem writes buffered here during cycles, flushed once at end of day.
    _postmortem_queue: list[dict] = []
    bt_entry_queue = BacktestEntryQueue(cfg.get("entry_queue", {}) or {})

    # ------------------------------------------------------------------ loop
    for i, sim_date in enumerate(trading_days):
        day_num = skip_days + i + 1
        total_days = skip_days + len(trading_days)
        if verbose:
            log.info(f"[backtest] === {sim_date} (day {day_num}/{total_days}) ===")

        # --- Deep scorer: daily pass, re-score any stale or unscored tickers ---
        # Cache-first: pull from deep_score_cache.json when available, only run
        # the live scorer for symbols with no valid cached entry (gap > 31 days).
        if use_deep_scorer:
            stale = [
                sym for sym in symbols
                if sym.upper() not in _perm_skip
                and (
                    sym.upper() not in deep_scores
                    or (sim_date - deep_score_dates.get(sym.upper(), date.min)).days > max_stale
                )
            ]
            if stale:
                # Split into cache hits vs symbols that genuinely need scoring
                from_cache, need_scoring = [], []
                for sym in stale:
                    cached = ds_cache.get(sym, sim_date)
                    if cached is not None:
                        deep_scores[sym.upper()] = cached
                        deep_score_dates[sym.upper()] = sim_date
                        from_cache.append(sym)
                    else:
                        _last = deep_score_dates.get(sym.upper())
                        _mem_status = (
                            f"stale in-memory ({(sim_date - _last).days}d since {_last})"
                            if _last else "never scored in-memory"
                        )
                        log.info(
                            f"[deep_score] LIVE SCORE TRIGGERED: {sym} | "
                            f"{_mem_status} | disk cache: {ds_cache.miss_reason(sym, sim_date)}"
                        )
                        need_scoring.append(sym)

                if from_cache:
                    log.info(
                        f"[backtest] {sim_date}: {len(from_cache)} deep scores from disk cache"
                    )

                if need_scoring:
                    log.info(
                        f"[backtest] deep scoring {len(need_scoring)} tickers "
                        f"(no cache) as of {sim_date}..."
                    )
                    try:
                        ds_results = _run_deep_score(need_scoring, cache, sim_date)
                        deep_scores.update(ds_results)
                        for sym in ds_results:
                            deep_score_dates[sym.upper()] = sim_date
                            ds_cache.put(sym, sim_date, ds_results[sym])
                            # Permanently skip any symbol that can never produce a score
                            _err = ds_results[sym].get("error", "")
                            if isinstance(_err, str) and _err.startswith("skipped:"):
                                _perm_skip.add(sym.upper())
                                log.info(f"[deep_score] {sym}: added to permanent skip ({_err})")
                        ds_cache.save()
                        deep_score_runs.append(str(sim_date))
                        log.info(
                            f"[backtest] deep scored {len(ds_results)} tickers: "
                            + ", ".join(
                                f"{s}={r.get('score', '?'):.0f}{r.get('grade', '')}"
                                for s, r in list(ds_results.items())[:5]
                            )
                        )
                    except Exception as e:
                        log.warning(f"[backtest] deep scorer failed for {sim_date}: {e}")
                elif from_cache:
                    deep_score_runs.append(str(sim_date))

        # Reset per-day caches
        _news_cache.clear()
        _tech_cache: dict = {}  # tech results shared between cull and rank (same _BProxy/daily data)

        # Daily cull: drop symbols the live bot would filter out at pre-market
        active_symbols = _cull_symbols(symbols, cache, deep_scores, sim_date, newsapi_key, t_cfg,
                                       news_cache=_news_cache, tech_cache=_tech_cache)

        # Trade-plan ranking: mirrors live bot's 09:00 step — rank by composite and cap at
        # top_n so we only run signal calls (and Finnhub news) on the strongest candidates.
        plan_top_n = int(t_cfg.get("plan_top_n", 20))
        active_symbols = _rank_symbols(
            active_symbols, cache, deep_scores, sim_date, newsapi_key, plan_top_n,
            news_cache=_news_cache, tech_cache=_tech_cache,
        )

        # Breadth and regime use only daily SPY/QQQ/IWM bars — identical for all 6 cycles.
        breadth = backtest_breadth(cache, sim_date)
        regime = backtest_regime(cache, sim_date)

        # Record start-of-day equity for circuit breaker
        start_of_day_equity = broker.get_account().equity
        _closed_today: set = set()  # symbols closed by signal this day (same-day re-entry block)
        _cb_tightened = False       # circuit breaker stop-tightening fires once per day

        # --- Six decision cycles (mirrors live bot: 09:30, 11:30, 12:30, 13:30, 14:30, 15:30) ---
        for cycle_idx, (cycle_label, skip_llm) in enumerate(_CYCLE_TIMES):
            h, m = map(int, cycle_label.split(":"))
            sim_dt = datetime.combine(sim_date, datetime.min.time()).replace(hour=h, minute=m)
            broker.set_sim_dt(sim_dt)

            # Circuit breaker: block BUYs if down more than threshold from day start
            cfg_cb = t_cfg.get("circuit_breaker", {})
            cb_threshold = float(cfg_cb.get("daily_loss_pct", 0.04))
            current_equity = broker.get_account().equity
            loss_pct = (
                (start_of_day_equity - current_equity) / start_of_day_equity
                if start_of_day_equity > 0 else 0.0
            )
            circuit_broken = loss_pct >= cb_threshold
            if circuit_broken:
                log.info(
                    f"[backtest] {sim_date} {cycle_label}: circuit breaker "
                    f"({loss_pct:.1%} down from day start)"
                )
                if not _cb_tightened:
                    try:
                        from ..analysis.position_reviewer import tighten_all_stops
                        tighten_all_stops(broker)
                        _cb_tightened = True
                    except Exception:
                        pass

            log.debug(
                f"[backtest] {sim_date} {cycle_label} | "
                f"breadth={breadth['score']:+.2f} regime={regime['label']}"
            )

            # Weak close (last cycle only): reduce each position by 50% instead of
            # closing entirely — keeps some exposure while cutting risk.
            if (
                cycle_label == "15:30"
                and t_cfg.get("flatten_on_weak_close", False)
                and (breadth["score"] <= -0.6 or regime["label"] in ("bearish", "volatile"))
            ):
                from ..broker.base import Order, OrderSide
                for pos in broker.get_positions():
                    trim_qty = math.floor(pos.quantity / 2)
                    if trim_qty <= 0:
                        continue
                    broker.place_order(Order(
                        symbol=pos.symbol,
                        side=OrderSide.SELL,
                        quantity=trim_qty,
                        notes="weak_close_trim_50pct",
                    ))
                    cycle_log.append({
                        "date": str(sim_date), "cycle": cycle_label,
                        "symbol": pos.symbol, "action": "reduce_half",
                        "qty": trim_qty,
                        "reason": f"regime={regime['label']},breadth={breadth['score']:+.2f}",
                    })
                    log.info(
                        f"[backtest] {sim_date}: weak close — trimmed {pos.symbol} "
                        f"by {trim_qty} shares (was {pos.quantity})"
                    )

            # Gather decisions for every symbol (culled active list for this day)
            held = {p.symbol: p for p in broker.get_positions()}
            _update_trailing_stops(broker, held, sim_date)
            if cycle_label == "15:30":
                _ratchet_locked_profit_stops(broker)
            # Intraday TP lock — mirrors live _check_tp_lock():
            # If cycle price >= take_profit, lock: move stop to TP, clear TP, tag locked.
            # Prevents should_flatten_for_risk from closing the position at TP intraday.
            for _sym, _pos in list(held.items()):
                _raw = broker._positions.get(_sym)
                if not _raw:
                    continue
                _tp = _raw.get("take_profit")
                if not _tp:
                    continue
                try:
                    _current = broker.get_quote(_sym).last
                    if _current >= float(_tp):
                        _etags = dict(_raw.get("tags") or {})
                        _raw["stop_loss"] = float(_tp)
                        _raw["take_profit"] = None
                        _etags.update({"locked_profit": True, "locked_at": float(_tp), "trailing": False})
                        _raw["tags"] = _etags
                        log.info(
                            f"[backtest] {sim_date} {cycle_label}: "
                            f"TP lock {_sym} @ ${_tp:.2f} — stop raised, letting winner run"
                        )
                except Exception:
                    pass
            decisions: dict[str, dict] = {}
            # Also include any held symbols even if culled today (need to manage exits)
            decision_symbols = list(dict.fromkeys(active_symbols + list(held.keys())))
            for sym in decision_symbols:
                try:
                    decisions[sym] = _decide(
                        broker=broker,
                        symbol=sym,
                        breadth=breadth,
                        regime=regime,
                        position=held.get(sym),
                        deep_scores=deep_scores,
                        newsapi_key=newsapi_key,
                        sim_date=sim_date,
                        weights=weights,
                        t_cfg=t_cfg,
                        use_llm=use_llm and not skip_llm,
                        deep_score_entry=deep_scores.get(sym.upper()),
                        circuit_broken=circuit_broken,
                        fingerprints_file=_BT_FINGERPRINTS,
                        cycle_dt=sim_dt,
                        news_cache=_news_cache,
                        closed_today=_closed_today,
                        bt_queue=bt_entry_queue,
                        queue_cfg=cfg.get("entry_queue", {}) or {},
                    )
                except Exception as e:
                    log.debug(f"[backtest] {sym} decide failed: {e}")

            # Log every decision (BUY, HOLD, CLOSE) with full signal details
            _buy_threshold_val = float(t_cfg.get("buy_threshold", 0.35))
            for _sym, _dec in decisions.items():
                try:
                    _tech_sig = (_dec.get("signals") or {}).get("technicals") or {}
                    _d = _tech_sig.get("details") or {}
                    _news_sig = (_dec.get("signals") or {}).get("news") or {}
                    _llm_sig = (_dec.get("signals") or {}).get("llm") or {}
                    _brd_sig = (_dec.get("signals") or {}).get("breadth") or {}
                    try:
                        _cur_px = broker.get_quote(_sym).last
                    except Exception:
                        _cur_px = None
                    decisions_log.append({
                        "date": str(sim_date), "cycle": cycle_label, "symbol": _sym,
                        "action": _dec.get("action"),
                        "combined": _dec.get("combined_score"),
                        "buy_threshold": _buy_threshold_val,
                        "tech_score": round(float(_tech_sig.get("score", 0)), 4),
                        "news_score": round(float(_news_sig.get("score", 0)), 4),
                        "breadth_score": round(float(_brd_sig.get("score", 0)), 4),
                        "llm_score": round(float(_llm_sig.get("score", 0)), 4),
                        # Technical sub-scores
                        "rsi": _d.get("rsi"),
                        "rsi_score": _d.get("rsi_score"),
                        "macd_hist": _d.get("macd_hist"),
                        "macd_score": _d.get("macd_score"),
                        "adx": _d.get("adx"),
                        "trend_score": _d.get("trend_score"),
                        "bb_pct_b": _d.get("bb_pct_b"),
                        "bb_score": _d.get("bb_score"),
                        "bb_squeeze": _d.get("bb_squeeze"),
                        "obv_score": _d.get("obv_score"),
                        "vwap_score": _d.get("vwap_score"),
                        "vwap_distance_pct": _d.get("vwap_distance_pct"),
                        "fib_score": _d.get("fib_score"),
                        "fib_ratio": _d.get("fib_nearest_ratio"),
                        "fib_proximity_pct": _d.get("fib_proximity_pct"),
                        "fib_direction": _d.get("fib_direction"),
                        # Context
                        "regime": (_dec.get("regime") or {}).get("label"),
                        "regime_score": (_dec.get("regime") or {}).get("score"),
                        "trend": (_dec.get("trend") or {}).get("label"),
                        "breadth_reason": str(_brd_sig.get("reason", ""))[:150],
                        # LLM
                        "llm_action": _llm_sig.get("action"),
                        "llm_confidence": _llm_sig.get("confidence"),
                        "llm_reason": str(_llm_sig.get("reason", ""))[:250],
                        # Quality / deep score
                        "quality": (_dec.get("quality") or {}).get("label"),
                        "deep_score": (deep_scores.get(_sym.upper()) or {}).get("score"),
                        "deep_grade": (deep_scores.get(_sym.upper()) or {}).get("grade"),
                        "gate_notes": _dec.get("gate_notes", ""),
                        # Position context
                        "had_position": bool(held.get(_sym)),
                        "current_price": round(float(_cur_px), 4) if _cur_px else None,
                        # Filled in at BUY execution
                        "fill_price": None,
                        "qty": None,
                        # Enriched post-hoc
                        "fwd_5d_return": None,
                    })
                except Exception:
                    pass

            # Phase 2a: execute CLOSEs first
            for sym, dec in decisions.items():
                if dec["action"] != "CLOSE":
                    continue
                try:
                    pos = held.get(sym)
                    close_reason = dec.get("reason", "signal")[:200]
                    close_price = 0.0
                    _is_stop = "stop" in close_reason.lower()
                    _raw_pos = broker._positions.get(sym)
                    if _is_stop and _raw_pos and _raw_pos.get("stop_loss"):
                        # Gap-aware stop fill: open price if gapped, stop price if intraday
                        # record_stop is handled inside _force_close
                        close_price = broker.close_position_stop(sym)
                    else:
                        try:
                            close_price = broker.get_quote(sym).last
                        except Exception:
                            pass
                        broker.close_position(sym)
                        if _is_stop:
                            broker.record_stop(sym)
                        _closed_today.add(sym.upper())
                    cycle_log.append({
                        "date": str(sim_date), "cycle": cycle_label,
                        "symbol": sym, "action": "CLOSE",
                        "reason": close_reason[:120],
                    })
                    # Setup fingerprint + post-mortem
                    if pos:
                        try:
                            from ..learning.setup_memory import record_close_outcome
                            record_close_outcome(
                                sym, close_price, close_reason,
                                float(pos.avg_entry), "",
                                as_of_date=sim_date, db_file=_BT_FINGERPRINTS,
                            )
                        except Exception as _fe:
                            log.debug(f"[backtest] fingerprint close {sym}: {_fe}")
                        _postmortem_queue.append({
                            "sym": sym, "close_reason": close_reason,
                            "close_price": close_price, "pos": pos,
                            "sim_date": sim_date, "use_llm": use_llm,
                        })
                except Exception as e:
                    log.debug(f"[backtest] {sym} CLOSE failed: {e}")

            # Phase 2b: BUYs in quality-priority order
            account = broker.get_account()
            held = {p.symbol: p for p in account.positions}

            from ..trading.decision_engine import sort_buys_by_quality, _resolve_max_positions
            max_pos = _resolve_max_positions(t_cfg, regime.get("label") or "neutral")
            buys = sort_buys_by_quality(
                [(s, d) for s, d in decisions.items() if d["action"] == "BUY"]
            )
            for sym, dec in buys:
                open_count = len([p for p in account.positions if p.quantity != 0])
                if open_count >= max_pos and sym not in held:
                    break
                try:
                    placed = _execute_buy(broker, sym, dec, account, t_cfg)
                    if placed and placed.status == "filled":
                        cycle_log.append({
                            "date": str(sim_date), "cycle": cycle_label,
                            "symbol": sym, "action": "BUY",
                            "qty": placed.quantity,
                            "price": placed.filled_price,
                            "combined": dec.get("combined_score"),
                            "tech_score": round(float((dec.get("signals") or {}).get("technicals", {}).get("score", 0)), 3),
                            "news_score": round(float((dec.get("signals") or {}).get("news", {}).get("score", 0)), 3),
                            "llm_score": round(float((dec.get("signals") or {}).get("llm", {}).get("score", 0)), 3),
                            "quality": (dec.get("quality") or {}).get("label"),
                            "regime": (dec.get("regime") or {}).get("label"),
                            "reason": dec.get("reason", "")[:200],
                        })
                        # Back-fill fill details into the matching decisions_log entry
                        for _r in reversed(decisions_log):
                            if (_r["symbol"] == sym and _r["date"] == str(sim_date)
                                    and _r["cycle"] == cycle_label):
                                _r["fill_price"] = placed.filled_price
                                _r["qty"] = placed.quantity
                                break
                        # Setup fingerprint: record entry conditions for pattern memory
                        try:
                            from ..learning.setup_memory import record_entry_fingerprint
                            record_entry_fingerprint(
                                sym, cycle_label, dec, placed.filled_price,
                                as_of_date=sim_date, db_file=_BT_FINGERPRINTS,
                            )
                        except Exception as _fe:
                            log.debug(f"[backtest] fingerprint entry {sym}: {_fe}")
                        account = broker.get_account()
                        held = {p.symbol: p for p in account.positions}
                except Exception as e:
                    log.debug(f"[backtest] {sym} BUY exec failed: {e}")

            next_dt = (
                datetime.combine(sim_date, datetime.min.time()).replace(
                    hour=int(_CYCLE_TIMES[cycle_idx + 1][0].split(":")[0]),
                    minute=int(_CYCLE_TIMES[cycle_idx + 1][0].split(":")[1]),
                )
                if cycle_idx + 1 < len(_CYCLE_TIMES)
                else datetime.combine(sim_date, datetime.min.time()).replace(hour=15, minute=55)
            )
            _run_entry_queue_monitor(
                broker=broker,
                bt_queue=bt_entry_queue,
                start_dt=sim_dt,
                end_dt=next_dt,
                breadth=breadth,
                regime=regime,
                newsapi_key=newsapi_key,
                weights=weights,
                t_cfg=t_cfg,
                queue_cfg=cfg.get("entry_queue", {}) or {},
                use_llm=use_llm,
                cycle_log=cycle_log,
                decisions_log=decisions_log,
                sim_date=sim_date,
                news_cache=_news_cache,
                fingerprints_file=_BT_FINGERPRINTS,
            )

        # --- End-of-day stop/TP check ---
        eod_dt = datetime.combine(sim_date, datetime.min.time()).replace(hour=15, minute=55)
        broker.set_sim_dt(eod_dt)
        # Capture positions before check_stops removes them
        pre_stop_positions = {p.symbol: p for p in broker.get_positions()}
        triggered = broker.check_stops()
        for ev in triggered:
            cycle_log.append({
                "date": str(sim_date), "cycle": "EOD",
                "symbol": ev["symbol"], "action": ev["type"],
                "price": ev["price"],
            })
            if ev["type"] == "tp_lock":
                log.info(
                    f"[backtest] {sim_date}: TP lock {ev['symbol']} — "
                    f"stop moved to ${ev['price']:.2f}, letting winner run"
                )
                continue  # position still open — skip postmortem
            _closed_today.add(ev["symbol"].upper())  # track EOD stop closes
            # Setup fingerprint + post-mortem for actual closes (stop_loss, locked_profit_stop)
            _stop_pos = pre_stop_positions.get(ev["symbol"])
            if _stop_pos:
                try:
                    from ..learning.setup_memory import record_close_outcome
                    record_close_outcome(
                        ev["symbol"], float(ev["price"]), ev["type"],
                        float(_stop_pos.avg_entry), "",
                        as_of_date=sim_date, db_file=_BT_FINGERPRINTS,
                    )
                except Exception as _fe:
                    log.debug(f"[backtest] fingerprint stop {ev['symbol']}: {_fe}")
                _postmortem_queue.append({
                    "sym": ev["symbol"], "close_reason": ev["type"],
                    "close_price": float(ev["price"]), "pos": _stop_pos,
                    "sim_date": sim_date, "use_llm": use_llm,
                })

        # --- Equity snapshot ---
        eod_close = datetime.combine(sim_date, datetime.min.time()).replace(hour=16, minute=0)
        broker.set_sim_dt(eod_close)
        bt_entry_queue.expire(eod_close)
        account = broker.get_account()
        equity_curve.append({
            "date": str(sim_date),
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "positions": len([p for p in account.positions if p.quantity != 0]),
        })

        # --- Flush buffered postmortems (one batch write per day, not per close) ---
        if _postmortem_queue:
            from ..learning.postmortem import run_trade_postmortem
            for _pm in _postmortem_queue:
                try:
                    run_trade_postmortem(
                        _pm["sym"], _pm["close_reason"], _pm["close_price"], _pm["pos"],
                        as_of_date=_pm["sim_date"],
                        postmortems_file=_BT_POSTMORTEMS,
                        lessons_file=_BT_LESSONS,
                        use_llm=_pm["use_llm"],
                        run_id=run_id,
                        stop_verdict=_pm.get("stop_verdict"),
                    )
                except Exception as _pe:
                    log.debug(f"[backtest] postmortem {_pm['sym']}: {_pe}")
            _postmortem_queue.clear()

    # ------------------------------------------------------------------ close out remaining positions at last close
    if trading_days:
        final_date = trading_days[-1]
        final_dt = datetime.combine(final_date, datetime.min.time()).replace(hour=16, minute=0)
        broker.set_sim_dt(final_dt)
        for pos in broker.get_positions():
            price = cache.price_at(pos.symbol, final_date)
            if price:
                pnl = (price - pos.avg_entry) * pos.quantity
                pnl_pct = pnl / (pos.avg_entry * pos.quantity) if pos.avg_entry > 0 else 0.0
                broker.trades.append({
                    "symbol": pos.symbol, "side": "SELL",
                    "qty": pos.quantity,
                    "entry": round(pos.avg_entry, 4),
                    "exit": round(price, 4),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "reason": "end_of_backtest",
                    "closed_at": str(final_date),
                })

    # Enrich stop-loss and locked-profit-stop trades with the day's closing price
    # so we can judge whether each stop fired at the right moment.
    for trade in broker.trades:
        reason = trade.get("reason")
        if reason not in ("stop_loss", "locked_profit_stop"):
            continue
        try:
            trade_date = date.fromisoformat(str(trade["closed_at"]))
            day_close = cache.price_at(trade["symbol"], trade_date)
            if day_close:
                exit_price = float(trade["exit"])
                post_move_pct = (day_close - exit_price) / exit_price
                trade["stop_close"] = round(day_close, 4)
                trade["post_stop_move_pct"] = round(post_move_pct, 4)
                if reason == "stop_loss":
                    if post_move_pct > 0.01:
                        trade["stop_verdict"] = "too_tight"
                    elif post_move_pct < -0.01:
                        trade["stop_verdict"] = "correct"
                    else:
                        trade["stop_verdict"] = "ambiguous"
                else:  # locked_profit_stop
                    if post_move_pct > 0.01:
                        trade["stop_verdict"] = "still_rising"  # locked stop fired too early
                    elif post_move_pct < -0.01:
                        trade["stop_verdict"] = "reversed"       # good timing, stock dropped
                    else:
                        trade["stop_verdict"] = "flat"
        except Exception:
            pass

    # Enrich signal-close trades with post-exit movement (did the stock keep falling after we sold?)
    _skip_exit_reasons = {"stop_loss", "locked_profit_stop", "end_of_backtest",
                          "weak_close_trim_50pct", "reduce_half", "flatten_all"}
    for trade in broker.trades:
        if trade.get("reason", "") in _skip_exit_reasons:
            continue
        if "close_verdict" in trade:
            continue
        try:
            trade_date = date.fromisoformat(str(trade["closed_at"]))
            day_close = cache.price_at(trade["symbol"], trade_date)
            if day_close:
                exit_price = float(trade["exit"])
                post_move_pct = (day_close - exit_price) / exit_price
                trade["close_day_end"] = round(day_close, 4)
                trade["post_close_move_pct"] = round(post_move_pct, 4)
                if post_move_pct < -0.01:
                    trade["close_verdict"] = "correct"
                elif post_move_pct > 0.01:
                    trade["close_verdict"] = "early"
                else:
                    trade["close_verdict"] = "neutral"
        except Exception:
            pass

    # Enrich ALL decisions with 5-day forward return for indicator effectiveness analysis.
    # BUY uses current_price (price at entry decision), CLOSE uses exit price.
    for _row in decisions_log:
        if not _row.get("current_price"):
            continue
        try:
            _d = date.fromisoformat(_row["date"])
            _fwd_day = _fifth_trading_day_after(_d, all_days)
            if _fwd_day is None:
                continue
            _fwd_p = cache.price_at(_row["symbol"], _fwd_day)
            _cur_p = float(_row["current_price"])
            if _fwd_p and _cur_p > 0:
                _row["fwd_5d_return"] = round((_fwd_p - _cur_p) / _cur_p, 4)
        except Exception:
            pass

    return {
        "trades": broker.trades,
        "equity_curve": equity_curve,
        "cycle_log": cycle_log,
        "decisions_log": decisions_log,
        "deep_score_runs": deep_score_runs,
        "starting_cash": starting_cash,
        "all_fills": broker._all_fills,
        "entry_queue_log": bt_entry_queue.history,
    }


# ------------------------------------------------------------------ helpers

def _run_deep_score(symbols: list[str], cache: DataCache, as_of_date: date) -> dict:
    """Run deep scorer for all symbols with history sliced to as_of_date."""
    from ..analysis.deep_scorer import score_ticker

    spy_bars = cache.daily_bars("SPY", as_of_date)
    spy_hist = spy_bars if not spy_bars.empty else None

    results: dict[str, dict] = {}
    for sym in symbols:
        try:
            result = score_ticker(sym, spy_hist=spy_hist, as_of_date=as_of_date)
            results[sym.upper()] = result
        except Exception as e:
            log.debug(f"[backtest] deep score failed for {sym}: {e}")
    return results


def _decide(
    broker: BacktestBroker,
    symbol: str,
    breadth: dict,
    regime: dict,
    position,
    deep_scores: dict,
    newsapi_key: str,
    sim_date: date,
    weights: dict,
    t_cfg: dict,
    use_llm: bool = True,
    deep_score_entry: dict | None = None,
    circuit_broken: bool = False,
    fingerprints_file: str | None = None,
    cycle_dt: "datetime | None" = None,
    news_cache: "dict | None" = None,
    closed_today: "set | None" = None,
    bt_queue: BacktestEntryQueue | None = None,
    queue_cfg: dict | None = None,
) -> dict:
    """Backtest-specific version of decide_for_ticker."""
    from ..analysis import technical_signal
    from ..analysis.trade_quality import classify_trade_quality
    from ..trading.position_manager import should_flatten_for_risk

    # Stop/TP check
    if position:
        try:
            q = broker.get_quote(symbol)
            flatten, why = should_flatten_for_risk(position, q.last)
            if flatten:
                return {"symbol": symbol, "action": "CLOSE",
                        "combined_score": -1.0, "reason": why, "signals": {},
                        "quality": {"label": "unknown"}, "trend": {}, "regime": {}}
        except Exception:
            pass

    tech = technical_signal(
        broker,
        symbol,
        regime=str(regime.get("label") or "neutral") if isinstance(regime, dict) else None,
    )
    _nkey = _news_cache_key(symbol, cycle_dt if cycle_dt else sim_date)
    if news_cache is not None and _nkey in news_cache:
        news = news_cache[_nkey]
    else:
        news = backtest_news_signal(symbol, cycle_dt if cycle_dt else sim_date, newsapi_key)
        if news_cache is not None:
            news_cache[_nkey] = news
    trend = backtest_trend(broker._cache, symbol, sim_date)

    # Similarity query: find past setups similar to current conditions
    similarity_line = ""
    try:
        from ..learning.setup_memory import find_similar_setups, format_similarity_block
        _partial_snap = {
            "regime": regime,
            "trend": {"label": trend.get("label"), "short": trend.get("short"),
                      "long": trend.get("long")},
            "signals": {"technicals": tech, "news": news, "breadth": breadth},
            "gap_up": False,
        }
        _current_price = broker.get_quote(symbol).last
        _match = find_similar_setups(
            _partial_snap, _current_price, window_days=60, min_matches=3,
            db_file=fingerprints_file,
        )
        similarity_line = format_similarity_block(_match) if _match else ""
    except Exception:
        pass

    if use_llm:
        llm = backtest_llm_signal(
            symbol=symbol, tech=tech, news=news, breadth=breadth,
            position_qty=position.quantity if position else 0.0,
            regime=regime, as_of_date=sim_date,
            deep_score=deep_score_entry,
            similarity_line=similarity_line,
        )
    else:
        llm = {"symbol": symbol, "source": "llm", "score": 0.0,
               "action": "HOLD", "confidence": 0.0, "reason": "skipped (non-LLM cycle)"}

    combined = (
        weights["technicals"] * tech["score"]
        + weights["news"] * news["score"]
        + weights["breadth"] * breadth["score"]
        + weights["llm"] * llm["score"]
    )

    # Deep score gate
    deep_allow = True
    deep_size_mult = 1.0
    deep_note = ""
    entry = deep_scores.get(symbol.upper())
    if entry and not entry.get("error"):
        sc = float(entry.get("score", 50))
        gr = entry.get("grade", "?")
        if sc < 25:
            deep_allow, deep_size_mult = False, 0.0
            deep_note = f"deep={sc:.0f}({gr}) vetoed"
        elif sc < 40:
            deep_size_mult = 0.5
            deep_note = f"deep={sc:.0f}({gr}) size×0.5"
        elif sc < 55:
            deep_size_mult = 0.75
            deep_note = f"deep={sc:.0f}({gr}) size×0.75"
        else:
            deep_note = f"deep={sc:.0f}({gr})"

    llm_action = str(llm.get("action", "HOLD")).upper()

    if llm_action == "CLOSE" and position:
        _min_hold_h = float(t_cfg.get("min_hold_hours_before_llm_close", 3.0))
        _allow_llm_close = True
        if _min_hold_h > 0:
            _tags = dict(getattr(position, "tags", {}) or {})
            _entry_str = _tags.get("entry_datetime", "")
            if _entry_str:
                try:
                    _entry_dt = datetime.fromisoformat(str(_entry_str))
                    _ref_dt = cycle_dt if cycle_dt else datetime.combine(sim_date, datetime.min.time())
                    _hold_h = (_ref_dt.replace(tzinfo=None) - _entry_dt.replace(tzinfo=None)).total_seconds() / 3600
                    _llm_conf = float(llm.get("confidence", 0.0))
                    if _hold_h < _min_hold_h and _llm_conf < 0.85:
                        _allow_llm_close = False
                except Exception:
                    pass
        if _allow_llm_close:
            action = "CLOSE"
    elif not deep_allow and not position:
        action = "HOLD"
    elif combined >= t_cfg["buy_threshold"] and not position:
        action = "BUY"
    elif combined <= t_cfg["sell_threshold"] and position and position.quantity > 0:
        action = "CLOSE"
    elif llm_action == "BUY" and not position and combined >= t_cfg["buy_threshold"] * 0.7:
        action = "BUY"
    else:
        action = "HOLD"

    quality = classify_trade_quality(
        combined_score=combined, tech=tech, news=news,
        breadth=breadth, llm=llm, trend=trend, regime=regime,
    )

    # Quality gate in adverse regimes — skip when LLM is disabled because the
    # combined score is structurally lower without the LLM weight, making "weak"
    # a misleading label rather than a genuine low-conviction signal.
    regime_label = str(regime.get("label") or "neutral").lower()
    if (
        action == "BUY"
        and use_llm
        and t_cfg.get("skip_weak_in_adverse_regimes", True)
        and regime_label in {"bearish", "volatile"}
        and quality.get("label") == "weak"
    ):
        action = "HOLD"
        deep_note += " | filtered:weak+adverse"

    # Weak quality in downtrend gate — mirrors live bot's hard block
    _trend_label = str(trend.get("label", "")).lower()
    if action == "BUY" and quality.get("label") == "weak" and "downtrend" in _trend_label:
        action = "HOLD"
        deep_note += " | filtered:weak+downtrend"

    # Min tech score gate — blocks entries where technicals are flat/negative
    _min_tech = float(t_cfg.get("min_entry_tech_score", 0.0))
    if action == "BUY" and _min_tech > 0 and tech.get("score", 0) < _min_tech:
        action = "HOLD"
        deep_note += f" | tech_too_low({tech.get('score', 0):.3f})"

    # Promoted rule gates (rules with fire_count>=25, hit_rate>=0.72 become hard constraints)
    if action == "BUY":
        try:
            from ..learning.rules import check_promoted_rules
            _promote_dec = {
                "regime": regime.get("label") or "neutral",
                "trend": trend.get("label", ""),
                "quality": quality.get("label", ""),
                "action": "BUY",
                "signals": {"breadth": breadth.get("score", 0.0)},
            }
            _blocks = check_promoted_rules(_promote_dec, regime=regime.get("label") or "neutral")
            if _blocks:
                action = "HOLD"
                deep_note += f" | {_blocks[0][:60]}"
        except Exception:
            pass

    # Circuit breaker gate
    if circuit_broken and action == "BUY":
        action = "HOLD"
        deep_note += " | circuit_breaker"

    # Per-day re-entry stop limit — don't re-enter a symbol already stopped out today
    if action == "BUY":
        max_day_stops = int(t_cfg.get("max_daily_stops_per_symbol", 1))
        if broker.get_stop_count(symbol) >= max_day_stops:
            action = "HOLD"
            deep_note += f" | blocked:stopped_{broker.get_stop_count(symbol)}x_today"

    # Same-day re-entry block — mirrors live bot's symbol_closed_today() gate
    if action == "BUY" and not position and t_cfg.get("same_day_reentry_blocked", True):
        if symbol.upper() in (closed_today or set()):
            action = "HOLD"
            deep_note += " | same_day_reentry_blocked"

    # Pre-entry filters (gap-up, volume, earnings blackout) — only relevant for BUY
    gap_up = False
    if action == "BUY":
        from .signals import backtest_gap_up, backtest_volume_ok, backtest_earnings_blackout
        # Gap-up: block if today opened too far above prior close with weak news
        _is_gap_up, _gap_pct = backtest_gap_up(broker._cache, symbol, sim_date)
        if _is_gap_up:
            gap_min_news = float(t_cfg.get("gap", {}).get("min_news_score", 0.40))
            if news["score"] < gap_min_news:
                action = "HOLD"
                deep_note += f" | gap_up_blocked({_gap_pct:.1%})"
            else:
                gap_up = True  # gap-up but news supports it — allow, tag for trailing stop
        # Volume: block if today's volume is unusually thin
        _vol_ok, _vol_ratio = backtest_volume_ok(broker._cache, symbol, sim_date)
        if not _vol_ok:
            action = "HOLD"
            deep_note += f" | vol_low({_vol_ratio:.2f}x)"
        # Earnings blackout: block if earnings are within blackout window
        _blk, _blk_reason = backtest_earnings_blackout(symbol, sim_date, deep_score_entry, news["score"])
        if _blk:
            action = "HOLD"
            deep_note += f" | {_blk_reason}"

    q_cfg = queue_cfg or {}
    sim_dt_for_queue = cycle_dt if cycle_dt else datetime.combine(sim_date, datetime.min.time())
    if bt_queue is not None and llm_action == "CLOSE" and not position and bt_queue.has_entry(symbol):
        bt_queue.remove_entry(symbol, reason="llm_close_cancel")
        deep_note += " | queue_cancelled_by_llm_close"

    fib_d = tech.get("details", {}) or {}
    fib_dir = str(fib_d.get("fib_direction") or "").lower()
    fib_price = fib_d.get("fib_nearest_price")
    fib_ratio = fib_d.get("fib_nearest_ratio")
    fib_prox_pct = fib_d.get("fib_proximity_pct")
    try:
        fib_price_f = float(fib_price) if fib_price is not None else None
    except Exception:
        fib_price_f = None
    try:
        fib_prox_pct_f = float(fib_prox_pct) if fib_prox_pct is not None else None
    except Exception:
        fib_prox_pct_f = None
    try:
        fib_prox_ratio = fib_prox_pct_f / 100.0 if fib_prox_pct_f is not None else float(fib_d.get("fib_proximity", 1.0))
    except Exception:
        fib_prox_ratio = 1.0

    if (
        bt_queue is not None
        and q_cfg.get("enabled", False)
        and action == "BUY"
        and not position
        and fib_dir == "resistance"
        and fib_prox_ratio < 0.03
        and fib_price_f is not None
        and fib_price_f > 0
    ):
        action = "HOLD"
        deep_note += " | queued:breakout_resistance"
        try:
            last_price = float(fib_d.get("last", 0) or 0)
            bt_queue.add_entry(
                symbol=symbol,
                entry_type="breakout_resistance",
                trigger_price=fib_price_f,
                fib_ratio=float(fib_ratio or 0),
                fib_direction="resistance",
                combined_score=combined,
                price_at_queue=last_price,
                deep_size_mult=deep_size_mult,
                sim_dt=sim_dt_for_queue,
            )
        except Exception as exc:
            log.debug("[backtest-queue] %s resistance queue add failed: %s", symbol, exc)

    if bt_queue is not None and q_cfg.get("enabled", False) and action == "HOLD" and not position:
        score_min = float(q_cfg.get("queue_score_min", 0.28))
        near_pct = float(q_cfg.get("near_level_pct", 0.05))
        tol_pct = float(
            load_config().get("signals", {}).get("technicals", {}).get("fib_tolerance", 0.02)
        ) * 100
        if (
            combined >= score_min
            and fib_dir == "support"
            and fib_price_f is not None
            and fib_price_f > 0
            and fib_prox_pct_f is not None
            and fib_prox_pct_f > tol_pct
        ):
            try:
                last_price = float(fib_d.get("last", 0) or 0)
                if last_price > 0 and (last_price - fib_price_f) / last_price <= near_pct:
                    bt_queue.add_entry(
                        symbol=symbol,
                        entry_type="bounce_support",
                        trigger_price=fib_price_f,
                        fib_ratio=float(fib_ratio or 0),
                        fib_direction="support",
                        combined_score=combined,
                        price_at_queue=last_price,
                        deep_size_mult=deep_size_mult,
                        sim_dt=sim_dt_for_queue,
                    )
                    deep_note += " | queued:bounce_support"
            except Exception as exc:
                log.debug("[backtest-queue] %s support queue add failed: %s", symbol, exc)

    return {
        "symbol": symbol,
        "action": action,
        "combined_score": round(float(combined), 3),
        "deep_size_mult": deep_size_mult,
        "reason": (
            f"tech={tech['score']:+.2f} news={news['score']:+.2f} "
            f"b={breadth['score']:+.2f} llm={llm['score']:+.2f} -> {combined:+.2f} | {deep_note}"
        )[:200],
        "trend": {
            "label": trend.get("label"),
            "short": trend.get("short", {}).get("label"),
            "long": trend.get("long", {}).get("label"),
        },
        "regime": {"label": regime.get("label") or "neutral", "score": regime.get("score")},
        "quality": quality,
        "gap_up": gap_up,
        "signals": {"technicals": tech, "news": news,
                    "breadth": {k: breadth[k] for k in ("score", "reason")}, "llm": llm},
        "gate_notes": deep_note,
    }


def _execute_buy(broker: BacktestBroker, sym: str, dec: dict, account, t_cfg: dict):
    """Size and submit a BUY order. Returns the placed Order or None."""
    from ..trading.position_manager import compute_dynamic_stop, compute_size, compute_take_profit
    from ..broker.base import Order, OrderSide

    q = broker.get_quote(sym)
    try:
        price = float(getattr(q, "last", 0.0))
    except (TypeError, ValueError):
        price = 0.0
    if not math.isfinite(price) or price <= 0:
        log.warning(f"[backtest] {sym}: BUY skipped - invalid quote price")
        return None
    min_price = float(t_cfg.get("min_price_for_buy", 0.0))
    if min_price > 0 and price < min_price:
        log.info(
            f"[backtest] {sym}: BUY skipped — price ${q.last:.2f} below "
            f"min_price_for_buy ${min_price:.2f}"
        )
        return None
    stop_info = compute_dynamic_stop(broker, sym, price)
    tp_price = compute_take_profit(price)

    trend_d = dec.get("trend") or {}
    qty, _size_info = compute_size(
        account, price,
        trend={
            "label": trend_d.get("label"),
            "short": {"label": trend_d.get("short")},
            "long": {"label": trend_d.get("long")},
        },
        stop_price=stop_info.get("stop"),
        regime=dec.get("regime") or {},
    )
    deep_mult = float(dec.get("deep_size_mult", 1.0))
    if deep_mult < 1.0 and qty > 0:
        qty = max(1, math.floor(qty * deep_mult))
    if qty <= 0:
        return None

    order = Order(
        symbol=sym, side=OrderSide.BUY, quantity=qty,
        stop_loss=stop_info.get("stop"),
        take_profit=tp_price,
        notes=dec.get("reason", "")[:200],
    )
    placed = broker.place_order(order)
    if placed.status == "filled":
        entry_price = placed.filled_price
        stop_price = stop_info.get("stop") or 0.0
        _ts_cfg = t_cfg.get("trailing_stop", {}) or {}
        is_small_cap = entry_price <= float(_ts_cfg.get("small_cap_price_threshold", 15.0))
        is_gap_up = dec.get("gap_up", False)
        is_trailing = (
            bool(_ts_cfg.get("enabled_for_small_caps", True)) if is_small_cap
            else bool(_ts_cfg.get("enabled_for_large_caps", False))
        ) or bool(is_gap_up)
        trail_pct = float(_ts_cfg.get("large_cap_trail_pct", 0.10))
        broker.set_position_stop(
            sym,
            stop_loss=stop_price or None,
            take_profit=tp_price,
            tags={
                "entry_price": entry_price,
                "entry_datetime": broker._sim_dt.isoformat() if getattr(broker, "_sim_dt", None) else "",
                "trail_pct": trail_pct,
                "trailing": is_trailing,
                "entry_tech": round(float((dec.get("signals") or {}).get("technicals", {}).get("score", 0)), 3),
                "entry_news": round(float((dec.get("signals") or {}).get("news", {}).get("score", 0)), 3),
                "entry_breadth": round(float((dec.get("signals") or {}).get("breadth", {}).get("score", 0)), 3),
                "entry_llm": round(float((dec.get("signals") or {}).get("llm", {}).get("score", 0)), 3),
                "entry_combined": round(float(dec.get("combined_score", 0)), 3),
                "entry_quality": str((dec.get("quality") or {}).get("label", "")),
                "entry_regime": str((dec.get("regime") or {}).get("label", "")),
                "entry_trend": str((dec.get("trend") or {}).get("label", "")),
            },
        )
    return placed


def _run_entry_queue_monitor(
    *,
    broker: BacktestBroker,
    bt_queue: BacktestEntryQueue,
    start_dt: datetime,
    end_dt: datetime,
    breadth: dict,
    regime: dict,
    newsapi_key: str,
    weights: dict,
    t_cfg: dict,
    queue_cfg: dict,
    use_llm: bool,
    cycle_log: list[dict],
    decisions_log: list[dict],
    sim_date: date,
    news_cache: dict | None,
    fingerprints_file: str,
) -> None:
    """Replay the live queue monitor between scheduled decision cycles.

    The market cache has 15-minute bars, so checking faster than that would
    just re-read the same candle. The default interval is therefore 15 minutes.
    """
    if not bt_queue.enabled or not bt_queue.entries:
        return

    from ..trading.decision_engine import _resolve_max_positions

    interval = int(queue_cfg.get("backtest_monitor_interval_minutes", 15))
    interval = max(5, interval)
    check_dt = start_dt + timedelta(minutes=interval)

    while check_dt < end_dt:
        broker.set_sim_dt(check_dt)
        max_pos = _resolve_max_positions(t_cfg, regime.get("label") or "neutral")

        def _execute(symbol: str, tags: dict, score: float, signals: dict):
            account = broker.get_account()
            held = {p.symbol: p for p in account.positions}
            current_open = sum(1 for p in account.positions if p.quantity != 0)
            if symbol in held:
                log.info("[backtest-queue] %s already held at %s - queued buy skipped", symbol, check_dt)
                return None
            if current_open >= max_pos:
                log.info(
                    "[backtest-queue] %s max_positions reached at %s (%s/%s)",
                    symbol,
                    check_dt,
                    current_open,
                    max_pos,
                )
                return None

            dec = {
                "symbol": symbol,
                "action": "BUY",
                "combined_score": round(float(score), 3),
                "deep_size_mult": float(tags.get("deep_size_mult", 1.0) or 1.0),
                "reason": (
                    f"queue-trigger:{tags.get('entry_type', 'entry')} "
                    f"@ {float(tags.get('trigger_price', 0) or 0):.2f}"
                ),
                "trend": {"label": "unknown", "short": None, "long": None},
                "regime": {"label": regime.get("label") or "neutral", "score": regime.get("score")},
                "quality": {"label": "queued", "score": None, "reason": "backtest entry queue"},
                "signals": signals,
                "gap_up": False,
            }
            placed = _execute_buy(broker, symbol, dec, account, t_cfg)
            if placed and placed.status == "filled":
                cycle = f"Q{check_dt.strftime('%H:%M')}"
                cycle_log.append({
                    "date": str(sim_date),
                    "cycle": cycle,
                    "symbol": symbol,
                    "action": "BUY",
                    "qty": placed.quantity,
                    "price": placed.filled_price,
                    "combined": dec.get("combined_score"),
                    "entry_type": tags.get("entry_type"),
                    "reason": dec["reason"],
                })
                _append_queue_decision_log(
                    decisions_log=decisions_log,
                    broker=broker,
                    sim_date=sim_date,
                    cycle=cycle,
                    symbol=symbol,
                    dec=dec,
                    placed=placed,
                    buy_threshold=float(t_cfg.get("buy_threshold", 0.35)),
                    queue_tags=tags,
                )
                try:
                    from ..learning.setup_memory import record_entry_fingerprint
                    record_entry_fingerprint(
                        symbol,
                        cycle,
                        dec,
                        placed.filled_price,
                        as_of_date=sim_date,
                        db_file=fingerprints_file,
                    )
                except Exception as exc:
                    log.debug("[backtest-queue] fingerprint entry %s failed: %s", symbol, exc)
            return placed

        bt_queue.check_and_fire(
            broker=broker,
            sim_dt=check_dt,
            breadth=breadth,
            regime=regime,
            newsapi_key=newsapi_key,
            weights=weights,
            buy_threshold=float(t_cfg.get("buy_threshold", 0.35)),
            use_llm=use_llm,
            execute_fn=_execute,
            news_cache=news_cache,
        )
        check_dt += timedelta(minutes=interval)


def _append_queue_decision_log(
    *,
    decisions_log: list[dict],
    broker: BacktestBroker,
    sim_date: date,
    cycle: str,
    symbol: str,
    dec: dict,
    placed,
    buy_threshold: float,
    queue_tags: dict,
) -> None:
    signals = dec.get("signals") or {}
    tech = signals.get("technicals") or {}
    details = tech.get("details") or {}
    news = signals.get("news") or {}
    breadth = signals.get("breadth") or {}
    llm = signals.get("llm") or {}
    try:
        current_price = broker.get_quote(symbol).last
    except Exception:
        current_price = None
    decisions_log.append({
        "date": str(sim_date),
        "cycle": cycle,
        "symbol": symbol,
        "action": "BUY",
        "combined": dec.get("combined_score"),
        "buy_threshold": buy_threshold,
        "tech_score": round(float(tech.get("score", 0)), 4),
        "news_score": round(float(news.get("score", 0)), 4),
        "breadth_score": round(float(breadth.get("score", 0)), 4),
        "llm_score": round(float(llm.get("score", 0)), 4),
        "rsi": details.get("rsi"),
        "rsi_score": details.get("rsi_score"),
        "macd_hist": details.get("macd_hist"),
        "macd_score": details.get("macd_score"),
        "adx": details.get("adx"),
        "trend_score": details.get("trend_score"),
        "bb_pct_b": details.get("bb_pct_b"),
        "bb_score": details.get("bb_score"),
        "bb_squeeze": details.get("bb_squeeze"),
        "obv_score": details.get("obv_score"),
        "vwap_score": details.get("vwap_score"),
        "vwap_distance_pct": details.get("vwap_distance_pct"),
        "fib_score": details.get("fib_score"),
        "fib_ratio": details.get("fib_nearest_ratio"),
        "fib_proximity_pct": details.get("fib_proximity_pct"),
        "fib_direction": details.get("fib_direction"),
        "regime": (dec.get("regime") or {}).get("label"),
        "regime_score": (dec.get("regime") or {}).get("score"),
        "trend": (dec.get("trend") or {}).get("label"),
        "breadth_reason": str(breadth.get("reason", ""))[:150],
        "llm_action": llm.get("action"),
        "llm_confidence": llm.get("confidence"),
        "llm_reason": str(llm.get("reason", ""))[:250],
        "quality": "queued",
        "gate_notes": f"queue_trigger:{queue_tags.get('entry_type', '')}",
        "had_position": False,
        "current_price": round(float(current_price), 4) if current_price else None,
        "fill_price": placed.filled_price,
        "qty": placed.quantity,
        "fwd_5d_return": None,
    })


# ------------------------------------------------------------------ watchlist cull helper

def _cull_symbols(
    symbols: list[str],
    cache: DataCache,
    deep_scores: dict,
    sim_date: date,
    newsapi_key: str,
    t_cfg: dict,
    news_cache: "dict | None" = None,
    tech_cache: "dict | None" = None,
) -> list[str]:
    """Drop tickers the live bot would cull: low deep score or weak premarket composite.

    Mirrors filter_and_replace_weak_tickers() from pre_market.py but uses cached
    data so it's safe for historical simulation. Fails open — never returns empty.
    """
    from .signals import backtest_news_signal
    from ..utils.config import load_config
    import pandas as pd

    cfg = load_config()
    cull_cfg = t_cfg.get("cull", {})
    weak_threshold = float(cull_cfg.get(
        "premarket_weak_threshold",
        cfg.get("screener", {}).get("premarket_weak_threshold", -0.10)
    ))
    deep_threshold = float(cull_cfg.get("min_deep_score", 55))

    class _BProxy:
        """Minimal broker-like proxy so technical_signal can read from cache."""
        mode = "backtest"
        def get_bars(self_inner, s, timeframe="1d", limit=200):
            df = cache.daily_bars(s, sim_date)
            if df.empty:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            }).tail(limit)
        def get_quote(self_inner, s):
            from ..broker.base import Quote
            price = cache.price_at(s, sim_date) or 1.0
            return Quote(symbol=s, last=price, bid=price * 0.999,
                         ask=price * 1.001, volume=0, timestamp=sim_date)

    kept: list[str] = []
    for sym in symbols:
        try:
            # Deep score gate
            ds = deep_scores.get(sym.upper())
            if ds and not ds.get("error"):
                if float(ds.get("score", 100)) < deep_threshold:
                    log.debug(
                        f"[backtest-cull] {sym}: deep_score="
                        f"{ds.get('score'):.0f} < {deep_threshold}"
                    )
                    continue

            # Premarket composite (tech * 0.6 + news * 0.4)
            from ..analysis import technical_signal as ts
            _tkey = sym.upper()
            if tech_cache is not None and _tkey in tech_cache:
                tech = tech_cache[_tkey]
            else:
                tech = ts(_BProxy(), sym)
                if tech_cache is not None:
                    tech_cache[_tkey] = tech
            _nkey = _news_cache_key(sym, sim_date)
            if news_cache is not None and _nkey in news_cache:
                news = news_cache[_nkey]
            else:
                news = backtest_news_signal(sym, sim_date, newsapi_key)
                if news_cache is not None:
                    news_cache[_nkey] = news
            composite = tech["score"] * 0.6 + news["score"] * 0.4
            if composite < weak_threshold:
                log.debug(
                    f"[backtest-cull] {sym}: composite={composite:.2f} < {weak_threshold}"
                )
                continue

            kept.append(sym)
        except Exception:
            kept.append(sym)  # fail open on any error

    removed = len(symbols) - len(kept)
    if removed:
        log.info(
            f"[backtest] {sim_date}: cull removed {removed} ticker(s), "
            f"{len(kept)} active"
        )
    return kept if kept else list(symbols)  # never return empty


# ------------------------------------------------------------------ trade-plan ranking helper

def _rank_symbols(
    symbols: list[str],
    cache: DataCache,
    deep_scores: dict,
    sim_date: date,
    newsapi_key: str,
    top_n: int = 20,
    news_cache: "dict | None" = None,
    tech_cache: "dict | None" = None,
) -> list[str]:
    """Rank symbols by composite score and return the top_n.

    Composite mirrors build_trade_plan():
      deep*0.40 + (tech*50+50)*0.35 + (news*50+50)*0.25

    News calls hit the per-day Finnhub cache so no extra API calls here if
    _cull_symbols already fetched them. Fails open — returns all symbols
    unchanged on total failure.
    """
    from ..analysis import technical_signal
    from .signals import backtest_news_signal
    import pandas as pd

    if len(symbols) <= top_n:
        return symbols  # nothing to trim

    class _BProxy:
        mode = "backtest"
        def get_bars(self_inner, s, timeframe="1d", limit=200):
            df = cache.daily_bars(s, sim_date)
            if df.empty:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            }).tail(limit)
        def get_quote(self_inner, s):
            from ..broker.base import Quote
            price = cache.price_at(s, sim_date) or 1.0
            return Quote(symbol=s, last=price, bid=price * 0.999,
                         ask=price * 1.001, volume=0, timestamp=sim_date)

    proxy = _BProxy()
    scored: list[tuple[str, float]] = []
    for sym in symbols:
        try:
            ds = deep_scores.get(sym.upper())
            deep = float(ds.get("score", 50)) if ds and not ds.get("error") else 50.0
            _tkey = sym.upper()
            if tech_cache is not None and _tkey in tech_cache:
                tech = tech_cache[_tkey]["score"]
            else:
                _tech_result = technical_signal(proxy, sym)
                if tech_cache is not None:
                    tech_cache[_tkey] = _tech_result
                tech = _tech_result["score"]
            _nkey = _news_cache_key(sym, sim_date)
            if news_cache is not None and _nkey in news_cache:
                news = news_cache[_nkey]["score"]
            else:
                _news_result = backtest_news_signal(sym, sim_date, newsapi_key)
                if news_cache is not None:
                    news_cache[_nkey] = _news_result
                news = _news_result["score"]
            composite = deep * 0.40 + (tech * 50 + 50) * 0.35 + (news * 50 + 50) * 0.25
            scored.append((sym, composite))
        except Exception:
            scored.append((sym, 50.0))

    scored.sort(key=lambda x: x[1], reverse=True)
    picked = [s for s, _ in scored[:top_n]]
    log.info(
        f"[backtest] {sim_date}: trade-plan {len(symbols)} -> top {len(picked)} | "
        + " ".join(f"{s}({c:.0f})" for s, c in scored[:5])
        + (" ..." if len(scored) > 5 else "")
    )
    return picked


# ------------------------------------------------------------------ trailing stop helper

def _update_trailing_stops(broker: BacktestBroker, held: dict, sim_date) -> None:
    """Ratchet trailing stops up as price rises — mirrors live bot's per-cycle update."""
    for sym, pos in held.items():
        tags = pos.tags or {}
        if not tags.get("trailing"):
            continue
        trail_pct = float(tags.get("trail_pct", 0.10))
        try:
            current = broker.get_quote(sym).last
        except Exception:
            continue
        new_stop = current * (1 - trail_pct)
        old_stop = pos.stop_loss or 0.0
        if new_stop > old_stop:
            broker.set_position_stop(sym, stop_loss=new_stop)
            log.debug(
                f"[backtest] trailing stop ratcheted: {sym} "
                f"${old_stop:.2f} -> ${new_stop:.2f} (trail {trail_pct:.1%})"
            )


# ------------------------------------------------------------------ locked profit daily ratchet

def _ratchet_locked_profit_stops(broker: BacktestBroker) -> None:
    """At 15:30 each day, move locked-profit stops 50% of the way toward current price.

    Example: stop=$105, price=$120 → new stop = $105 + 50%×($120-$105) = $112.50
    Runs once per day (15:30 only) so each day's close locks in half the remaining gap.
    """
    for pos in broker.get_positions():
        tags = pos.tags or {}
        if not tags.get("locked_profit"):
            continue
        try:
            current = broker.get_quote(pos.symbol).last
        except Exception:
            continue
        old_stop = pos.stop_loss or 0.0
        if current <= old_stop:
            continue
        _t = load_config().get("trading", {})
        ratchet_min = float(_t.get("ratchet_min_move_pct", 0.025))
        if current < old_stop * (1.0 + ratchet_min):
            continue
        ratchet_step = float(_t.get("ratchet_step_pct", 0.30))
        new_stop = old_stop + ratchet_step * (current - old_stop)
        if (new_stop - old_stop) < 0.005 * current:
            continue
        if new_stop > old_stop:
            broker.set_position_stop(pos.symbol, stop_loss=new_stop)
            log.info(
                f"[backtest] locked ratchet: {pos.symbol} "
                f"${old_stop:.2f} -> ${new_stop:.2f} (price=${current:.2f})"
            )


# ------------------------------------------------------------------ CLI entry point

def main():
    import argparse
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Walk-forward backtest")
    parser.add_argument("tickers", nargs="*", help="Specific tickers to test (default: shortlist)")
    parser.add_argument("--days", type=int, default=90, help="Trading days to backtest (default 90)")
    parser.add_argument("--cash", type=float, default=100_000.0, help="Starting cash")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM advisor calls")
    parser.add_argument("--no-deep", action="store_true", help="Skip deep scorer")
    parser.add_argument("--skip-days", type=int, default=0, help="Resume: skip first N days")
    parser.add_argument("--today", action="store_true", help="Backtest today's session (use after market close)")
    parser.add_argument("--end-date", default=None, help="Last date to backtest (YYYY-MM-DD). Default: yesterday")
    parser.add_argument("--out", default="data/backtest_results.json", help="Output JSON path")
    args = parser.parse_args()

    if args.today:
        args.days = 1
        _end_date = date.today()
    elif args.end_date:
        _end_date = date.fromisoformat(args.end_date)
    else:
        _end_date = None

    if args.tickers:
        syms = [s.upper() for s in args.tickers]
    else:
        cfg = load_config()
        syms = cfg.get("screener", {}).get("watchlist", [])

    if not syms:
        print("No tickers to backtest. Pass tickers as arguments or configure a watchlist.")
        sys.exit(1)

    print(f"\nBacktesting {len(syms)} ticker(s) over {args.days} trading days: {', '.join(syms[:10])}")
    if len(syms) > 10:
        print(f"  ... and {len(syms)-10} more")
    print()

    out_path = Path(args.out)
    _label = "full" if (not args.no_llm and not args.no_deep) else (
        "no-llm-deep" if (args.no_llm and args.no_deep) else
        "no-llm" if args.no_llm else "no-deep"
    )
    _archive_dir = out_path.parent
    _existing = sorted(_archive_dir.glob(f"backtest_results_{_label}*.json"))
    _next_num = 1
    if _existing:
        import re as _re
        for _p in reversed(_existing):
            _m = _re.search(r"(\d+)\.json$", _p.name)
            if _m:
                _next_num = int(_m.group(1)) + 1
                break
    _archive_path = _archive_dir / f"backtest_results_{_label}{_next_num:03d}.json"

    results = run_backtest(
        symbols=syms,
        days=args.days,
        starting_cash=args.cash,
        use_deep_scorer=not args.no_deep,
        use_llm=not args.no_llm,
        verbose=True,
        skip_days=args.skip_days,
        end_date=_end_date,
        run_id=_archive_path.stem,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nRaw results saved to {out_path}")

    _dl_path = out_path.parent / "backtest_decisions.jsonl"
    _dl_rows = results.get("decisions_log", [])
    with open(_dl_path, "w", encoding="utf-8") as _f:
        for _row in _dl_rows:
            _f.write(json.dumps(_row, default=str) + "\n")
    print(f"Decisions log saved to {_dl_path}  ({len(_dl_rows)} rows)")

    _archive_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Archive copy saved to  {_archive_path}")
    # Archive backtest run to permanent history + decisions master
    try:
        from src.dashboard.archiver import archive_backtest_run
        _run_meta = {
            "run_id": _archive_path.stem,
            "label": _label,
            "days": args.days,
            "results_file": _archive_path.as_posix(),
            "flags": (["no-llm"] if args.no_llm else []) + (["no-deep"] if args.no_deep else []),
        }
        archive_backtest_run(results, _run_meta, out_path.parent)
        print("Backtest run archived to history + decisions master")
    except Exception as _arc_e:
        print(f"[archive] non-critical failure: {_arc_e}")

    from .reporter import generate_report
    print(generate_report(results))

    try:
        from src.utils.git_sync import push_data_to_github
        push_data_to_github(f"backtest-{_archive_path.stem}")
    except Exception as _ge:
        print(f"[git-sync] non-critical: {_ge}")


if __name__ == "__main__":
    main()
