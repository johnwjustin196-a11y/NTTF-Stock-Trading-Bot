# Planned Backtest Improvements

Queued tweaks based on run 006 vs run 007 comparison (same 10-day window Apr 10-23 2026).

Run 007 baseline: 42 trades, +$448 (+0.50%), 28.6% win rate, avg hold 1.1 days.
Run 006 baseline: 66 trades, -$1,784 (-1.78%), 27.3% win rate.

Apply in order, run a backtest after each batch, compare to run 007.

---

## Priority 1 — Pure config changes (easy to test and revert)

### 1. Raise news signal threshold
- **File:** `config/settings.yaml`
- **Key:** `signals.news.min_polarized_signals`
- **Current:** `3`
- **Change to:** `5`
- **Reason:** At threshold=3, broad market headlines still score 1.0 on any mentioned ticker
  (e.g. "gains", "strong", "lead" = 3 hits). Genuine catalysts like TXN analyst upgrades
  and GILD partnerships had 5-6 hits. Run 007: 27/42 trades had news=1.0, averaged -$58,
  11% win rate — still the single biggest drag.
- **Revert:** change back to `3`

### 2. Raise buy threshold
- **File:** `config/settings.yaml`
- **Key:** `trading.buy_threshold`
- **Current:** `0.35`
- **Change to:** `0.40`
- **Reason:** Run 007 min combined score was 0.235 — many entries barely above threshold.
  Tightening cuts marginal entries where weak tech is bumped over the line by a fake news=1.0.
- **Revert:** change back to `0.35`
- **Note:** In no-llm mode buy_threshold auto-scales downward (~0.2275 effective).
  Raising the base raises the effective threshold proportionally.

### 3. Lower circuit breaker trigger
- **File:** `config/settings.yaml`
- **Key:** `trading.circuit_breaker.daily_loss_pct`
- **Current:** `0.04`
- **Change to:** `0.03`
- **Reason:** Apr 16 had 4 correlated large-cap stops in one day (-$525 total).
  Circuit breaker at 4% triggers too late on high-volatility days.
- **Revert:** change back to `0.04`

---

## Priority 2 — Small code gate

### 4. Block bullish-regime entries without minimum news
- **Files:** `src/backtester/engine.py` AND `src/trading/decision_engine.py` (live parity)
- **Gate logic:** if action == "BUY" and regime == "bullish" and entry_news < 0.5: HOLD
- **Reason:** Run 007 had 7 bullish-regime entries, 0 wins, -$452 total. Regime detector
  appears to lag — flags "bullish" on days about to reverse. Requiring news >= 0.5 in
  bullish regime forces a real catalyst before entering on a potentially false signal.
- **Alternative:** require combined score >= 0.45 when regime=bullish instead of news gate

---

## Do NOT touch (working well in run 007)

- `ratchet_step_pct: 0.30` — 3 correct ratchet moves seen in log
- `stop_loss_pct: 0.035` — wider stop is correct, premature stops reduced vs 006
- `min_entry_tech_score: 0.05` — cut 18 bad entries that existed in 006
- `same_day_reentry_blocked: true` — no churn visible in 007 log
- Signal close logic — 8/8 wins (100%) in run 007, avg +$364
- locked_profit_stop logic — working correctly

---

## Reference: key run 007 breakdowns

| Exit reason | Count | Win rate | Avg PnL | Total PnL |
|---|---|---|---|---|
| close (signal) | 8 | 100% | +$364 | +$2,910 |
| end_of_backtest | 5 | 60% | +$48 | +$238 |
| locked_profit_stop | 2 | 50% | +$84 | +$168 |
| stop_loss | 27 | 0% | -$106 | -$2,869 |

| Entry filter | Count | Win rate | Avg PnL |
|---|---|---|---|
| news = 1.0 | 27 | 11% | -$58 |
| news < 1.0 | 15 | 60% | +$128 |
| regime = bullish | 7 | 0% | -$65 |
| regime = neutral | 30 | 30% | +$22 |
| quality = weak | 19 | 32% | +$65 |
| quality = normal | 17 | 18% | -$56 |
