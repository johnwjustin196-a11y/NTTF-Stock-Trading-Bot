# Stock Bot Changelog

Each block is date/time-stamped. Use the block timestamp to correlate against
backtest or live-run results. To undo a block, reverse the listed changes.

---

## [2026-04-27 | Session P] Minimum 4% Stop Floor

**Purpose:** Give new positions more room before stops trigger by preventing default stop-loss placement tighter than 4%.

**Modified files:**
- `config/settings.yaml` - changed `trading.stop_loss_pct` to `0.04` and added `trading.stop_loss_min_pct: 0.04`.
- `src/trading/position_manager.py` - applies the minimum stop floor to fixed stops, dynamic stops, ATR-widened stops, and fallback stop checks for older positions without stored stop metadata. If `stop_loss_max_pct` is accidentally set below the minimum floor, the minimum floor wins.
- `tests/test_smoke.py` - added coverage for tight dynamic stops widening to 4% and fixed/fallback stop checks respecting the 4% floor.

**Behavior note:** With the current config, initial stops will generally land between 4% and 5% below entry: tight chart-based stops widen to 4%, while very wide dynamic stops still clamp at the existing 5% max.

**Verification:** `python -m compileall -q src tests dashboard.py scripts`; `pytest tests/test_smoke.py -q --ignore=tmp` passed (21 tests, 2 existing warnings); `git diff --check` passed with CRLF normalization warnings only.

**Revert:** Remove `stop_loss_min_pct` from `config/settings.yaml`; restore `stop_loss_pct` to its prior value; remove `_stop_loss_pcts()` and the minimum-floor widening/fallback usage from `src/trading/position_manager.py`; remove the two minimum-stop tests.

---

## [2026-04-27 | Session O] Entry Queue Observability

**Purpose:** Make live and backtest queue behavior auditable so queued tickers can be reviewed and tuned instead of disappearing into a silent side path.

**Modified files:**
- `src/trading/entry_queue.py` - writes persistent queue lifecycle events to `data/queue_cache/queue_history.jsonl`: queued, replaced, triggered/rescored, fired, fire-skipped, cancelled, expired, restart-dropped, EOD never-triggered, and processing errors. Fired events include rescore, threshold, rescore type, order status, fill price, and quantity when available.
- `src/trading/decision_engine.py` - queued buys now return the broker order object when one is placed, and LLM-close queue cancellations are logged with a cancellation reason.
- `src/scheduler.py` - entry monitor now returns the queued-buy order result back into queue logging instead of discarding it.
- `src/backtester/entry_queue.py` - records backtest queue processing errors in the in-memory queue history.
- `dashboard.py` - adds a live Entry Queue History table on Positions & Orders and a Backtest Queue tab in the trade journal, with event counts and per-symbol lifecycle rows.

**What this shows:** You can now see whether a stock was queued, replaced, touched the trigger, failed the rescore, placed an order, skipped execution, expired, or never triggered by EOD. This should make it much easier to tune queue thresholds and Fib proximity settings.

**Verification:** `python -m compileall -q src tests dashboard.py scripts`; `pytest tests/test_smoke.py -q --ignore=tmp` passed (19 tests, 2 existing warnings); `git diff --check` passed with CRLF normalization warnings only.

**Revert:** Remove the queue history writes from `src/trading/entry_queue.py`; restore `_place_queued_buy()` and the scheduler entry monitor to discard order results; remove queue error history from `src/backtester/entry_queue.py`; remove `QUEUE_HISTORY_FILE`, queue loader/table helpers, live queue panel, and backtest Queue tab from `dashboard.py`.

---

## [2026-04-27 | Session N] Backtest Entry Queue Simulation

**Purpose:** Make the backtester simulate the live bot's deferred entry queue without touching live queue files.

**New files:**
- `src/backtester/entry_queue.py` - in-memory backtest queue that mirrors live support-bounce and resistance-breakout trigger checks, uses backtest-safe technical/news/LLM rescoring, expires entries at simulated EOD, and records queue history in results.

**Modified files:**
- `src/backtester/engine.py` - creates a `BacktestEntryQueue` per run; routes backtest HOLD/BUY setups into the queue using the same Fib support/resistance rules as live; replays queue checks between decision cycles on simulated intraday timestamps; sends queue-triggered buys through normal backtest sizing/execution; includes `entry_queue_log` in results.
- `src/trading/decision_engine.py` - fixed the live resistance-queue proximity lookup to use `fib_proximity_pct`, so the live resistance queue path matches the technical signal schema.
- `tests/test_smoke.py` - added an in-memory queue bounce-trigger smoke test.

**Behavior note:** The live scheduler checks every 5 minutes, but the backtest market cache is 15-minute intraday bars. The simulator defaults to 15-minute queue checks so it does not repeatedly rescore the same candle; `entry_queue.backtest_monitor_interval_minutes` can override this.

**Verification:** `python -m compileall -q src tests dashboard.py scripts`; `pytest tests/test_smoke.py -q --ignore=tmp` passed (19 tests, 2 existing warnings); `git diff --check` passed with CRLF normalization warnings only.

**Revert:** Remove `src/backtester/entry_queue.py`; remove `BacktestEntryQueue` creation, queue routing, queue monitor calls, and `entry_queue_log` from `src/backtester/engine.py`; restore the old Fib proximity lookup in `src/trading/decision_engine.py`; remove the queue smoke test.

---

## [2026-04-27 | Session M] Backtest Integrity, Queue Sizing, Dashboard Data Fixes

**Purpose:** Implement the pending live/backtest/dashboard fixes: prevent NaN score poisoning, make backtest news and forward-return calculations time-safe, carry deep-score sizing through queued buys, harden sizing/quote validation, and repair dashboard/reporting data mismatches.

**Modified files:**
- `src/analysis/deep_scorer.py` - renormalizes composite deep scores across only the dimensions actually returned by the LLM so missing dimensions no longer inflate or deflate scores.
- `src/analysis/technicals.py` - guards ATR, MACD, SMA trend, Bollinger, OBV, Fib, ROC, RS/ETF, and composite averaging against NaN/inf values; averages only finite sub-scores.
- `src/backtester/signals.py` - caps date-only backtest news at the 09:30 cycle instead of end of day; intraday cycles remain capped at their exact simulated timestamp.
- `src/backtester/engine.py` - separates daily and intraday news cache keys; computes forward returns using five actual trading days; uses neutral regime fallback; validates quote prices before buys; uses configured trailing-stop percent; preserves `decisions_log` in normal results JSON; writes archive paths with POSIX separators; passes `run_id` into backtest postmortems.
- `src/trading/entry_queue.py` - expires queued entries at New York market close with DST handling; stores and forwards `deep_size_mult` for queued buys.
- `src/trading/decision_engine.py` - uses neutral regime fallback; rejects invalid/non-positive quotes before live/queued buy sizing; applies queued-buy `deep_size_mult`.
- `src/trading/position_manager.py` - rejects invalid price/equity/buying power and invalid percentage config before sizing.
- `src/broker/alpaca_broker.py` - writes `state.json` atomically via temp file plus `os.replace()`.
- `src/learning/postmortem.py` - writes `run_id` and `stop_verdict`, and reads JSONL with `utf-8-sig`.
- `dashboard.py` - fixes live/paper return display, parsed datetime decision sorting, backtest path normalization, BOM-safe JSONL reads, win-rate unit normalization, missing-pnl/side handling, Sharpe risk-free/actual-window calculation, and postmortem stop-verdict display.
- `src/dashboard/archiver.py` and `src/backtester/reporter.py` - skip missing-PnL trades in win/loss stats, use risk-free adjusted Sharpe with actual backtest length, and normalize archived result paths.

**Verification:** `python -m compileall -q src tests dashboard.py scripts`; `pytest tests/test_smoke.py -q --ignore=tmp` passed (18 tests, 2 existing warnings); `git diff --check` passed with CRLF normalization warnings only.

**Revert:** Restore the previous scoring/composite logic in `deep_scorer.py` and `technicals.py`; restore date-keyed news cache and calendar-day forward return in `backtester/engine.py`; remove `deep_size_mult` from queue entries and queued buys; restore direct `state.json` writes; revert dashboard/reporting stat and path handling.

---

## [2026-04-27 | Session L] Safety Review Fixes

**Purpose:** Fix critical live-order state drift and several review findings from the full-project audit: unconfirmed Alpaca fills, EOD queue crashes, disabled small-cap screening, downtrend exit blocking, live queue mutation during backtests, stale queue config paths, dead `pandas_ta` safety checks, and swallowed live-stop failures.

**Modified files:**
- `src/broker/alpaca_broker.py` — market orders now poll Alpaca and update local state only when a confirmed filled quantity and average fill price are returned; partial fills update only the confirmed quantity; unconfirmed accepted/rejected/canceled orders no longer synthesize quote fills. Native stop-order placement now returns success/failure, records `stop_order_status`, and raises on failed stop placement.
- `src/trading/entry_queue.py` — fixed invalid EOD close-price formatting; queue trigger threshold now reads `trading.buy_threshold`; restart validation now reads `entry_queue.queue_rescore_min` with legacy fallback.
- `src/learning/reflection.py` — fixed invalid queue-history close-price formatting in the LLM reflection prompt.
- `src/screener/pre_market.py` — removed the hard-coded `$15` price floor that blocked all configured small-cap candidates; replaced optional `pandas_ta` ATR volatility filter with local ATR calculation.
- `src/trading/position_manager.py` — replaced optional `pandas_ta` ATR stop-floor logic with local ATR calculation and re-applies the configured max stop cap after ATR widening.
- `src/trading/decision_engine.py` — downtrend blocks now apply only to new BUY entries, allowing held positions to continue through close/review/urgent-news logic; queued buys now use regime-aware `max_positions`; confirmed Alpaca partial fills are treated as real entry fills for stop metadata; failed stop placement is logged as an unprotected live position.
- `src/backtester/engine.py` — removed imports/calls to the live `entry_queue`, preventing backtests from firing or expiring `data/queue_cache/entry_queue.json`.
- `tests/test_smoke.py` — made journal roundtrip use a project-local temp directory for sandboxed runs; made the unclamped dynamic-stop test explicitly disable `stop_loss_max_pct` so it does not depend on the active trading config.

**Verification:** `python -m compileall -q src tests dashboard.py scripts`; `pytest tests/test_smoke.py -q --ignore=tmp` passed (18 tests).

**Revert:** Restore the previous order-fill block in `alpaca_broker.py`; restore the old queue threshold lookups/format strings; re-add the screener `$15` floor and `pandas_ta` imports; restore the early downtrend return in `decision_engine.py`; re-add the live `entry_queue` import/calls in `backtester/engine.py`.

---

## [2026-04-25 | Session K3] Indicator Score Normalization + All-Decision Tracking

**Purpose:** Normalize all indicator sub-scores from [-1, 1] to [0, 1] for clean dashboard analysis; populate `fwd_5d_return` for BUY and SELL decisions (not just HOLDs); update report buckets and dashboard split threshold to match new scale; purge legacy data files.

**Modified files:**
- `src/analysis/technicals.py` — added `_n = lambda s: (s+1)/2` helper; wrapped all sub-scores in `details` dict with `_n()`: rsi_score, macd_score, trend_score, bb_score, obv_score, vwap_score, fib_score, roc_score, rs_etf_score. Raw `sub_scores` list and composite `score` stay in [-1,1] for the engine.
- `src/backtester/engine.py` — removed `action != "HOLD"` filter from fwd_5d_return enrichment loop; now computes forward return for ALL decision types using `current_price` as baseline.
- `src/backtester/reporter.py` — updated indicator bucket labels/thresholds: bullish >0.6, neutral 0.4-0.6, bearish <0.4 (was: pos >0.2, neutral ±0.2, neg <-0.2).
- `dashboard.py` — changed `compute_bt_indicator_stats` split from median to explicit 0.5; updated bar chart labels to "Bullish (>=0.5)" / "Bearish (<0.5)"; scatter vline hardcoded at x=0.5.

**Data purged (fresh start):**
- `data/indicator_outcomes.jsonl` — truncated
- `data/indicator_stats.json` — reset to `{}`
- `data/backtest_decisions.jsonl` — truncated
- `data/archive/backtest_decisions_master.jsonl` — truncated
- `data/archive/indicator_outcomes_master.jsonl` — truncated (if existed)
- `data/archive/indicator_stats_history.jsonl` — truncated (if existed)

**Revert:** Restore `_n()` calls to raw values in `technicals.py` details dict; re-add `action != "HOLD"` guard in `engine.py:751`; revert reporter bucket lambdas; revert dashboard threshold back to median split.

---

## [2026-04-25 | Session K2] Indicators Page Overhaul

**Purpose:** Replaced empty indicator page (which relied on `indicator_stats.json`, currently empty) with a full computation engine for backtests. Now computes per-indicator effectiveness directly from `backtest_decisions_master.jsonl` and shows win %, avg return, above/below median signal split, correlation, scatter plot, and by-action breakdown. Live bot indicators remain wired to `indicator_stats.json` (populated at EOD reflection after each live session).

**Modified files:**
- `dashboard.py` — added `compute_bt_indicator_stats()` and `_render_indicator_tab()` helpers; rewrote `page_indicators()` to branch on live vs backtest; backtest page computes stats on-the-fly from decisions master; added "All Runs" vs "Selected Run" scope selector; summary table across all indicators; removed `_render_entry_scatter()` (subsumed into per-tab scatter)

**Revert:** Restore previous `page_indicators()` (lines ~1312-1419 before this session) and remove `compute_bt_indicator_stats()` and `_IND_SCORE_KEYS` / `_pearson_simple()` helpers.

---

## [2026-04-25 | Session K] Complete Dashboard Overhaul

**Purpose:** Replaced the 5-tab dashboard with a sidebar-nav architecture featuring a Live Bot ↔ Backtester mode toggle. Added compounding history (365-day retention for live data, permanent for backtest), archive infrastructure, and full visibility into every signal, gate, indicator, decision, and lesson.

**New files:**
- `src/dashboard/__init__.py` — package marker
- `src/dashboard/archiver.py` — `archive_live_eod()` and `archive_backtest_run()` functions; appends transient logs to permanent master archives, prunes live data at 365 days, never prunes backtest data

**Modified files:**
- `dashboard.py` — complete rewrite (2,129 lines); sidebar nav, Live/Backtest toggle, 8 pages per mode, auto-reload on backtest completion
- `src/learning/reflection.py` — calls `archive_live_eod()` at EOD (after line 137)
- `src/backtester/engine.py` — calls `archive_backtest_run()` after each run (after line 1436)

**New data files created at runtime:**
- `data/archive/decisions_master.jsonl` — all live decisions (365-day retention)
- `data/archive/outcomes_master.jsonl` — all live outcomes
- `data/archive/indicator_outcomes_master.jsonl` — per-indicator outcome history
- `data/archive/indicator_stats_history.jsonl` — daily snapshots of indicator stats
- `data/archive/lessons_master.md` — all lessons (365-day retention)
- `data/archive/backtest_decisions_master.jsonl` — all backtest decisions (permanent)
- `data/backtest_history.json` — one-line summary per backtest run (permanent)

**Dashboard pages (Live Bot):** Home | Positions & Orders | Signals & Gates | Decisions Log | Deep Scores | Indicators | Rules & Learning | Lessons & Reflections

**Dashboard pages (Backtester):** Run History | Equity Curve | Trade Journal | Signals & Gates | Decisions Log | Deep Scores | Indicators | Lessons

**Revert:** `git checkout dashboard.py src/learning/reflection.py src/backtester/engine.py` and delete `src/dashboard/`

---

## [2026-04-24 | Session J] Deep Score Backfill — Fix False-Skip Bug

**Purpose:** The backfill's "already cached?" check used `ds_cache.get(sym, snap)` which
returns any entry within MAX_GAP_DAYS (40) of the snap date. So the Oct 27 entry
satisfied the check for Nov 26 (30-day gap < 40), and the backfill skipped writing a
Nov 26 entry. When the backtest ran on Dec 12, the nearest entry was Oct 27 — 46 days
back — which exceeded MAX_GAP_DAYS and triggered live scoring for all 198 symbols.

**Fix:** Changed the skip check to `has_near(sym, snap, tolerance_days=interval//2)`.
With a 31-day interval, tolerance is 15 days. Oct 27 is 30 days from Nov 26 → not near
→ Nov 26 is now actually scored and stored. Must re-run `scripts/run_backfill.bat`
after this change to populate the missing interval entries.

### Changes

#### `src/backtester/deep_score_cache.py`
- **ADDED** `has_near(symbol, target_date, tolerance_days=7)` method — returns True only
  if a stored entry falls within tolerance_days of the target, not within MAX_GAP_DAYS
  - Revert: remove the `has_near()` method

#### `scripts/backfill_deep_scores.py`
- **CHANGED** skip check: `ds_cache.get(sym, snap) is not None` → `ds_cache.has_near(sym, snap, tolerance_days=args.interval // 2)`
  - Revert: change back to `if not args.force and ds_cache.get(sym, snap) is not None:`

---

## [2026-04-24 | Session I] Deep Score — Live-Trigger Diagnostics + ETF Permanent Skip

**Purpose:** When the backtester triggered a live deep score during a run (e.g., on
day 12/12 of a 90-day run), there was no log explaining WHY the cache missed for each
symbol. Added per-symbol diagnostic logging and ETF permanent skip so the cause is
always visible and ETFs never waste time going through the scorer.

**Impact on tests:** No change to scores or trade logic. ETF symbols (SPY, QQQ, IWM,
XLK, XLF, etc.) will be silently skipped from deep scoring rather than making a live
call that returns "unavailable" each time. Log lines prefixed `[deep_score] LIVE SCORE
TRIGGERED:` will appear for any symbol that goes live, with full miss reason detail.

### Changes

#### `src/backtester/deep_score_cache.py`
- **ADDED** `miss_reason(symbol, sim_date)` method — returns a human-readable string
  explaining why `get()` returns None: "no cache entries", "all entries after sim_date",
  or "gap Nd > 40d limit (best entry=YYYY-MM-DD)"
  - Revert: remove the `miss_reason()` method
- **CHANGED** module docstring: updated stale-day references from 31 to 40 (cosmetic)
  - Revert: change 40 back to 31 in the docstring

#### `src/backtester/engine.py`
- **ADDED** `_perm_skip` set initialized from `signals.breadth.index_symbols` and
  `signals.breadth.sectors` config keys — ETFs in this set are never added to the
  stale list and never trigger live deep scoring
  - Revert: remove the `_brd_cfg` / `_perm_skip` init block (3 lines)
- **CHANGED** stale list comprehension: now excludes `_perm_skip` symbols
  - Revert: remove the `sym.upper() not in _perm_skip and (...)` wrapper; restore
    the original two-condition list comprehension
- **ADDED** per-symbol log line (`[deep_score] LIVE SCORE TRIGGERED: ...`) inside
  the `need_scoring` build loop, showing in-memory staleness and disk miss reason
  - Revert: remove the `_last` / `_mem_status` / `log.info(...)` block (5 lines)
- **ADDED** post-scoring loop adds any "skipped: ETF/INDEX" result to `_perm_skip`
  - Revert: remove the `_err` / `if ... startswith("skipped:")` block (3 lines)

---

## [2026-04-24 | Session H] Deep Score Cache — Wider Gap Tolerance

**Purpose:** The backfill creates snapshots at 31-day calendar intervals then snaps
each date backward to the nearest trading day. That snapping can shift a stored date
back by up to 4 days, making consecutive snapshot gaps as wide as 33–35 days.
The cache's hardcoded 31-day limit was treating those dates as misses, causing the
90-day fast backtest to re-score all symbols live on affected dates (e.g. Jan 27 fell
33 days from the Dec 26 snapshot).

**Impact on tests:** No change to scores or trade logic. The 90-day backtest will no
longer trigger live deep scoring for dates that fall in the snapping gap.

### Changes

#### `src/backtester/deep_score_cache.py`
- **CHANGED** `_MAX_GAP_DAYS: 31` → `40`
  - Revert: change back to `31`

---

## [2026-04-24 | Session G] Volume Gate — Lowered min_ratio by 30%

**Purpose:** The `vol_low` gate was blocking BUY entries on days where volume
was between 35–50% of the 60-day average. Run 007/008 showed `end_of_backtest`
positions with decent wins that likely cleared the buy threshold but were
rejected on volume. Lowering the threshold by 30% (0.50 → 0.35) allows entries
on moderately thin-volume days while still blocking genuinely illiquid sessions.

**Impact on tests:** Expect more total trades. Watch win rate — if the gate was
correctly filtering noise, win rate may dip slightly.

### Changes

#### `config/settings.yaml`
- **CHANGED** `trading.volume.min_ratio: 0.50` → `0.35`
  - Revert: change back to `0.50`

---

## [2026-04-24 | Session A] Live Bot — Anti-Churn & Regime Fixes

**Purpose:** Fix the buy→LLM-close→rebuy churn pattern seen in live trading
(AMD 3x in one day). Fix RSI overbought obsession in bull markets.

**Impact on tests:** Any backtest or live run from this point forward will
reflect these new gates. Runs before this block will show higher churn.

### Changes

#### `config/settings.yaml`
- **ADDED** `min_hold_hours_before_llm_close: 3.0`
  - Revert: remove this line
- **ADDED** `same_day_reentry_blocked: true`
  - Revert: remove this line

#### `src/trading/decision_engine.py`
- **ADDED** LLM CLOSE min-hold guard (Step 5 action logic)
  - Before executing an LLM-recommended CLOSE, checks that position has been
    held >= 3h OR LLM confidence >= 85%
  - Revert: replace the guarded block with `if llm_action == "CLOSE" and position: action = "CLOSE"`
- **ADDED** Same-day re-entry block (Step 7 BUY gate)
  - Calls `symbol_closed_today()` from position_reviewer; blocks BUY if symbol
    was already closed today for any reason
  - Revert: remove the `same_day_reentry_blocked` guard block
- **CHANGED** `technical_signal()` call now passes `regime=`
  - Revert: change back to `technical_signal(broker, symbol)`
- **ADDED** `record_today_close(sym)` call inside `_fire_postmortem()`
  - Revert: remove that call

#### `src/analysis/technicals.py`
- **CHANGED** RSI overbought score: was `-0.8` always; now `-0.3` when regime is BULLISH
  - Revert: `rsi_score = -0.8` (remove `_regime_bullish` branch)
- **CHANGED** Bollinger Band upper-band score: was uncapped; now capped at `-0.3` in BULLISH
  - Revert: remove `if _regime_bullish and pct_b > 0.8: bb_score = max(bb_score, -0.3)`
- **CHANGED** Function signature: added `regime: str | None = None` parameter
  - Revert: remove parameter

#### `src/analysis/llm_advisor.py`
- **CHANGED** SYSTEM_PROMPT: added rule against closing solely on RSI overbought
  - Revert: remove the added rule sentence
- **CHANGED** `_build_user_prompt()`: added `regime_label: str = ""` parameter;
  prompt now includes `Market regime: BULLISH` line
  - Revert: remove parameter and the `f"Market regime: ..."` line
- **CHANGED** `llm_signal()`: passes `regime_label=` to `_build_user_prompt()`
  - Revert: remove that kwarg

#### `src/analysis/position_reviewer.py`
- **ADDED** `record_today_close(symbol)` function
- **ADDED** `symbol_closed_today(symbol)` function
  - Revert: remove both functions

#### `run-backtest-fast.bat`
- **REMOVED** `--no-deep` flag from engine call (backtester now uses cached deep scores)
  - Revert: add `--no-deep` back to the python command line

#### `scripts/test_indicator.py` *(new file)*
- Standalone fib indicator backtester using real 15-min candle data
- Revert: delete this file

#### `scripts/run_indicator_test.bat` *(new file)*
- Launcher for test_indicator.py
- Revert: delete this file

---

## [2026-04-24 | Session B] Live Bot — Ratchet Stop 2.5% Guard

**Purpose:** The ratcheting locked-profit stop was moving too eagerly. Now it
only ratchets upward if price is at least 2.5% above the current stop level.

**Impact on tests:** Winners held through the ratchet phase will see fewer
(but more meaningful) stop raises. Positions near TP may stay in longer.

### Changes

#### `config/settings.yaml`
- **ADDED** `ratchet_min_move_pct: 0.025` under the `trading:` section
  - Revert: remove this line

#### `src/trading/decision_engine.py` — `_ratchet_locked_profit_stops()`
- **ADDED** `ratchet_min` check: `if price < old_stop * (1.0 + ratchet_min): continue`
  - Revert: remove that line and the `ratchet_min = ...` line above it

#### `scripts/test_indicator.py` — `_track_exit()`
- **ADDED** same 2.5% ratchet guard using `ratchet_min_move_pct` config key
  - Revert: revert to unconditional `if locked_profit and candle_count % 4 == 0:`

---

## [2026-04-24 | Session C] Backtester — Parity with Live Bot

**Purpose:** Full audit found 9 gaps where the backtester diverged from the
live bot's decision logic. Fixed all of them so backtest results better reflect
what the live bot would actually do.

**Impact on tests:** Backtest results will change. Expect:
- Fewer entries in downtrends and adverse regimes (new gates)
- Better regime-aware technical scoring (RSI/BB less punishing in bull runs)
- Fewer LLM-driven same-cycle closes (min-hold guard)
- No same-day re-buys after a close
- Stops tighten on big losing days (circuit breaker)
- Ratchet behavior now matches live bot exactly

### Changes

#### `src/backtester/engine.py`

1. **CHANGED** `technical_signal()` call: added `regime=` parameter
   - Revert: change back to `technical_signal(broker, symbol, regime=None)` (remove regime kwarg)

2. **ADDED** LLM CLOSE min-hold guard in `_decide()` (mirrors Session A live bot change)
   - Revert: replace with `if llm_action == "CLOSE" and position: action = "CLOSE"`

3. **ADDED** Weak quality in downtrend → HOLD gate in `_decide()`
   - Revert: remove the `if action == "BUY" and quality.get("label") == "weak" and "downtrend" in _trend_label` block

4. **ADDED** Same-day re-entry block in `_decide()` using `closed_today` set
   - Revert: remove the `same_day_reentry_blocked` gate block; remove `closed_today` param from signature

5. **ADDED** `closed_today` set initialized at day start, updated on signal closes and EOD stops
   - Revert: remove `_closed_today: set = set()` init; remove `_closed_today.add(...)` calls; remove `closed_today=_closed_today` from `_decide()` call

6. **ADDED** Circuit breaker stop-tightening: calls `tighten_all_stops()` once per day when CB fires
   - Revert: remove the `if not _cb_tightened:` block and `_cb_tightened = False` init

7. **CHANGED** Circuit breaker default threshold: `0.03` → `0.04` (matches live bot config default)
   - Revert: change back to `float(cfg_cb.get("daily_loss_pct", 0.03))`

8. **CHANGED** Trailing stop small-cap threshold: was hardcoded `15.0`; now reads
   `small_cap_price_threshold` from config
   - Revert: change back to `is_small_cap = entry_price <= 15.0`

9. **ADDED** `entry_datetime` stored in position tags at buy time (enables min-hold guard)
   - Revert: remove `"entry_datetime": broker._sim_dt.isoformat() ...` from tags dict

10. **CHANGED** `_ratchet_locked_profit_stops()`: ratchet guard now uses
    `price < old_stop * (1.0 + ratchet_min)` instead of entry-based `gain_pct < 0.025`
    - Revert: restore the original block:
      ```
      _entry_px = float(pos.tags.get("entry_price", current)) if hasattr(pos, "tags") and pos.tags else current
      _gain_pct = (current - _entry_px) / _entry_px if _entry_px else 0.0
      if _gain_pct < 0.025:
          continue
      ```

#### `src/backtester/signals.py`

11. **CHANGED** `backtest_llm_signal()`: `_build_user_prompt()` now receives
    `regime_label=` so LLM sees "Market regime: BULLISH" in backtests
    - Revert: remove `regime_label=...` kwarg from the `_build_user_prompt(...)` call

---

## [2026-04-24 | Session D] News Scorer — Minimum Signal Threshold

**Purpose:** Backtest analysis showed 74–82% of entries had `entry_news=1.0`,
and those trades averaged -$18 to -$40 per trade. Trades with news < 1.0
averaged +$11 to +$22. Root cause: the lexicon was scoring 1.0 from a single
positive keyword ("launch", "rally", "gains") in market-wide or cross-stock
headlines that had no relevance to the target ticker.

**What was happening:** Alpaca News returns broad market articles for any
ticker query. A headline like "Goldman Sachs, Caterpillar Lead DIA ETF Gains
In Dow's Strongest Session In A Year" would score NFLX, WMT, HD, and 20 other
stocks at 1.0 because the words "gains" and "strong" appeared with zero
negative words. One word = `1/1 = 1.0`.

**Fix:** Require at least 3 total polarized word hits before trusting the
score. Fewer than 3 → neutral 0.0. Genuine news catalysts (e.g. TXN analyst
upgrades = 6 hits, GILD partnership = 5 hits) still score correctly.

**Impact on tests:** Expect significantly fewer entries in the next backtest.
Remaining entries will have more genuine news backing. Combined score will
drop for many symbols that previously cleared the buy_threshold on news alone.

### Changes

#### `config/settings.yaml`
- **ADDED** `min_polarized_signals: 3` under `signals.news`
  - Revert: remove this line; the scorer defaults to 3 anyway, so removing the
    config key effectively reverts the behavior only if you also revert the code

#### `src/analysis/news_sentiment.py` — `_lexicon_score()`
- **CHANGED** added `min_signals: int = 3` parameter; returns `(0.0, reason)`
  when `total < min_signals`
  - Revert: remove the `if total < min_signals:` block and the `min_signals` parameter
- **CHANGED** `news_signal()` now reads `min_polarized_signals` from config and
  passes it to `_lexicon_score()`
  - Revert: remove `_min_sig = int(news_cfg.get(...))` line and revert both
    `_lexicon_score(headlines, ...)` calls back to `_lexicon_score(headlines)`

---

## [2026-04-24 | Session E] Stop/Entry Tuning + Trade Record Fix

**Purpose:** Four improvements from backtest analysis of runs 005 vs 006:
53% of stops were "too_tight" (stock continued up after firing); ratchet was
moving stops too aggressively; entries were happening with near-zero tech
scores; hold-time analysis was impossible with no opened_at field.

**Impact on tests:** Expect fewer total trades (min_tech gate), fewer
stop-outs (wider stop floor), winners held longer before stop catches up
(slower ratchet), and proper hold-time stats in all future runs.

### Changes

#### `config/settings.yaml`
- **CHANGED** `stop_loss_pct: 0.02` → `0.035`
  - Revert: change back to `0.02`
- **ADDED** `ratchet_step_pct: 0.30` (was hardcoded 0.50 everywhere)
  - Revert: remove this line; all three ratchet sites will fall back to 0.30 default, so also revert the code changes below
- **ADDED** `min_entry_tech_score: 0.05`
  - Revert: remove this line (code defaults to 0.0, so gate is disabled without it)

#### `src/trading/decision_engine.py`
- **CHANGED** `_ratchet_locked_profit_stops()`: ratchet step now reads
  `ratchet_step_pct` from config instead of hardcoded `0.5`
  - Revert: change `ratchet_step * (price - old_stop)` back to `0.5 * (price - old_stop)`; remove `ratchet_step = ...` line
- **ADDED** min_entry_tech_score BUY gate in Step 7
  - Revert: remove the `_min_tech` block (4 lines)

#### `src/backtester/engine.py`
- **CHANGED** `_ratchet_locked_profit_stops()`: same ratchet_step_pct config read
  - Revert: change `ratchet_step * (current - old_stop)` back to `0.5 * (current - old_stop)`; remove `ratchet_step = ...` line
- **ADDED** min_entry_tech_score BUY gate in `_decide()`
  - Revert: remove the `_min_tech` block (3 lines)

#### `scripts/test_indicator.py`
- **CHANGED** `_track_exit()`: ratchet step reads `ratchet_step_pct` from config
  - Revert: change `ratchet_step * (close - current_stop)` back to `0.5 * (close - current_stop)`; remove `ratchet_step = ...` line

#### `src/backtester/broker.py`
- **ADDED** `opened_at` field to every trade record in both `place_order()` SELL
  path and `_force_close()`. Value is the date portion of `entry_datetime` tag
  (set at buy time). Empty string if position predates this change.
  - Revert: remove the `_opened_at` / `_entry_dt_str` block and `"opened_at": _opened_at` line from both methods

#### `src/backtester/signals.py` — `backtest_news_signal()`
- **CHANGED** same threshold applied: reads `signals.news.min_polarized_signals`
  from config (default 3); fewer hits → neutral 0.0
  - Revert: remove the `_min_sig` block and the `elif total < _min_sig:` branch

---

## [2026-04-24 | Session F] Backtester — Redundant Computation Eliminated

**Purpose:** Cut 10-day fast backtest runtime by ~25% (~5 min off a 21-min run)
without changing any decision logic, signal weights, or trade outcomes.

**Impact on tests:** Results are bit-for-bit identical. Only wall-clock time changes.

### Changes

#### `src/backtester/engine.py`

1. **ADDED** `_tech_cache: dict = {}` initialized each day before cull
   - Passed as `tech_cache=_tech_cache` to both `_cull_symbols()` and `_rank_symbols()`
   - Revert: remove `_tech_cache` line; remove `tech_cache=_tech_cache` from both calls

2. **CHANGED** `_cull_symbols()`: added `tech_cache: dict | None = None` parameter;
   stores each computed `technical_signal()` result in the cache keyed by `sym.upper()`
   - Revert: remove the `tech_cache` parameter and the `_tkey`/cache-check block;
     restore `tech = ts(_BProxy(), sym)`

3. **CHANGED** `_rank_symbols()`: added `tech_cache: dict | None = None` parameter;
   reads from cache instead of recomputing `technical_signal()` for symbols already
   scored by cull (same `_BProxy`, same daily data — result is identical)
   - Revert: remove the `tech_cache` parameter and the `_tkey`/cache-check block;
     restore `tech = technical_signal(proxy, sym)["score"]`

4. **MOVED** `breadth = backtest_breadth(cache, sim_date)` and
   `regime = backtest_regime(cache, sim_date)` from inside the 6-cycle loop to
   before it. Both use only daily SPY/QQQ/IWM bars — result is identical for all
   6 cycles on the same day.
   - Revert: move both lines back inside the `for cycle_label, skip_llm in _CYCLE_TIMES:` loop
