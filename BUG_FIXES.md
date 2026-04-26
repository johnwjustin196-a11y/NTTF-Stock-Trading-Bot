# BUG FIXES TRACKING — Stock Trading Bot
*Full codebase audit conducted: 2026-04-26 | Three parallel agents | Real-money bot — no compromises*

Use the checkboxes to track which fixes have been applied. Do NOT close a box until the fix is tested.

---

## TIER 1 — CRITICAL (Do Not Run Live Until Fixed)

- [ ] **C1 — EXPOSED API KEYS IN `.env`**
  - **File:** `.env` lines 13, 18-19, 24
  - **Issue:** `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` are plaintext in a committed file. User email also exposed at line 24.
  - **Action:** REVOKE ALL THREE KEYS IMMEDIATELY at alpaca.markets and finnhub.io. Regenerate new keys. Add `.env` to `.gitignore` so it is never committed again.

- [ ] **C2 — CONFIG PATH MISMATCH: `buy_threshold` IS NEVER READ**
  - **File:** `src/trading/entry_queue.py` lines 272, 406
  - **Issue:** Code does `cfg.get("trading", {}).get("thresholds", {}).get("buy_threshold", 0.35)` but `settings.yaml` has `buy_threshold` as a direct child of `trading:`, NOT nested under `thresholds:`. The `.get("thresholds", {})` always returns `{}`, so the hardcoded default `0.35` is always used. Changing `buy_threshold` in `settings.yaml` has zero effect.
  - **Fix:** Change to `cfg.get("trading", {}).get("buy_threshold", 0.35)` at both lines 272 and 406.

- [ ] **C3 — LOOK-AHEAD BIAS IN BACKTEST `fwd_5d_return`**
  - **File:** `src/backtester/engine.py` line 756
  - **Issue:** `cache.price_at(_row["symbol"], _d + timedelta(days=8))` — uses 8 calendar days of future data but labels it "5-day return." Every signal win-rate metric in the dashboard is computed from data that did not exist at trade time. All backtest effectiveness statistics are unreliable.
  - **Fix:** Change `timedelta(days=8)` to `timedelta(days=5)`, or count actual trading days.

- [ ] **C4 — DEEP SCORE INFLATES WHEN LLM DIMENSIONS ARE MISSING**
  - **File:** `src/analysis/deep_scorer.py` lines 835-837
  - **Issue:** `score = sum(_WEIGHTS[dim] * breakdown[dim]["score"] for dim in _WEIGHTS if dim in breakdown)` — missing dimensions are skipped but weights are NOT renormalized. If "thesis" dimension is absent (LLM timeout), remaining weights sum to 0.85. A neutral score of 50 becomes ~58.8. Grades and trade cutoffs are applied to inflated values.
  - **Fix:** After the sum, divide by the sum of weights actually present: `total_w = sum(_WEIGHTS[dim] for dim in _WEIGHTS if dim in breakdown); score = score / total_w if total_w > 0 else 0`

- [ ] **C5 — P&L PERCENTAGE HAS WRONG SIGN FOR CLOSING TRADES**
  - **Files:** `src/backtester/broker.py` lines 365-367, 203; `src/backtester/engine.py` line 681
  - **Issue:** `pnl_pct = pnl / (entry * qty)` — when `qty` is negative (closing a short, or in closing-sell context), the denominator is negative, flipping the sign of `pnl_pct`. All backtest P&L percentages on trade closes are sign-wrong.
  - **Fix:** Change denominator to `abs(entry * qty)` at all three locations.

---

## TIER 2 — HIGH (Significant Logic Errors, Silent Failures)

- [ ] **H1 — QUEUE EXPIRES 1 HOUR TOO EARLY IN WINTER (EST vs EDT)**
  - **File:** `src/trading/entry_queue.py` lines 58-62
  - **Issue:** `expires = now.replace(hour=20, ...)` assumes UTC+0 = 4 PM ET. This is only true during EDT (March–November). During EST (November–March), 4 PM ET = 21:00 UTC, not 20:00 UTC. End-of-day entries expire at 3 PM ET all winter.
  - **Fix:** Use `zoneinfo.ZoneInfo("America/New_York")` to localize the 4 PM ET expiry properly.

- [ ] **H2 — NaN PROPAGATION: ATR → MACD/FIB → COMPOSITE SCORE**
  - **File:** `src/analysis/technicals.py` lines 55-60, 254, 334-346
  - **Issue:** ATR's `.shift()` creates NaN in first row, which flows into MACD via `np.tanh(macd_cross / (atr or 1))` when `macd_cross` is NaN. `_fib_score()` can return `None` which gets appended to `sub_scores`, and `np.mean([..., None, ...])` returns NaN. Composite score becomes NaN and trade decisions are made on garbage.
  - **Fix:** (1) Add `.fillna(0)` on ATR rolling mean output. (2) Check `np.isfinite(macd_cross)` before computing macd_score. (3) Only append `fib_score_val` to `sub_scores` if it is not None.

- [ ] **H3 — ACCOUNT EQUITY NOT VALIDATED BEFORE POSITION SIZING**
  - **File:** `src/trading/position_manager.py` line 79
  - **Issue:** `target_usd = min(account.equity * pct, ...)` — if broker returns NaN or 0 for equity (API glitch, connectivity issue), position sizes become NaN or zero with no warning.
  - **Fix:** Add guard: `if not account.equity or account.equity <= 0: return 0, {"reason": f"invalid equity: {account.equity}"}`

- [ ] **H4 — TRADE PLANNER MIXES INCOMPATIBLE SCORE SCALES**
  - **File:** `src/analysis/trade_planner.py` line 54
  - **Issue:** `composite = deep * 0.40 + (tech * 50 + 50) * 0.35 + (news * 50 + 50) * 0.25` — `deep` is on [0, 100] from deep_scorer; `tech` and `news` are on [-1, 1] from technicals/signals. The scaling `tech * 50 + 50` converts to [0, 100] but the distribution of deep_scorer scores is not uniform 0-100. The blending weights produce biased composite values.
  - **Fix:** Normalize all three inputs to the same scale before blending, or document explicitly.

- [ ] **H5 — BARE `except: pass` SWALLOWING REAL ERRORS (15+ LOCATIONS)**
  - **Files:** `src/trading/decision_engine.py` (~15 locations), `src/trading/scheduler.py` (~4), `src/trading/position_manager.py` (~2)
  - **Critical instances:**
    - `decision_engine.py:309` — quote fetch fails silently → stop/TP check skipped → unmanaged position
    - `decision_engine.py:452` — entry datetime parse fails silently → hold-time guard bypassed → premature exits
  - **Fix:** Replace `except Exception: pass` with at minimum `except Exception as e: log.warning(f"[symbol] error detail: {e}")`. Use specific exception types where possible.

- [ ] **H6 — SURVIVORSHIP BIAS: BACKTEST ONLY TESTS CURRENT WATCHLIST**
  - **File:** `src/backtester/data_cache.py` (design-level issue)
  - **Issue:** Backtest only runs on symbols currently in the watchlist. Stocks delisted, bankrupt, or removed for underperforming during the backtest window are excluded. All backtest results overstate performance because losing stocks that got cut are invisible.
  - **Fix:** At minimum, add a disclaimer to backtest output: `"WARNING: survivorship bias present — only current watchlist symbols tested."`

- [ ] **H7 — QUEUED BUYS IGNORE REGIME POSITION CAP AND DEEP SIZE MULT**
  - **File:** `src/trading/decision_engine.py` lines 1078-1091
  - **Issue:** (1) Queued buy uses hardcoded `max_positions` fallback of 15, ignoring regime-aware cap (could be 5 in bearish). (2) Queued buy does not apply `deep_size_mult` (lines 982-985 in main `_execute`). Bot can over-leverage in bearish conditions if an entry was queued before regime shifted.
  - **Fix:** Replicate the `_resolve_max_positions()` call and `deep_size_mult` application from `_execute` into the queued buy path.

- [ ] **H8 — EARNINGS BLACKOUT NEWS SCORE LOGIC IS INVERTED**
  - **File:** `src/backtester/signals.py` line 777
  - **Issue:** `if news_score >= min_news: return False, "strong news catalyst"` — this EXEMPTS trades when news score exceeds a minimum. With a low `min_news_score_to_trade` (e.g., 0.1), almost anything gets exempted. With a high threshold (0.5), a score of 0.3 blocks a trade even though sentiment is positive. The threshold direction is wrong.
  - **Fix:** Clarify intent: should this block trades near earnings UNLESS news is very strong (score > 0.7), not "unless score > minimum"?

---

## TIER 3 — MEDIUM (Wrong Results, Missing Validation)

- [ ] **M1 — ROC CALCULATION: DIVIDE BY ZERO IF OLD CLOSE IS 0 OR NaN**
  - **File:** `src/analysis/technicals.py` line 349
  - **Issue:** `roc_10 = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11])` — no guard for `close.iloc[-11] == 0` or NaN. Rare but possible with bad market data.
  - **Fix:** `prev = float(close.iloc[-11]); roc_10 = (close.iloc[-1] - prev) / prev if prev > 0 else 0.0`

- [ ] **M2 — REGIME LABEL BECOMES STRING `"none"` WHEN Python `None` RETURNED**
  - **File:** `src/trading/decision_engine.py` line 499
  - **Issue:** `regime_label = str(regime.get("label", "")).lower()` — if `.get("label")` returns Python `None`, `str(None)` = `"none"`, which never matches `"bearish"` or `"volatile"`. Regime filtering silently does nothing.
  - **Fix:** `regime_label = str(regime.get("label") or "neutral").lower()`

- [ ] **M3 — BACKTEST STOP/TP CHECKS USE DAILY OHLC, NOT INTRADAY**
  - **File:** `src/backtester/broker.py` lines 297-304
  - **Issue:** Stops/TPs are checked using only the daily bar high/low. A stock that opens, spikes to TP, then retreats — the daily high shows TP hit, but intraday timing could differ from reality. Stop and TP fill prices are unrealistic.
  - **Fix:** Use intraday bars for stop checking if available, or document this limitation in backtest output.

- [ ] **M4 — NEWS CACHE LOOKAHEAD: INCLUDES FULL DAY INSTEAD OF CYCLE TIME**
  - **File:** `src/backtester/signals.py` lines 416-421
  - **Issue:** `end_ts = datetime.combine(end_d, datetime.max.time()).timestamp()` includes news published up to 23:59:59. A 9:30 AM decision cycle sees afternoon headlines. Minor per-cycle but compounds across many trades.
  - **Fix:** Pass the cycle datetime to the filter and cap at cycle time: `end_ts = sim_date.timestamp()` if `sim_date` is a datetime.

- [ ] **M5 — TRAILING STOP PERCENT CALCULATED FROM WRONG BASELINE**
  - **File:** `src/backtester/engine.py` lines 1086-1094
  - **Issue:** `trail_pct = (entry_price - stop_price) / entry_price` computes initial stop width as % of entry, not the intended trailing %. When `_update_trailing_stops()` later applies `new_stop = current * (1 - trail_pct)`, it is using the wrong value. Trailing stop behavior in backtest is distorted.
  - **Fix:** Use the configured `trail_pct` from settings directly rather than back-calculating from entry/stop distance.

- [ ] **M6 — CONFIG VALUES NOT RANGE-CHECKED (COULD CAUSE MASSIVE OVER-SIZING)**
  - **File:** `src/trading/position_manager.py` lines 60-104
  - **Issue:** No bounds validation on `per_trade_pct`, `risk_per_trade_pct`, or `stop_loss_lookback_candles`. If `per_trade_pct` is set to `10.0` instead of `0.10`, position = 10x equity. If `risk_per_trade_pct` is negative, `shares_by_risk` goes negative.
  - **Fix:** Add validation: `if pct < 0 or pct > 1.0: raise ValueError(f"per_trade_pct={pct} out of bounds [0, 1]")`

- [ ] **M7 — QUOTE PRICE NOT VALIDATED BEFORE ORDER EXECUTION**
  - **File:** `src/trading/decision_engine.py` line 954
  - **Issue:** `q = broker.get_quote(sym)` then `if q.last < min_price:` — no check that `q.last > 0`. If broker returns 0.0, the comparison passes and an order could theoretically be placed at $0.
  - **Fix:** Add `if not q or q.last <= 0: log.warning(...); return None` immediately after the quote call.

- [ ] **M8 — MISSING TRANSACTION COSTS IN BACKTEST**
  - **File:** `src/backtester/broker.py` lines 53-55
  - **Issue:** Only a flat 0.1% slippage is applied. Commissions, real bid-ask spread, and market impact are missing. Across 100+ trades/year this inflates returns by several percent annually. Backtest results look better than reality.
  - **Fix:** Add a configurable commission per-trade (e.g., `commission_per_trade: 0.0` in settings, default to 0 for Alpaca but leave it configurable).

- [ ] **M9 — `effective_weights()` CALLED 40-50x PER CYCLE (PERFORMANCE)**
  - **File:** `src/trading/decision_engine.py` lines 286-290
  - **Issue:** `from ..learning.signal_weights import effective_weights; weights = effective_weights()` runs inside the per-ticker loop. It is called once for every ticker every cycle. Should be hoisted to cycle level.
  - **Fix:** Load weights once before the ticker loop and pass them in, or cache the result with `functools.lru_cache`.

---

## TIER 4 — LOW / USELESS CODE (Clean Up When Convenient)

- [ ] **L1 — SHORT POSITION HANDLING IS UNREACHABLE DEAD CODE**
  - **File:** `src/trading/position_manager.py` line 306+
  - **Issue:** Full short-position stop/TP logic exists but the bot never creates short positions. No buy-to-open-short logic anywhere in the codebase. This code will never execute.
  - **Fix:** Remove or mark with a `# TODO: not used — bot is long-only` comment.

- [ ] **L2 — DUPLICATE `math` IMPORT**
  - **File:** `src/backtester/engine.py` lines 23 and 125
  - **Issue:** `import math` at the top, then `import math as _math` locally in a function. One is unused.
  - **Fix:** Remove the duplicate local import.

- [ ] **L3 — TREND STRUCT REDUNDANTLY REBUILT BEFORE `compute_size()`**
  - **File:** `src/trading/decision_engine.py` lines 963-967
  - **Issue:** Extracts `trend` fields into flat variables then immediately reassembles the same nested dict. Could just pass `decision.get("trend") or {}` directly to `compute_size()`.
  - **Fix:** Remove the intermediate unpacking and reassembly.

- [ ] **L4 — `promoted_blocks` REASON STRING CAN GROW UNBOUNDED**
  - **File:** `src/trading/decision_engine.py` lines 382-395
  - **Issue:** If `check_promoted_rules()` returns many strings, all are appended to the journal reason field. No length cap. Won't break anything but postmortem logs become cluttered.
  - **Fix:** Cap at first 3-5 items: `promoted_blocks[:5]`.

- [ ] **L5 — MISLEADING EXCEPTION VARIABLE NAME `_npe`**
  - **File:** `src/backtester/engine.py` line 175
  - **Issue:** Named `_npe` (suggesting Java's NullPointerException) but catches all Python `Exception`. Misleading.
  - **Fix:** Rename to `_e` or `_err`.

- [ ] **L6 — BACKTEST NEWS LOOKBACK HARDCODED TO 3 DAYS, LIVE USES CONFIG**
  - **File:** `src/backtester/signals.py` line 522
  - **Issue:** `lookback_days: int = 3` is hardcoded in backtest. Live news uses config-driven lookback. If config is changed, live and backtest diverge silently. You cannot trust backtest to simulate what live will do.
  - **Fix:** Load from config: `lookback_days: int = int(cfg.get("news", {}).get("lookback_days", 3))`

- [ ] **L7 — STATE FILE WRITES ARE NOT ATOMIC (CRASH RISK)**
  - **File:** `src/broker/alpaca_broker.py` lines 84-87
  - **Issue:** `json.dump()` writes directly to the state file. If the process crashes mid-write, the state file is corrupted. On restart, position state is lost and the bot may re-enter already-open positions.
  - **Fix:** Write to a `.tmp` file first, then `os.replace(tmp_path, state_path)` for atomic swap.

---

## Audit Summary

| Tier | Count | Description |
|------|-------|-------------|
| CRITICAL | 5 | Must fix before any live trading |
| HIGH | 8 | Significant logic errors, silent failures |
| MEDIUM | 9 | Wrong calculations, missing validation |
| LOW | 7 | Dead code, naming, minor improvements |
| **Total** | **29** | |

**Most urgent:** C1 (API keys), C2 (buy threshold broken), C3 (backtest lookahead), C4 (score inflation), H1 (timezone/winter queue), H2 (NaN cascade), H5 (silent exceptions).
