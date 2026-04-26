# DASHBOARD BUGS — Full Audit
*Conducted: 2026-04-26 | Three parallel agents | Every line read*

Three agents covered: (1) dashboard.py display/calculation logic, (2) data producer schemas vs. dashboard expectations, (3) backtester/reporter metrics pipeline. Use checkboxes to track fixes.

---

## TIER 1 — CRITICAL (Dashboard Is Actively Wrong Right Now)

- [ ] **D-C1 — EQUITY RETURN ALWAYS SHOWS 0% IN LIVE/PAPER MODE**
  - **File:** `dashboard.py` lines 620–624
  - **Issue:** `starting = 100_000.0 if MODE == "sim" else equity` — in live and paper mode, `starting` is set to the *current* equity, so `(equity / starting) - 1` always equals 0. The return percentage shown to the user is permanently 0%.
  - **Fix:** Store actual starting capital in state or config; read it from `account.starting_cash` or a config value instead of using current equity as the baseline.

- [ ] **D-C2 — DECISIONS LOG SORT IS BROKEN (STRING CONCAT, NOT DATETIME)**
  - **File:** `dashboard.py` line 295
  - **Issue:** `rows.sort(key=lambda r: r.get("date", "") + r.get("timestamp", ""), reverse=True)` — sorts by concatenating date + timestamp as a raw string. `"2024-02-10" + ""` < `"2024-02-09" + "23:59"` in string comparison, so recent dates can sort BEFORE older ones. Decision log order is wrong.
  - **Fix:** Parse to datetime for sorting: `key=lambda r: r.get("date","") + "T" + r.get("timestamp","00:00:00")` still sorts as ISO string correctly IF date is always present, OR use `datetime.fromisoformat()` with fallback.

- [ ] **D-C3 — `stop_verdict` COLUMN ALWAYS EMPTY — FIELD IS NEVER WRITTEN**
  - **File:** `dashboard.py` line 2288, `src/learning/postmortem.py` lines 178–192
  - **Issue:** Dashboard reads `pm.get("stop_verdict", "")` but `postmortem.py` never writes a `stop_verdict` field. It writes `"sentiment"` and `"tags"` instead. Every row in the Stop Verdict column will be blank forever.
  - **Fix:** Add `"stop_verdict"` computation to `postmortem.py` (derive from `close_reason` + `post_stop_move_pct`), or rename the dashboard column to match what is actually written.

- [ ] **D-C4 — BACKTEST RESULTS FILE STRIPS `decisions_log` BEFORE SAVING**
  - **File:** `src/backtester/engine.py` line 1410
  - **Issue:** `_results_slim = {k: v for k, v in results.items() if k != "decisions_log"}` — `decisions_log` is explicitly removed before writing to JSON. The dashboard cannot load or graph decision quality over time from any backtest results file. It was already written to `backtest_decisions.jsonl` on line 1414, making this removal redundant AND destructive.
  - **Fix:** Remove the filter (delete line 1410). The decisions_log should stay in the results file.

- [ ] **D-C5 — HARDCODED WINDOWS BACKSLASHES IN `backtest_history.json`**
  - **File:** `data/backtest_history.json` lines 7, 27, 46, 64
  - **Issue:** `"results_file": "data\\backtest_results_no-llm009.json"` — backslash separators. `Path("data\\file.json")` on any non-Windows system creates an invalid path. Dashboard will silently fail to load any backtest run's results file.
  - **Fix:** Use forward slashes everywhere: `"data/backtest_results_no-llm009.json"`. Fix the writer that produced these paths to use `Path.as_posix()`.

- [ ] **D-C6 — P&L BY SYMBOL CALCULATION IS WRONG**
  - **File:** `dashboard.py` lines 857–867
  - **Issue:** Calculates P&L by summing BUY side as negative cash and SELL side as positive cash. This works only if every BUY has an exact matching SELL at the same quantity. Partial fills, multiple entries, and any position that closed via stop-loss (not a SELL order) will produce a wrong P&L figure.
  - **Fix:** Calculate P&L directly from the `pnl` field in trade records, not by reconstructing from order fills.

- [ ] **D-C7 — BACKTEST HAS NO OUTCOME GRADING (LIVE AND BACKTEST METRICS ARE INCOMPARABLE)**
  - **File:** `src/backtester/engine.py` vs `src/learning/outcomes.py`
  - **Issue:** Live mode computes per-decision outcome fields: `edge`, `hit`, `max_favorable_pct`, `max_adverse_pct`, `stop_hit`, `tp_hit`, `realized_pct` (outcomes.py lines 45–79). Backtester computes none of these. Dashboard metrics that rely on these fields show nothing for backtest runs. Signal win-rate and edge statistics are live-only.
  - **Fix:** After each closed backtest trade, call the outcomes grading logic and persist results so backtest metrics match live metrics.

---

## TIER 2 — HIGH (Wrong Numbers, Silent Failures, Display Errors)

- [ ] **D-H1 — WIN RATE DISPLAY DOUBLE-SCALES ON SOME DATA SOURCES**
  - **File:** `dashboard.py` line 1323
  - **Issue:** `f"{(wr or 0)*100:.1f}%"` assumes `wr` is a decimal in [0, 1]. Some code paths write `win_rate` as an already-computed percentage (0–100). If data comes from a source that already multiplied by 100, display shows "65.0%" as "6500.0%".
  - **Fix:** Normalize all win_rate values to [0, 1] at the data loading layer, or add explicit scale detection.

- [ ] **D-H2 — NaN FROM ATR/MACD PROPAGATES INTO COMPOSITE SCORE DISPLAYED ON DASHBOARD**
  - **File:** `src/analysis/technicals.py` lines 55–60, 254, 334–346
  - **Issue:** ATR `.shift()` creates NaN in row 1. MACD score receives NaN. `_fib_score()` can return `None` appended to `sub_scores`. `np.mean([..., None])` = NaN. Dashboard displays NaN scores as blank or `-` while the bot is still making trade decisions based on them.
  - **Fix:** `.fillna(0)` on ATR output; guard `np.isfinite(macd_cross)` before MACD score; only append fib sub_score if `is not None`.

- [ ] **D-H3 — SHARPE RATIO MISSING RISK-FREE RATE AND WRONG ANNUALIZATION**
  - **File:** `dashboard.py` line 691, `src/backtester/reporter.py` line 489
  - **Issue:** (1) Sharpe formula assumes Rf = 0, inflating all Sharpe values. (2) Annualization multiplies by `sqrt(252)` regardless of how many trading days are in the window — a 30-day backtest gets annualized at the same rate as a 252-day one, making short backtests look artificially volatile or smooth.
  - **Fix:** Subtract risk-free rate (configurable, default 0.04/252 per day). Use `sqrt(actual_trading_days)` not hardcoded 252.

- [ ] **D-H4 — MISSING `pnl` FIELD TREATED AS LOSS IN REPORTER WIN RATE**
  - **File:** `src/backtester/reporter.py` line 70
  - **Issue:** `losses = [t for t in trades if t.get("pnl", 0) <= 0]` — if a trade record is missing the `pnl` field, it defaults to 0 and is counted as a loss. Any partial or in-progress trade with no pnl yet will inflate the loss count.
  - **Fix:** `losses = [t for t in trades if t.get("pnl") is not None and t.get("pnl", 0) <= 0]`

- [ ] **D-H5 — ORDER SIDE MISSING DEFAULTS TO TREATING TRADE AS SELL**
  - **File:** `dashboard.py` line 834
  - **Issue:** `if str(r.get("side", "")).upper() == "BUY":` — a missing `side` field becomes `""`, which is not `"BUY"`, so the order is treated as a SELL. Any trade with a missing side adds to the sell-side cash instead of buy-side. P&L by symbol becomes wrong.
  - **Fix:** Explicitly skip rows where `side` is missing rather than defaulting them to SELL.

- [ ] **D-H6 — `best`/`worst` TRADE CALLS `max()`/`min()` ON EMPTY LIST → CRASH**
  - **File:** `dashboard.py` lines 969–970
  - **Issue:** `best = max(trades, key=lambda t: t.get("pnl", 0))` — if `trades` is empty, this raises `ValueError: max() arg is an empty sequence`. This crash is not caught and will take down the entire dashboard page.
  - **Fix:** Add guard: `if not trades: ...` before these calls (there may be a check nearby but it must be BEFORE these lines, not after).

- [ ] **D-H7 — DEEP SCORES CHAINED `.get()` CAN CRASH ON `None` VALUE**
  - **File:** `dashboard.py` lines 1191–1195
  - **Issue:** `(_bd.get("technical") or {}).get("score")` — if `breakdown["technical"]` exists but is `None`, then `None or {}` gives `{}` and `.get("score")` returns None. But if `_bd.get("technical")` returns an integer or string (bad data), `None or {}` still works but gives wrong result. Actually safe with `or {}` — BUT if the breakdown structure has `"technical": {"score": None}`, the score silently becomes None and downstream math fails.
  - **Fix:** Add explicit `isinstance` check and default to 0 for display: `float((_bd.get("technical") or {}).get("score") or 0)`.

- [ ] **D-H8 — `postmortem.py` NEVER WRITES `run_id` — BACKTEST FILTER DOES NOTHING**
  - **File:** `src/learning/postmortem.py` lines 178–192, `dashboard.py` line 2281
  - **Issue:** Dashboard filters postmortems by `run_id` with `if _sel_run_id and pm.get("run_id") and pm.get("run_id") != _sel_run_id`. Since postmortem.py never writes `run_id`, all postmortems from ALL backtest runs appear together regardless of which run is selected. There's no way to isolate postmortems for a specific backtest.
  - **Fix:** Pass `run_id` into `run_trade_postmortem()` and write it to the record.

- [ ] **D-H9 — UTF-8 BOM IN `backtest_postmortems.jsonl` DROPS FIRST ROW**
  - **File:** `data/backtest_postmortems.jsonl` line 1
  - **Issue:** File starts with UTF-8 BOM (`﻿`). The first JSON line cannot be parsed and is silently skipped. The first postmortem entry from all backtest history is invisible on the dashboard.
  - **Fix:** Strip BOM on read: `open(path, encoding="utf-8-sig")` instead of `encoding="utf-8"`. Also fix the writer to not emit BOM.

- [ ] **D-H10 — DECISIONS MATCH LOGIC IN REPORTER BREAKS WITH SAME-DAY RE-ENTRIES**
  - **File:** `src/backtester/reporter.py` lines 291–295
  - **Issue:** `_lookup_pnl()` finds the first trade closing on or after the decision date. If a symbol is bought and sold on day 1, then bought again on day 2, the day 2 BUY decision is matched to the day 1 SELL's P&L. Signal effectiveness stats are incorrectly attributed.
  - **Fix:** Match decision to trade by both symbol AND the specific position opened after that decision date, not just "first close on or after."

- [ ] **D-H11 — INDICATOR SUB-SCORE BUCKETS HAVE WRONG THRESHOLDS**
  - **File:** `src/backtester/reporter.py` lines 326–340
  - **Issue:** Buckets: bullish `> 0.6`, neutral `0.4–0.6`, bearish `< 0.4`. Scores between 0.6 and 1.0 fall in bullish bucket (correct) but the neutral band is only 0.2 wide and the bearish bucket swallows everything below 0.4 including 0.0. With tech scores on [-1, 1] scaled to [0, 1], a score of 0.3 is "barely bearish" but gets same label as -1.0.
  - **Fix:** Adjust thresholds to `> 0.5` / `0.35–0.5` / `< 0.35` or normalize scores before bucketing.

- [ ] **D-H12 — ENTRY SIGNAL PERFORMANCE: DIVISION BY ZERO ON EMPTY SUBSET**
  - **File:** `src/backtester/reporter.py` lines 191–193
  - **Issue:** `avg_p = sum(...) / len(subset)` and `wins / len(subset)` — if `subset` is empty, crashes with `ZeroDivisionError`. This can happen if a signal category (e.g., "LLM:HOLD") has no matching trades in the window.
  - **Fix:** Add `if not subset: return "  0 trades"` before these lines.

---

## TIER 3 — MEDIUM (Wrong Display, Wrong Defaults, Silent Bad Data)

- [ ] **D-M1 — RETURN UNITS INCONSISTENT ACROSS DASHBOARD (DECIMAL vs PERCENT)**
  - **File:** `dashboard.py` line 676 (returns as decimal), line 880 (`* 100` to make percent), line 1924 (`* 100` again)
  - **Issue:** Some calculations keep returns as decimals (0.05 = 5%), others multiply by 100 (5.0 = 5%). The equity curve chart and metrics cards pull from different code paths. Numbers in different panels are not comparable.
  - **Fix:** Standardize: all internal calculations in decimal [0,1]; multiply by 100 only at display/formatting layer.

- [ ] **D-M2 — REGIME LABEL BECOMES STRING `"none"` WHEN PYTHON `None` IS RETURNED**
  - **File:** `src/trading/decision_engine.py` line 499
  - **Issue:** `str(regime.get("label", "")).lower()` — if label is Python `None`, becomes the string `"none"`, which never matches `"bearish"` or `"volatile"`. Regime-based filtering and coloring on the dashboard silently does nothing during undefined regimes.
  - **Fix:** `str(regime.get("label") or "neutral").lower()`

- [ ] **D-M3 — DATE FILTER DEFAULTS TO WRONG 30-DAY WINDOW WHEN `run_start` MISSING**
  - **File:** `dashboard.py` lines 1043–1044
  - **Issue:** If `run_start` is empty or missing from the selected backtest run, the decision filter defaults to "last 30 days." But a backtest from 6 months ago would show only the most recent 30 days of its decisions, not the full run. Most data would be invisible.
  - **Fix:** Default to the run's actual date range or show all decisions if no date range is available.

- [ ] **D-M4 — COLUMN FORMAT STRINGS USE `%%` — MAY DISPLAY AS `%%`**
  - **File:** `dashboard.py` lines 1606, 1608
  - **Issue:** `format="%.0%%"` and `format="%.2f%%"` — the `%%` in Python format strings escapes to a literal `%`, but in Streamlit's `column_config.NumberColumn`, the format string uses C-style printf format where `%%` may render literally as `%%` instead of `%`. Users may see "65%%" instead of "65%".
  - **Fix:** Use `"%.0%"` (no double-percent) and verify Streamlit column_config format string spec.

- [ ] **D-M5 — NEWS CACHE INCLUDES FULL-DAY NEWS FOR INTRADAY DECISIONS (LOOKAHEAD)**
  - **File:** `src/backtester/signals.py` lines 416–421
  - **Issue:** `end_ts = datetime.combine(end_d, datetime.max.time()).timestamp()` caps news at 23:59:59 on the decision day. A 9:30 AM decision can see a 2:00 PM earnings headline from the same day. Minor per trade but compounds across hundreds of backtest decisions.
  - **Fix:** Cap `end_ts` at the actual cycle time: `end_ts = sim_datetime.timestamp()` when a full datetime is available.

- [ ] **D-M6 — MAX DRAWDOWN CRASHES WHEN `peak = 0` (ZERO STARTING EQUITY)**
  - **File:** `src/backtester/reporter.py` lines 495–510
  - **Issue:** `dd = (eq - peak) / peak if peak > 0 else 0.0` — if `starting_cash = 0`, the peak stays 0 for the entire run and all drawdowns are ignored. Dashboard shows 0% max drawdown for any account that somehow starts with $0 equity.
  - **Fix:** Initialize peak to `starting_cash` and validate `starting_cash > 0` before the calculation.

- [ ] **D-M7 — FIB RATIO LABEL CRASHES ON `None` FIB RATIO**
  - **File:** `src/backtester/reporter.py` line 362
  - **Issue:** `label = f"{fr*100:.1f}%"` — `fr` can be `None` (lines 347–348 can skip setting it). Multiplying `None * 100` raises `TypeError` and crashes the reporter mid-run.
  - **Fix:** Guard with `if fr is not None: label = f"{fr*100:.1f}%"` else `label = "N/A"`.

- [ ] **D-M8 — GATE NOTES PARSING BREAKS ON MALFORMED FORMAT**
  - **File:** `dashboard.py` lines 879–891
  - **Issue:** Parses gate status by splitting on `:` and assuming `"gate:status"` format. If gate_notes contains extra colons (e.g., a reason string like `"regime:bearish:volatile"`), the split produces wrong keys. Gate pills on the dashboard show wrong or missing statuses.
  - **Fix:** Use `split(":", 1)` to split on first colon only.

- [ ] **D-M9 — STALE INDICATOR STATS CACHED 60 SECONDS BUT UPDATED ONCE PER DAY**
  - **File:** `dashboard.py` line 251
  - **Issue:** `@st.cache_data(ttl=60)` on `load_indicator_stats()` — file is only updated EOD, so re-reading it every 60 seconds wastes I/O. More importantly, the TTL is the *maximum* freshness guarantee; the data the user sees could be from the beginning of the trading day regardless. Should match actual update cadence.
  - **Fix:** Change TTL to 3600 (1 hour) or `None` with manual cache clear at EOD.

- [ ] **D-M10 — DECISION DATE VALIDATION SILENTLY HIDES STALE DATA**
  - **File:** `dashboard.py` lines 1180–1183
  - **Issue:** Parsing `_updated` timestamp for stale data detection falls back to `_stale = False` on any exception. If the date string is in the wrong format, the dashboard never shows the stale warning even when data is days old.
  - **Fix:** Log a warning on parse failure and default `_stale = True` (fail safe rather than fail silent).

- [ ] **D-M11 — CLOSE REASON STRINGS ARE INCONSISTENT ACROSS ALL DATA**
  - **File:** `data/backtest_postmortems.jsonl` (data quality issue)
  - **Issue:** Close reason field contains wildly inconsistent formats: `"stop-loss hit @ 197.31 (stop=198.05)"`, `"stop_loss"`, `"locked_profit_stop"`, `"take-profit hit @ 127.08 (tp=125.77)"`, `"take_profit"`. Dashboard displays raw strings — users see cluttered, inconsistent text in the table. Pattern matching on close_reason (for grouping, filtering, or coloring) will miss variants.
  - **Fix:** Normalize to a canonical enum set (`"stop_loss"`, `"take_profit"`, `"trailing_stop"`, `"signal"`, `"eod"`, etc.) at write time. Store detail in a separate `close_detail` field.

- [ ] **D-M12 — TRADE RECORD MISSING ENTRY SIGNAL FIELDS IN SOME PATHS**
  - **File:** `src/backtester/broker.py` lines 229–232
  - **Issue:** Fields `entry_tech`, `entry_news`, `entry_breadth`, `entry_llm`, `entry_combined`, `entry_quality`, `entry_regime`, `entry_trend` are only written if present in the position's tags. If a position was opened without tags (e.g., queued buy, forced entry), these fields are absent. Dashboard panels that display per-signal trade breakdown will silently skip these trades.
  - **Fix:** Always write these fields with a default of `None` so dashboard can show "N/A" instead of hiding the trade entirely.

---

## TIER 4 — LOW (Bad Defaults, Dead Code, Fragile Paths)

- [ ] **D-L1 — GRADE BADGE DEFAULTS TO `C` STYLING FOR UNKNOWN GRADES**
  - **File:** `dashboard.py` line 162
  - **Issue:** `css = _GRADE_CSS.get(str(g).upper(), "g-c")` — any unrecognized grade (e.g., `"A-"`, `"B+"`, `"E"`, `None`) silently renders as a grade C badge. User sees misleading quality indicator.
  - **Fix:** Default to `"g-unknown"` or a gray badge CSS class and add it to the grade map.

- [ ] **D-L2 — `outcomes.jsonl` IS EMPTY — ALL LIVE TRACK RECORD DATA MISSING**
  - **File:** `data/outcomes.jsonl`
  - **Issue:** File is 0 bytes. Dashboard reads this for per-ticker live performance history. No live trading has occurred to populate it. All "Track Record" panels on the live dashboard will show nothing. This is expected for a new bot, but the dashboard should show a friendly "no live data yet" message instead of blank panels.
  - **Fix:** Add empty-state handling in the track record display section.

- [ ] **D-L3 — RELATIVE PATHS BREAK IF BOT IS STARTED FROM NON-ROOT DIRECTORY**
  - **File:** `src/backtester/engine.py` lines 48–50, `src/learning/signal_weights.py` line 253
  - **Issue:** `_BT_FINGERPRINTS = "data/backtest_fingerprints.jsonl"` and similar — hardcoded relative paths. If the bot is launched from `src/` instead of the project root, all data files are written to `src/data/` and the dashboard (running from root) can't find them.
  - **Fix:** Use `Path(__file__).parent.parent / "data" / "filename"` to make paths relative to the module, not the working directory.

- [ ] **D-L4 — SESSION STATE KEY `_bt_mtime` CAN COLLIDE ACROSS TABS**
  - **File:** `dashboard.py` line 569
  - **Issue:** `st.session_state["_bt_mtime"]` is a global key with no namespace. If user opens two dashboard tabs in the same browser session, they share this state and trigger cross-tab reruns.
  - **Fix:** Namespace the key: `st.session_state[f"_bt_mtime_{run_id}"]`.

- [ ] **D-L5 — ARCHIVER CUTOFF HARDCODED TO 365 DAYS, NOT CONFIGURABLE**
  - **File:** `src/dashboard/archiver.py` line 53
  - **Issue:** `return date.today() - timedelta(days=365)` — retention window is hardcoded. If the user wants 2 years of backtest history, they have to edit source code.
  - **Fix:** Read from config: `cfg.get("dashboard", {}).get("retention_days", 365)`.

- [ ] **D-L6 — ARCHIVER KEEPS CORRUPTED JSON LINES IN ARCHIVE**
  - **File:** `src/dashboard/archiver.py` line 71
  - **Issue:** `kept.append(raw)` when JSON decode fails — the bad line is preserved so it will fail again on every future archive read. Silent accumulation of corrupt data.
  - **Fix:** Log the bad line and skip it rather than keeping it.

- [ ] **D-L7 — BACKTEST NEWS LOOKBACK HARDCODED TO 3 DAYS VS CONFIG IN LIVE**
  - **File:** `src/backtester/signals.py` line 522
  - **Issue:** `lookback_days: int = 3` hardcoded in backtest. Live news uses config-driven lookback. If the config is changed for live trading, backtest and live diverge silently — you can't trust the backtest to reflect what live will actually do.
  - **Fix:** `lookback_days: int = int(cfg.get("news", {}).get("lookback_days", 3))`

- [ ] **D-L8 — NO ATOMIC WRITES ON `outcomes.jsonl`**
  - **File:** `src/learning/outcomes.py` lines 92–94
  - **Issue:** Writes line by line with `f.write(...)`. If the process crashes mid-write, the file ends with a partial JSON line. Next read will fail on that line and silently skip it (or crash).
  - **Fix:** Accumulate all rows, write to a `.tmp` file, then `os.replace(tmp, target)` for atomic swap.

---

## Root Cause Summary

| Category | Count | Worst Example |
|---|---|---|
| Schema mismatch (dashboard expects field that's never written) | 3 | `stop_verdict` column permanently empty |
| Wrong calculation / wrong formula | 6 | Equity return always 0%, Sharpe no Rf rate |
| Silent None/NaN propagation | 5 | NaN composite scores shown as valid numbers |
| Data produced in wrong format | 4 | Windows backslashes in JSON, BOM in JSONL |
| Display unit inconsistency (% vs decimal) | 3 | Win rate could show 6500% |
| Missing data / empty files | 3 | outcomes.jsonl empty, decisions_log empty |
| Hardcoded paths / fragile defaults | 5 | Relative paths break on non-root launch |
| **Total** | **29** | |

---

## Fix Order (Fastest Impact First)

1. **D-C1** — Equity return shows 0% (one-line fix, visible immediately)
2. **D-C2** — Decision log sort broken (one-line fix)
3. **D-C3** — `stop_verdict` column empty (add field to postmortem writer)
4. **D-H9** — BOM drops first postmortem row (change encoding to `utf-8-sig`)
5. **D-C5** — Windows backslash paths break backtest loading (fix paths in history JSON)
6. **D-C4** — decisions_log stripped from results file (delete one line in engine.py)
7. **D-H1** — Win rate double-scaling (add normalization at load)
8. **D-H6** — Dashboard crash on empty trades list (add empty guard)
9. **D-M4** — Column format `%%` bug (fix format strings)
10. **D-H3** — Sharpe ratio formula (add Rf, fix annualization window)
