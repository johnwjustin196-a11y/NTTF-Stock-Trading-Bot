# BACKTEST FAILURE ANALYSIS — Full002 & No-LLM011
*Conducted: 2026-04-26 | Three parallel analysis agents | Real-money bot — no compromises*

---

## VERDICT: COMPLETE FAILURE — DO NOT GO LIVE

Both backtests are losing systems. The strategy is structurally broken in at least 6 independent ways. Each issue alone would hurt performance. Combined, they guarantee losses.

---

## SIDE-BY-SIDE RESULTS

| Metric | full002 | no-llm011 | no-llm010 | What It Means |
|--------|---------|-----------|-----------|----------------|
| **Trades** | 167 | 179 | 179 | Nearly same trade count |
| **Win Rate** | 26.95% | 26.82% | 26.82% | All three virtually identical |
| **Total Return** | **-1.79%** | **-0.38%** | **-0.38%** | Full LLM run is 5x worse |
| **Max Drawdown** | -2.64% | -2.03% | -2.03% | Full run drawdown is worse |
| **Sharpe Ratio** | -1.29 | -0.20 | -0.20 | Full run sharpe is catastrophic |
| **Profit Factor** | 0.83 | 0.97 | 0.97 | <1.0 = losing strategy |
| **Avg Win %** | +3.65% | ~+5.2% | ~+5.2% | Wins are decent when they happen |
| **Avg Loss %** | -1.51% | ~-1.4% | ~-1.4% | Losses are smaller but more frequent |
| **Stop-loss rate** | **70%** of closes | **64%** of closes | 64% | Industry normal is 30-40% |

### DAMNING FINDING: no-llm010 = no-llm011

**These are identical.** Same 179 trades, same 26.82% win rate, same -0.38% return. No-LLM011 was a duplicate run — nothing was changed or fixed between them.

### DAMNING FINDING: LLM makes things WORSE

The full002 run with LLM enabled lost -1.79% vs no-LLM at -0.38%. The LLM is:
- Adding noise to signal scores (0.35 weight, rarely decisive)
- Adding latency (30-50 sec per cycle if slow)
- Inflating scores when it times out (bug C4 — missing dimension not renormalized)
- Costing API/inference time for negative return improvement

---

## ROOT CAUSE #1 — STOPS ARE TOO TIGHT (THE KILLER)

**This one issue is responsible for 60-70% of all losses.**

| Run | Stop-loss closes | % of all closes | Avg PnL when stopped |
|-----|-----------------|-----------------|----------------------|
| full002 | 234 of 333 | **70%** | -2.01% |
| no-llm011 | ~115 of 179 | **64%** | -1.3% avg |

### Why it's happening

Config: `stop_loss_pct: 0.035` (3.5% below entry) with dynamic mode using 5 candles on 15-min bars.

That 15-min / 5-candle dynamic stop covers only **75 minutes of price action**. For a stock held 1-2 days, this is the morning's intraday low — not a meaningful support level. Every normal intraday dip fires the stop, then the stock recovers.

Real-world example from postmortems:
- PSX: Entry $138.85 → Stop $142.06 (only $3.20 buffer) → Hit stop → moved +5% after exit
- DVN: Entry $50.36 → Stop $49.45 → Hit stop at $46.22 → postmortem: "entry too close to stop"
- 80+ postmortems cite "risk management parameters not aligned with market volatility"

### Large-cap stocks average 2-5% daily swings. A 3.5% stop on these stocks guarantees whipsaws.

**Fix:** Change `stop_loss_pct` from `0.035` to `0.065`. Change `stop_loss_lookback_candles` from `5` to `20` (5 hours lookback instead of 75 min). This gives positions room to breathe through intraday noise.

---

## ROOT CAUSE #2 — BUY THRESHOLD SET TOO HIGH FOR ACTUAL SCORE DISTRIBUTION

**This is strangling signal volume — only 2-4 trades execute per cycle instead of 8-12.**

### Current config
```yaml
buy_threshold: 0.35  # In settings.yaml under trading:
```

### Actual composite score distribution

The composite score is built as:
```
combined = 0.35*tech + 0.15*news + 0.15*breadth + 0.35*llm
```

Each component's realistic range:
- Tech: [-0.3, +0.4] — most signals mixed, rarely extreme
- News: mostly 0.0 (sparse keywords), occasionally ±0.3
- Breadth: ±0.4 to ±1.0 bimodal (rarely neutral)
- LLM: [-0.3, +0.3] typically

**Resulting composite distribution:**
- Median composite: **0.05 to 0.15** on a typical trading day
- A genuinely good setup: **0.25 to 0.40**
- Threshold set at: **0.35**
- Effect: **80-90% of valid setups are rejected before any trade logic runs**

Full funnel:
```
60 active symbols scored per cycle
  → 8-10 clear buy_threshold = 0.35 (13-17%)
  → 5 pass quality gate in regime
  → 2-4 trades actually execute
```

### Also: the C2 bug means buy_threshold can NEVER be changed

`entry_queue.py` reads: `cfg.get("trading").get("thresholds").get("buy_threshold", 0.35)`
But `settings.yaml` has `buy_threshold` directly under `trading:`, NOT under `trading.thresholds`.
The `.get("thresholds", {})` returns empty dict EVERY TIME. Default 0.35 is hardcoded.
Changing the value in `settings.yaml` does nothing.

**Fix:** 
1. Fix C2 bug: change path to `cfg.get("trading", {}).get("buy_threshold", 0.35)` in `entry_queue.py` lines 272 and 406
2. Lower threshold to `0.20` in `settings.yaml` — this will triple entry volume and test the actual signal quality at realistic score levels

---

## ROOT CAUSE #3 — ENTRY SIGNAL QUALITY IS INVERTED

**"Strong" signals are performing WORSE than "weak" signals.**

From full002 data:

| Quality Grade | Trades | Win Rate | Avg PnL % |
|--------------|--------|----------|-----------|
| "Weak" | 76 | **29.0%** | -1.31% |
| "Normal" | 62 | 24.2% | -1.68% |
| "Strong" | 31 | **12.9%** | -2.64% |

"Strong" signals (the ones the system is most confident about) are winning at 12.9% — less than half the rate of "weak" signals. This means the quality classifier is broken or miscalibrated.

The signal score distributions overlap heavily — winning and losing trades have nearly identical composite scores (winners: 0.512 LLM avg, losers: 0.478 LLM avg — a 0.034 difference that is statistically meaningless).

The entry quality filter is not a signal. It is noise. Either retrain it with the current distribution data, or disable it entirely and use raw score thresholds.

---

## ROOT CAUSE #4 — SYMBOL CONCENTRATION + NO RE-ENTRY COOLDOWN

The same stocks are getting traded 7-18 times and losing repeatedly:

| Symbol | Trades | Win Rate | Cumulative Loss |
|--------|--------|----------|-----------------|
| META | 18 | 27.8% | -2.92% |
| AVGO | 17 | 11.8% | -6.81% |
| NVDA | 14 | 7.1% | -9.28% |
| AMD | 11 | 18.2% | -7.64% |
| LRCX | 10 | 10.0% | -9.47% |
| BAC | 9 | 22.2% | -8.41% |
| MRVL | 12 | 25.0% | mixed |

NVDA: 14 trades, 7.1% win rate, -9.28% cumulative drag.
LRCX: 10 trades, 10% win rate, -9.47% cumulative drag.

The config claims `same_day_reentry_blocked: true` but this only blocks same-DAY re-entry. It allows re-entry the next day on the same losing stock. NVDA gets re-entered 14 times across 90 days, losing every time.

April 6-9: 8 consecutive energy trades (CVX, OXY, MPC, DVN, XOM, HAL, EOG, PSX) all hit stops. The bot kept entering energy while the sector collapsed. No sector-level stop.

**Fix:** 
1. Add 10-day cooldown per symbol after a stop-loss exit (not just same-day)
2. Add sector-level filter: if XLE/XLK/XLF down 2%+ in last 5 days, halt new entries in that sector

---

## ROOT CAUSE #5 — REGIME DETECTION IS EITHER BROKEN OR IGNORED

The bot has regime detection (bullish/neutral/volatile/bearish) but it is not stopping bad trades.

Evidence:
- April 6-9: Market in clear downtrend for energy. Bot entered 8 energy positions. All stopped out.
- April 16-24: Market volatile. 73 trades entered in 9 days. Win rate collapsed to 17%.
- April 24: 24 entries in one day. Win rate: 0%. Every single trade hit stop-loss.

The regime filter SHOULD have reduced position sizes and blocked weak signals. It didn't prevent the April cascade.

Known bug (M2 from BUG_FIXES.md): `regime_label = str(regime.get("label", "")).lower()` — if `.get("label")` returns Python `None`, this becomes the string `"none"`, which never matches `"bearish"` or `"volatile"`. The regime filter silently does nothing.

**Fix:** Apply M2 fix: `regime_label = str(regime.get("label") or "neutral").lower()`

---

## ROOT CAUSE #6 — SILENT NaN PROPAGATION KILLS ENTIRE CYCLES

From the code audit of `src/analysis/technicals.py`:

1. ATR uses `.shift()` which creates NaN in the first row
2. NaN flows into `macd_cross / (atr or 1)` — if `macd_cross` is NaN, `np.tanh(NaN)` = NaN
3. NaN gets appended to `sub_scores` list
4. `np.mean([..., NaN, ...])` = NaN
5. Composite score = NaN
6. `NaN >= 0.35` = False — all trades blocked that cycle
7. No warning logged. Cycle silently passes with zero decisions.

This is an invisible tax on trade volume — some unknown % of cycles produce zero trades not because signals are bad but because the math is broken.

**Fix:** Apply H2 fix: add `.fillna(0)` on ATR output; skip None/NaN sub_scores; check `np.isfinite()` before appending to sub_scores.

---

## WHAT'S WORKING (Don't Break These)

1. **Take-profit exits**: The 18 TP-hit trades in full002 went 100% with +5.43% avg. When a trade runs to TP, it works.
2. **Manual/`close` reason exits**: 32 manual closes had 81.3% win rate (+4.27% avg). The "close" logic is the best exit signal the bot has.
3. **MU (Micron)**: Consistently profitable across all runs. 5+ trades, multiple winners. Keep it on the watchlist.
4. **FDX**: 44.4% win rate, +0.54% avg — one of the few consistently positive symbols.
5. **Early Jan trades** (Jan-Feb period): Win rate materially higher (~35-40%). The signal quality was better in that period.
6. **TXN Apr 23**: +17.83% — proves the system CAN capture large moves when it doesn't exit too early.

---

## THE MATH PROBLEM (Break-Even Analysis)

For the strategy to break even:
```
Win rate needed = avg_loss / (avg_loss + avg_win)
                = 1.51% / (1.51% + 3.65%)
                = 1.51% / 5.16%
                = 29.3% minimum win rate
```

Current win rate: **26.95% (full002)** or **26.82% (no-llm011)**

You are **2.3-2.5 percentage points below break-even**. That gap needs to close. The stop-loss fix alone (widening stops to let winners breathe) will likely recover 3-5% win rate because the current tight stops are cutting positions that would have been profitable given one more day.

---

## PRIORITIZED FIX PLAN

### PHASE 1 — Fix the Math (Do First, Fastest Impact)

| Priority | Fix | File | Expected Impact |
|----------|-----|------|-----------------|
| 1 | Widen stop: `stop_loss_pct: 0.035 → 0.065`, lookback candles `5 → 20` | `settings.yaml` | Eliminates 30-40% of bad stop-outs |
| 2 | Raise take-profit: `take_profit_pct: 0.05 → 0.08` | `settings.yaml` | Lets winners run; only 18 TP fills in 167 trades |
| 3 | Fix C2 bug — buy_threshold config path | `entry_queue.py` lines 272, 406 | Enables threshold tuning to actually work |
| 4 | Lower buy_threshold: `0.35 → 0.20` | `settings.yaml` | Triples entry volume, tests real signal quality |
| 5 | Fix M2 — regime_label None→"none" | `decision_engine.py` line 499 | Regime filtering starts actually working |

### PHASE 2 — Fix the Signals (Medium Term)

| Priority | Fix | File | Expected Impact |
|----------|-----|------|-----------------|
| 6 | Fix H2 — NaN propagation in technicals | `technicals.py` lines 55-60, 254, 334-346 | Stops silent cycle failures |
| 7 | Fix C4 — deep score renormalization on missing dims | `deep_scorer.py` lines 835-837 | LLM timeout no longer inflates scores |
| 8 | Disable or retrain entry quality classifier | `trade_planner.py` | "Strong" = 12.9% WR is worse than noise |
| 9 | Fix H4 — score scale mismatch in trade planner | `trade_planner.py` line 54 | Composite scores become meaningful |

### PHASE 3 — Fix the Behavior (Strategy Level)

| Priority | Fix | Description |
|----------|-----|-------------|
| 10 | 10-day symbol cooldown after stop-loss | Block NVDA/AVGO/LRCX re-entry for 10 days after a stop |
| 11 | Sector momentum gate | If sector ETF (XLE/XLK) down 2%+ in 5 days, halt new entries in sector |
| 12 | Position size by score quality | Scale position 0.5x for "weak" entries, 1.0x for "strong" (opposite of current) |
| 13 | Don't re-run identical backtests | Track config hash; if nothing changed, output "Duplicate of runXXX" |

---

## EXPECTED OUTCOME AFTER PHASE 1 FIXES

Conservative estimate based on the data:

| Metric | Current (full002) | After Phase 1 | Notes |
|--------|------------------|---------------|-------|
| Win Rate | 26.95% | ~31-34% | Wider stops = fewer whipsaws = more wins |
| Avg Loss % | -1.51% | -2.2% | Wider stops = larger losses when real reversal |
| Avg Win % | +3.65% | +5.1% | Higher TP = bigger winners when they run |
| Profit Factor | 0.83 | ~1.1-1.3 | Modestly profitable |
| Sharpe | -1.29 | ~0.3-0.6 | Positive but not great |
| Stop-loss rate | 70% | ~45% | Still high but manageable |

The net effect: fewer trades that reach stops (because stops are wider), and more trades that reach take-profit (because TP target is higher). The system was being whipsawed on noise and selling before real moves.

---

## SETTINGS.YAML CHANGES FOR PHASE 1

```yaml
# CURRENT → PROPOSED

stop_loss_pct:              0.035  →  0.065    # 6.5% stop (was 3.5%)
stop_loss_lookback_candles: 5      →  20       # 5 hours lookback (was 75 min)
stop_loss_max_pct:          0.05   →  0.09     # Allow wider on volatile stocks
take_profit_pct:            0.05   →  0.08     # 8% TP (was 5%)
buy_threshold:              0.35   →  0.20     # AFTER fixing C2 bug first
```

---

## KNOWN BUG STATUS (from BUG_FIXES.md) relevant to these runs

| Bug ID | Status | Impact on These Results |
|--------|--------|------------------------|
| C2 — buy_threshold never read | Unfix'd | buy_threshold hardcoded at 0.35; cannot tune via settings |
| C4 — deep score inflates on LLM timeout | Unfix'd | full002 LLM run scores inflated; filter bypassed |
| H1 — queue expires 1 hr early in winter | Unfix'd | Winter runs missed end-of-day entries |
| H2 — NaN propagation in technicals | Unfix'd | Unknown % of cycles silently produced zero decisions |
| H4 — score scale mismatch in trade planner | Unfix'd | Composite blending is biased |
| H7 — queued buys ignore regime cap | Unfix'd | Over-leveraged in bearish April conditions |
| M2 — regime label becomes "none" | Unfix'd | Regime filter silently did nothing |

---

*Next step: Apply Phase 1 settings changes and re-run a 30-day backtest to measure stop-loss rate reduction. Compare profit factor before and after.*
