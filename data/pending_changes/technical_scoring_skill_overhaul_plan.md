# Pending Change: Technical Scoring Skill Overhaul

Date: 2026-04-27

## Purpose

Overhaul the bot's technical scoring by translating the local `New Skills/trade-technical/SKILL.md` framework into a deterministic bot-safe scoring model. This should be tested against baseline backtests before implementation so we can judge whether the change improves performance, risk, and queue behavior.

## Recommendation

Do not run the technical skill literally as a live LLM/WebSearch agent inside the bot. The skill is written for long-form human technical reports with a 0-100 score. For the trading bot, convert that framework into local deterministic code that uses broker/backtest OHLCV data, preserves reproducibility, and keeps the existing `technical_signal()` API stable.

## Current Contract To Preserve

Keep `src/analysis/technicals.py::technical_signal(broker, symbol, regime=None)` returning:

- `score` in `[-1, 1]`
- `reason`
- `details`
- existing dashboard/backtest/queue fields such as:
  - `rsi_score`
  - `macd_score`
  - `trend_score`
  - `bb_score`
  - `obv_score`
  - `vwap_score`
  - `fib_score`
  - `fib_nearest_price`
  - `fib_nearest_ratio`
  - `fib_proximity_pct`
  - `fib_direction`
  - `roc_score`
  - `rs_etf_score`

This avoids breaking live decisions, backtests, queue routing, indicator tracking, dashboard views, and LLM prompts.

## Proposed Scoring Model

Translate the skill's five-dimension 0-100 framework into bot scoring:

- Trend: 0-20
- Momentum: 0-20
- Volume: 0-20
- Pattern Quality: 0-20
- Relative Strength: 0-20

Then convert the final technical score to the bot's existing range:

```python
bot_score = (technical_score_0_100 - 50) / 50
```

Interpretation:

- `100` becomes `+1.0`
- `75` becomes `+0.5`
- `50` becomes `0.0`
- `25` becomes `-0.5`
- `0` becomes `-1.0`

## Important Adaptation

Use a neutral-centered scoring approach. Each dimension should start near `10/20`, then add bullish evidence and subtract bearish evidence.

This matters because the skill's raw rubric gives no points for missing or absent bullish evidence. In a bot, missing data should usually be neutral, not automatically bearish.

Examples:

- No clear chart pattern: neutral pattern score, not a bearish score.
- Missing stochastic data: no award or deduction.
- Missing volume data: conservative neutral score and a data-gap note.
- Recently IPO'd stock: score conservatively, but do not fabricate indicator values.

## Proposed Dimension Details

### Trend

Use:

- price vs EMA/SMA 20, 50, 200
- moving average alignment
- moving average slope
- higher highs / higher lows or lower highs / lower lows
- distance from 52-week high/low when available
- ADX as trend-strength confirmation

### Momentum

Use:

- RSI zone and RSI trend
- MACD line/signal/histogram
- MACD histogram expansion or contraction
- stochastic oscillator if implemented locally
- rate of change
- bullish or bearish divergences if implemented reliably

### Volume

Use:

- current volume vs 20-day and 50-day averages
- up-day volume vs down-day volume
- OBV direction
- accumulation/distribution if implemented locally
- volume spike or thin-liquidity warnings

### Pattern Quality

Start simple and deterministic:

- breakout above recent resistance
- bounce from support
- consolidation/base detection
- volatility contraction
- failed breakout or breakdown
- Fib support/resistance confluence

Avoid overfitting complex chart-pattern labels until simpler structure proves useful.

### Relative Strength

Use:

- 1-month stock return vs SPY
- 3-month stock return vs SPY
- 6-month stock return vs SPY, if enough data exists
- sector ETF comparison when sector can be safely known

Backtests must use only data available as of the simulated date.

## Backtest Safety Rules

- No WebSearch inside live scoring or backtest scoring.
- No current yfinance data inside historical backtests.
- Relative strength comparisons must use historical bars from the backtest cache when running backtests.
- Missing data should be recorded in `details`, not silently converted into bullish or bearish evidence.
- Every returned score must be finite and clipped to `[-1, 1]`.

## Rollout Plan

### Phase 1: Baseline Before Implementation

Run current-system backtests and save results before changing technical scoring.

Suggested baseline set:

- normal backtest with LLM enabled
- no-LLM backtest
- small-cap enabled period
- volatile/bearish regime period
- recent bull/uptrend period

Record:

- total return
- win rate
- profit factor
- max drawdown
- Sharpe
- trade count
- average hold time
- stop-loss count
- queue queued count
- queue fired count
- queue skipped count
- queue expired/missed count
- indicator score buckets vs forward returns

### Phase 2: Shadow Implementation

Add the new technical scoring model while keeping the old score active.

Suggested config:

```yaml
signals:
  technicals:
    scoring_model: legacy
    shadow_skill_score: true
```

In shadow mode:

- live/backtest decisions still use the legacy `score`
- `details.skill_score_0_100` is recorded
- `details.skill_score_bot` is recorded
- dimension scores are recorded
- dashboards/backtest reports can compare old vs new

### Phase 3: Comparison Backtests

Run the same baseline backtest set with shadow scoring enabled.

Compare:

- legacy technical score vs skill technical score
- whether skill score better predicts forward 5-day returns
- whether skill score improves BUY selectivity
- whether fewer weak trades pass the min-tech gate
- whether queue triggers improve or become too conservative

### Phase 4: Controlled Switch

Only after comparison, switch:

```yaml
signals:
  technicals:
    scoring_model: skill_v1
```

Then rerun the same backtests and compare performance against baseline.

### Phase 5: Tune Thresholds

The bot's thresholds may need recalibration because the new score distribution will likely differ from the old average-based score.

Likely settings to revisit:

- `trading.min_entry_tech_score`
- `trading.buy_threshold`
- `entry_queue.queue_score_min`
- `entry_queue.near_level_pct`
- signal weights under `signals.weights`

## Files Likely To Change Later

- `src/analysis/technicals.py`
- `config/settings.yaml`
- `src/learning/indicator_tracker.py`
- `src/backtester/reporter.py`
- `dashboard.py`
- `tests/test_smoke.py`
- `CHANGELOG.md`

## Acceptance Criteria

Implementation should be considered successful only if:

- `technical_signal()` always returns a finite score in `[-1, 1]`
- missing data does not create NaN scores or silent no-trade cycles
- backtests are deterministic
- queue Fib fields remain compatible
- indicator tracking still works
- dashboard still renders technical details
- old vs new backtest comparison shows a measurable improvement or a clearly useful tradeoff

## Main Risks

- The new score may be more conservative and reduce trade count too much.
- More complex scoring may overfit historical periods.
- Pattern detection can become noisy if added too aggressively.
- Relative strength can introduce lookahead if backtest data handling is not strict.
- Existing thresholds may become miscalibrated.

## Suggested First Implementation Shape

When ready to implement, start with a conservative `skill_v1`:

- implement the five dimensions
- keep legacy details
- add new dimension details
- run in shadow mode first
- do not change live decision thresholds until after backtest comparison
