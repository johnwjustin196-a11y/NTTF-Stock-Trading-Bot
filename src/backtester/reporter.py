"""Backtest performance reporter.

Computes and formats the standard suite of trading metrics from the
`run_backtest()` results dict:

  - Total return & P&L
  - Sharpe ratio (annualized from daily equity returns)
  - Max drawdown (peak-to-trough on equity curve)
  - Win rate, avg win, avg loss, profit factor
  - Best / worst 5 individual trades
  - Per-symbol breakdown (trades, win rate, total P&L)
  - Equity curve sample (every ~20th day so it fits in a terminal)
  - Deep scorer dates + cycle log summary
"""
from __future__ import annotations

import statistics
from math import isfinite


def generate_report(results: dict) -> str:
    """Return a formatted plaintext performance report."""
    trades = results.get("trades", [])
    equity_curve = results.get("equity_curve", [])
    cycle_log = results.get("cycle_log", [])
    deep_score_runs = results.get("deep_score_runs", [])
    starting_cash = float(results.get("starting_cash", 100_000.0))

    if results.get("error"):
        return f"\n[backtest] ERROR: {results['error']}\n"

    lines: list[str] = []

    # -------------------------------------------------------------- header
    start_date = equity_curve[0]["date"] if equity_curve else "n/a"
    end_date = equity_curve[-1]["date"] if equity_curve else "n/a"
    final_equity = float(equity_curve[-1]["equity"]) if equity_curve else starting_cash
    total_pnl = final_equity - starting_cash
    total_return = total_pnl / starting_cash if starting_cash > 0 else 0.0

    lines += [
        "",
        "=" * 62,
        "  BACKTEST RESULTS",
        "=" * 62,
        "",
        f"  Period:           {start_date} to {end_date}",
        f"  Trading days:     {len(equity_curve)}",
        f"  Starting capital: ${starting_cash:>12,.2f}",
        f"  Final equity:     ${final_equity:>12,.2f}",
        f"  Total P&L:        ${total_pnl:>+12,.2f}",
        f"  Total return:     {total_return:>12.2%}",
        "",
    ]

    # -------------------------------------------------------------- Sharpe + drawdown
    daily_returns = _daily_returns(equity_curve, starting_cash)
    sharpe = _sharpe(daily_returns)
    max_dd, max_dd_from, max_dd_to = _max_drawdown(equity_curve, starting_cash)

    lines += [
        "--- RISK ---",
        f"  Sharpe ratio:     {sharpe:>12.2f}  (annualized, daily returns)",
        f"  Max drawdown:     {max_dd:>12.2%}  ({max_dd_from} to {max_dd_to})",
        "",
    ]

    # -------------------------------------------------------------- trade stats
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = statistics.mean(t["pnl"] for t in wins) if wins else 0.0
    avg_loss = statistics.mean(t["pnl"] for t in losses) if losses else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    lines += [
        "--- TRADES ---",
        f"  Total trades:     {len(trades):>12}",
        f"  Wins / Losses:    {len(wins):>5} / {len(losses):<5}",
        f"  Win rate:         {win_rate:>12.2%}",
        f"  Avg win:          ${avg_win:>+11,.2f}",
        f"  Avg loss:         ${avg_loss:>+11,.2f}",
        f"  Profit factor:    {_fmt_pf(profit_factor):>12}",
        f"  Gross profit:     ${gross_win:>12,.2f}",
        f"  Gross loss:       ${gross_loss:>12,.2f}",
        "",
    ]

    # -------------------------------------------------------------- best / worst
    if trades:
        by_pnl = sorted(trades, key=lambda t: t.get("pnl", 0), reverse=True)
        lines.append("--- BEST TRADES ---")
        for t in by_pnl[:5]:
            lines.append(
                f"  {t['symbol']:<8} ${t['pnl']:>+8,.2f}  ({t.get('pnl_pct', 0):+.1%})  "
                f"{t.get('closed_at', '')}  {t.get('reason', '')[:35]}"
            )
        lines.append("")
        lines.append("--- WORST TRADES ---")
        for t in by_pnl[-5:]:
            lines.append(
                f"  {t['symbol']:<8} ${t['pnl']:>+8,.2f}  ({t.get('pnl_pct', 0):+.1%})  "
                f"{t.get('closed_at', '')}  {t.get('reason', '')[:35]}"
            )
        lines.append("")

    # -------------------------------------------------------------- stop analysis
    stop_trades = [t for t in trades if t.get("reason") == "stop_loss" and "stop_verdict" in t]
    if stop_trades:
        too_tight  = [t for t in stop_trades if t["stop_verdict"] == "too_tight"]
        correct    = [t for t in stop_trades if t["stop_verdict"] == "correct"]
        ambiguous  = [t for t in stop_trades if t["stop_verdict"] == "ambiguous"]
        n = len(stop_trades)
        lines += [
            "--- STOP ANALYSIS (did the stock recover after being stopped out?) ---",
            f"  Stop-loss exits:  {n:>6}",
            f"  Too tight  (closed >1% above stop):  {len(too_tight):>4}  ({len(too_tight)/n:5.0%})",
            f"  Correct    (closed >1% below stop):  {len(correct):>4}  ({len(correct)/n:5.0%})",
            f"  Ambiguous  (closed within 1%):       {len(ambiguous):>4}  ({len(ambiguous)/n:5.0%})",
            "",
            f"  {'Symbol':<8} {'Date':<12} {'StopExit':>9} {'DayClose':>9} {'Move':>7}  Verdict",
            "  " + "-" * 56,
        ]
        for t in sorted(stop_trades, key=lambda x: x.get("post_stop_move_pct", 0), reverse=True):
            move = t.get("post_stop_move_pct", 0)
            lines.append(
                f"  {t['symbol']:<8} {t.get('closed_at',''):<12} "
                f"${t['exit']:>8.2f} ${t.get('stop_close',0):>8.2f} "
                f"{move:>+6.1%}  {t['stop_verdict']}"
            )
        lines.append("")

    # -------------------------------------------------------------- signal exit analysis
    _skip_exit = {"stop_loss", "locked_profit_stop", "end_of_backtest",
                  "weak_close_trim_50pct", "reduce_half", "flatten_all"}
    signal_exits = [t for t in trades if t.get("reason", "") not in _skip_exit and "close_verdict" in t]
    if signal_exits:
        correct = [t for t in signal_exits if t["close_verdict"] == "correct"]
        early   = [t for t in signal_exits if t["close_verdict"] == "early"]
        neutral = [t for t in signal_exits if t["close_verdict"] == "neutral"]
        n = len(signal_exits)
        lines += [
            "--- SIGNAL EXIT ANALYSIS (did the stock keep falling after we sold?) ---",
            f"  Signal closes with post-exit data:    {n}",
            f"  Correct  (dropped >1% rest of day):   {len(correct):>4}  ({len(correct)/n:5.0%})  <- good timing",
            f"  Early    (kept rising >1% rest of day):{len(early):>4}  ({len(early)/n:5.0%})  <- sold too soon",
            f"  Neutral  (within 1% of day end):      {len(neutral):>4}  ({len(neutral)/n:5.0%})",
            "",
            f"  {'Symbol':<8} {'Date':<12} {'Exit':>8} {'DayEnd':>8} {'Move':>7}  Verdict",
            "  " + "-" * 56,
        ]
        for t in sorted(signal_exits, key=lambda x: x.get("post_close_move_pct", 0), reverse=True):
            move = t.get("post_close_move_pct", 0)
            lines.append(
                f"  {t['symbol']:<8} {t.get('closed_at',''):<12} "
                f"${t['exit']:>7.2f} ${t.get('close_day_end', 0):>7.2f} "
                f"{move:>+6.1%}  {t['close_verdict']}"
            )
        lines.append("")

    # -------------------------------------------------------------- by symbol
    by_sym: dict[str, dict] = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in by_sym:
            by_sym[sym] = {"n": 0, "pnl": 0.0, "wins": 0}
        by_sym[sym]["n"] += 1
        by_sym[sym]["pnl"] += t.get("pnl", 0.0)
        if t.get("pnl", 0.0) > 0:
            by_sym[sym]["wins"] += 1

    if by_sym:
        lines.append("--- BY SYMBOL ---")
        lines.append(f"  {'Symbol':<10} {'Trades':>6} {'WinRate':>8} {'Total P&L':>12}")
        lines.append("  " + "-" * 38)
        for sym, s in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
            wr = s["wins"] / s["n"] if s["n"] else 0.0
            lines.append(
                f"  {sym:<10} {s['n']:>6}   {wr:>6.0%}   ${s['pnl']:>9,.2f}"
            )
        lines.append("")

    # -------------------------------------------------------------- entry signal performance
    entry_sig_trades = [t for t in trades if "entry_tech" in t]
    if entry_sig_trades:
        def _perf(subset: list) -> str:
            if not subset:
                return f"{'':>4}  0 trades"
            wins = sum(1 for t in subset if t.get("pnl", 0) > 0)
            avg_p = sum(t.get("pnl", 0) for t in subset) / len(subset)
            return f"  {len(subset):>3} trades  {wins/len(subset):5.0%} win  avg ${avg_p:>+7.0f}"

        lines += [
            "--- ENTRY SIGNAL PERFORMANCE ---",
            f"  {len(entry_sig_trades)} trades with signal data at entry",
            "",
        ]
        for sig_key, sig_label in [
            ("entry_tech",    "Tech"),
            ("entry_news",    "News"),
            ("entry_llm",     "LLM"),
        ]:
            pos_t = [t for t in entry_sig_trades if t.get(sig_key, 0) > 0.20]
            neu_t = [t for t in entry_sig_trades if -0.20 <= t.get(sig_key, 0) <= 0.20]
            neg_t = [t for t in entry_sig_trades if t.get(sig_key, 0) < -0.20]
            lines.append(f"  {sig_label} at entry:")
            lines.append(f"    Positive (>+0.20): {_perf(pos_t)}")
            lines.append(f"    Neutral  (± 0.20): {_perf(neu_t)}")
            lines.append(f"    Negative (<-0.20): {_perf(neg_t)}")
            lines.append("")
        for grp_key, grp_label, grp_vals in [
            ("entry_quality", "Entry quality",   ["strong", "good", "weak", "unknown"]),
            ("entry_regime",  "Regime at entry", ["bullish", "neutral", "bearish", "volatile", "uncertain"]),
            ("entry_trend",   "Trend at entry",  ["up", "sideways", "down", "neutral", "unknown"]),
        ]:
            subset_any = [t for t in entry_sig_trades if t.get(grp_key)]
            if subset_any:
                lines.append(f"  {grp_label}:")
                for val in grp_vals:
                    sub = [t for t in entry_sig_trades if t.get(grp_key) == val]
                    if sub:
                        lines.append(f"    {val:<12}: {_perf(sub)}")
                lines.append("")

    # -------------------------------------------------------------- locked profit stops (wins — TP hit, then runner stopped out)
    locked_stops = [t for t in trades if t.get("reason") == "locked_profit_stop" and "stop_verdict" in t]
    if locked_stops:
        still_rising = [t for t in locked_stops if t["stop_verdict"] == "still_rising"]
        reversed_    = [t for t in locked_stops if t["stop_verdict"] == "reversed"]
        flat         = [t for t in locked_stops if t["stop_verdict"] == "flat"]
        n = len(locked_stops)
        lines += [
            "--- LOCKED PROFIT STOPS (TP hit first, then runner closed at locked stop) ---",
            f"  Count: {n}  — exit price = locked TP price (all should be winners)",
            f"  Still rising (closed >1% above exit):  {len(still_rising):>4}  ({len(still_rising)/n:5.0%})  <- stop may need more room",
            f"  Reversed     (closed >1% below exit):  {len(reversed_):>4}  ({len(reversed_)/n:5.0%})  <- good timing",
            f"  Flat         (closed within 1%):       {len(flat):>4}  ({len(flat)/n:5.0%})",
            "",
            f"  {'Symbol':<8} {'Date':<12} {'Exit':>8} {'DayClose':>9} {'Move':>7}  Verdict",
            "  " + "-" * 56,
        ]
        for t in sorted(locked_stops, key=lambda x: x.get("post_stop_move_pct", 0), reverse=True):
            move = t.get("post_stop_move_pct", 0)
            lines.append(
                f"  {t['symbol']:<8} {t.get('closed_at',''):<12} "
                f"${t['exit']:>7.2f} ${t.get('stop_close', 0):>8.2f} "
                f"{move:>+6.1%}  {t['stop_verdict']}"
            )
        lines.append("")

    # -------------------------------------------------------------- cycle log summary
    buys = [e for e in cycle_log if e.get("action") == "BUY"]
    closes = [e for e in cycle_log if e.get("action") == "CLOSE"]
    stops = [e for e in cycle_log if e.get("action") in ("stop_loss", "locked_profit_stop", "flatten_all")]
    tp_locks = [e for e in cycle_log if e.get("action") == "tp_lock"]

    lines += [
        "--- CYCLE SUMMARY ---",
        f"  BUY signals:      {len(buys):>6}",
        f"  CLOSE signals:    {len(closes):>6}",
        f"  Stops hit:        {len(stops):>6}",
        f"  TP locks (runners):{len(tp_locks):>5}",
        "",
    ]

    # -------------------------------------------------------------- deep scorer
    if deep_score_runs:
        lines.append("--- DEEP SCORER RUNS ---")
        for d in deep_score_runs:
            lines.append(f"  {d}")
        lines.append("")

    # -------------------------------------------------------------- decisions log analysis
    decisions_log = results.get("decisions_log", [])
    if decisions_log:
        buy_decisions = [r for r in decisions_log if r.get("action") == "BUY"]
        # Build per-symbol sorted trade list (closed_at, pnl) so we can match
        # each BUY decision to the first trade that closed on or after the decision date.
        # One position per symbol at a time means the first trade closing >= entry date is the right one.
        _sym_trades: dict[str, list] = {}
        for t in trades:
            sym = t["symbol"]
            _sym_trades.setdefault(sym, []).append(
                (str(t.get("closed_at", ""))[:10], float(t.get("pnl", 0)))
            )
        for sym in _sym_trades:
            _sym_trades[sym].sort()  # sort by closed_at ascending

        def _lookup_pnl(symbol: str, decision_date: str) -> "float | None":
            for close_date, pnl in _sym_trades.get(symbol, []):
                if close_date >= decision_date:
                    return pnl
            return None

        # ---- Section A: indicator sub-score hit rates ----
        _sub_scores = [
            ("rsi_score",   "RSI"),
            ("macd_score",  "MACD"),
            ("trend_score", "Trend"),
            ("bb_score",    "BB"),
            ("obv_score",   "OBV"),
            ("vwap_score",  "VWAP"),
            ("fib_score",   "Fib"),
        ]

        lines += [
            "--- INDICATOR SUB-SCORE HIT RATES ---",
            "  (BUY decisions matched to completed trades by symbol+date)",
            "",
            f"  {'Indicator':<12} {'Bucket':<14} {'Trades':>6}  {'WinRate':>7}  {'Avg P&L':>9}",
            "  " + "-" * 54,
        ]
        for _sk, _sl in _sub_scores:
            matched = []
            for r in buy_decisions:
                sv = r.get(_sk)
                if sv is None:
                    continue
                pnl = _lookup_pnl(r["symbol"], r["date"])
                if pnl is not None:
                    matched.append((float(sv), pnl))
            if not matched:
                continue
            for _bkt_label, _bkt_fn in [
                ("bullish (>0.6)", lambda v: v > 0.6),
                ("neutral",        lambda v: 0.4 <= v <= 0.6),
                ("bearish (<0.4)", lambda v: v < 0.4),
            ]:
                subset = [(sv, pnl) for sv, pnl in matched if _bkt_fn(sv)]
                if not subset:
                    continue
                _wins = sum(1 for _, pnl in subset if pnl > 0)
                _wr = _wins / len(subset)
                _avg = sum(pnl for _, pnl in subset) / len(subset)
                lines.append(
                    f"  {_sl:<12} {_bkt_label:<14} {len(subset):>6}  {_wr:>6.0%}  ${_avg:>+8,.0f}"
                )
        lines.append("")

        # Fib ratio breakdown (which specific level performs best)
        fib_buys_with_pnl = []
        for r in buy_decisions:
            fv = r.get("fib_score")
            fr = r.get("fib_ratio")
            if fv is None or fr is None:
                continue
            pnl = _lookup_pnl(r["symbol"], r["date"])
            if pnl is not None:
                fib_buys_with_pnl.append((float(fr), float(fv), pnl))
        if fib_buys_with_pnl:
            lines += [
                "  Fib level breakdown (positive fib_score BUY entries):",
                f"  {'Ratio':<8} {'Trades':>6}  {'WinRate':>7}  {'Avg P&L':>9}",
                "  " + "-" * 35,
            ]
            _ratio_buckets: dict[str, list[float]] = {}
            for fr, fv, pnl in fib_buys_with_pnl:
                if fv <= 0:
                    continue
                label = f"{fr*100:.1f}%"
                _ratio_buckets.setdefault(label, []).append(pnl)
            for rl in sorted(_ratio_buckets.keys(), key=lambda x: float(x.rstrip("%"))):
                pnls = _ratio_buckets[rl]
                _wr = sum(1 for p in pnls if p > 0) / len(pnls)
                _avg = sum(pnls) / len(pnls)
                lines.append(f"  {rl:<8} {len(pnls):>6}  {_wr:>6.0%}  ${_avg:>+8,.0f}")
            lines.append("")

        # ---- Section B: gate filter analysis ----
        buy_thresh = float((decisions_log[0].get("buy_threshold") or 0.35)) if decisions_log else 0.35
        near_thresh_holds = [
            r for r in decisions_log
            if r.get("action") == "HOLD"
            and (r.get("combined") or 0) >= buy_thresh * 0.85
            and not r.get("had_position")
        ]
        if near_thresh_holds:
            def _parse_gate(notes: str) -> str:
                n = str(notes)
                if "vetoed" in n:
                    return "deep_score_veto"
                if "gap_up_blocked" in n:
                    return "gap_up_blocked"
                if "vol_low" in n:
                    return "vol_low"
                if "circuit_breaker" in n:
                    return "circuit_breaker"
                if "filtered:weak" in n:
                    return "weak+adverse_regime"
                if "blocked:stopped" in n:
                    return "re-entry_stop_limit"
                if notes.strip():
                    return "other_filter"
                return "near_miss"

            _gate_buckets: dict[str, list] = {}
            for r in near_thresh_holds:
                g = _parse_gate(r.get("gate_notes", ""))
                _gate_buckets.setdefault(g, []).append(r)

            lines += [
                "--- GATE FILTER ANALYSIS ---",
                f"  Near-threshold HOLDs (combined >= {buy_thresh*0.85:.3f}, no position): {len(near_thresh_holds)}",
                "",
                f"  {'Gate':<24} {'Count':>5}  {'AvgFwd5d':>8}  {'Profitable%':>11}",
                "  " + "-" * 54,
            ]
            for g, rows in sorted(_gate_buckets.items(), key=lambda x: -len(x[1])):
                fwd = [r["fwd_5d_return"] for r in rows if r.get("fwd_5d_return") is not None]
                avg_fwd = sum(fwd) / len(fwd) if fwd else None
                pct_pos = sum(1 for f in fwd if f > 0.02) / len(fwd) if fwd else None
                avg_s = f"{avg_fwd:>+7.1%}" if avg_fwd is not None else "     n/a"
                pct_s = f"{pct_pos:>10.0%}" if pct_pos is not None else "        n/a"
                lines.append(f"  {g:<24} {len(rows):>5}  {avg_s}  {pct_s}")
            lines.append("")

        # ---- Section C: top missed opportunities ----
        hold_with_fwd = [
            r for r in decisions_log
            if r.get("action") == "HOLD"
            and r.get("fwd_5d_return") is not None
            and not r.get("had_position")
        ]
        if hold_with_fwd:
            top_misses = sorted(hold_with_fwd, key=lambda r: r.get("fwd_5d_return", 0), reverse=True)[:10]
            lines += [
                "--- TOP MISSED OPPORTUNITIES (best 5-day return after a HOLD) ---",
                f"  {'Symbol':<8} {'Date':<12} {'Cycle':<6} {'Combined':>8}  {'Gate':<28}  {'Fwd5d':>6}",
                "  " + "-" * 72,
            ]
            for r in top_misses:
                gate = _parse_gate(r.get("gate_notes", "")) if near_thresh_holds else r.get("gate_notes", "")[:25]
                lines.append(
                    f"  {r['symbol']:<8} {r['date']:<12} {r['cycle']:<6} "
                    f"{(r.get('combined') or 0):>+7.3f}  {gate:<28}  {r['fwd_5d_return']:>+5.1%}"
                )
            lines.append("")

    # -------------------------------------------------------------- equity curve (sampled)
    if equity_curve:
        step = max(1, len(equity_curve) // 20)
        sample = equity_curve[::step]
        if equity_curve[-1] not in sample:
            sample.append(equity_curve[-1])
        lines.append("--- EQUITY CURVE (sampled) ---")
        lines.append(f"  {'Date':<12} {'Equity':>12} {'Pos':>5}")
        for snap in sample:
            lines.append(
                f"  {snap['date']:<12} ${snap['equity']:>10,.0f} {snap['positions']:>5}"
            )
        lines.append("")

    lines.append("=" * 62)
    lines.append("")
    lines.append(
        "NOTE: yfinance `info` fields (fundamentals, analyst targets) reflect\n"
        "      CURRENT values, not historical snapshots — a known limitation\n"
        "      of the deep scorer in backtest mode. Technical/price data is\n"
        "      correctly date-sliced. News uses NewsAPI with date-range filter."
    )
    lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ helpers

def _daily_returns(equity_curve: list[dict], starting_cash: float) -> list[float]:
    rets: list[float] = []
    prev = starting_cash
    for snap in equity_curve:
        eq = float(snap["equity"])
        if prev > 0:
            rets.append((eq - prev) / prev)
        prev = eq
    return rets


def _sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    try:
        mean = statistics.mean(daily_returns)
        std = statistics.stdev(daily_returns)
        if std <= 0:
            return 0.0
        ann = mean / std * (252 ** 0.5)
        return ann if isfinite(ann) else 0.0
    except Exception:
        return 0.0


def _max_drawdown(equity_curve: list[dict], starting_cash: float) -> tuple[float, str, str]:
    peak = starting_cash
    max_dd = 0.0
    peak_date = dd_from = dd_to = ""
    for snap in equity_curve:
        eq = float(snap["equity"])
        d = snap["date"]
        if eq >= peak:
            peak = eq
            peak_date = d
        dd = (eq - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_from = peak_date
            dd_to = d
    return max_dd, dd_from, dd_to


def _fmt_pf(pf: float) -> str:
    if not isfinite(pf):
        return "inf (no losses)"
    return f"{pf:.2f}"
