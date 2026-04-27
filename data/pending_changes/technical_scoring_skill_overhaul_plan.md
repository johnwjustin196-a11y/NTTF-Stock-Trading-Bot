# Pending Change: Technical Scoring Skill Overhaul

Date: 2026-04-27

## Purpose

Overhaul the bot's technical scoring by translating the local `New Skills/trade-technical/SKILL.md` framework into a deterministic bot-safe scoring model. This should be tested against baseline backtests before implementation so we can judge whether the change improves performance, risk, and queue behavior.

## Recommendation

Do not run the technical skill literally as a live LLM/WebSearch agent inside the bot. The skill is written for long-form human technical reports with a 0-100 score. For the trading bot, convert that framework into local deterministic code that uses broker/backtest OHLCV data, preserves reproducibility, and keeps the existing `technical_signal()` API stable.

## Full Skill Coverage Requirement

This overhaul should replace the current technical process for backtester trials, not merely add a few extra indicators. The new `skill_v1` scorer must either compute each indicator from the provided `New Skills/trade-technical/SKILL.md` framework or record a clear `data_gap` explaining why it could not be computed for that symbol/date.

### Price And Moving Averages

Implement:

- current backtest-as-of price, current daily change, and percent change
- 20-day EMA, with SMA fallback only if needed
- 50-day EMA
- 200-day EMA
- price position versus each moving average, including distance percent
- moving average slopes
- bullish EMA stack: price > EMA20 > EMA50 > EMA200
- bearish EMA stack: price < EMA20 < EMA50 < EMA200
- recent golden cross and death cross detection
- imminent golden/death cross warning when EMA50 and EMA200 are close and converging

### Trend And Price Structure

Implement:

- higher highs / higher lows
- lower highs / lower lows
- range-bound structure between support and resistance
- trend classification: strong uptrend, uptrend, neutral, downtrend, strong downtrend
- trend strength using price structure, volume confirmation, and exhaustion signals
- 52-week high and 52-week low
- distance from current price to 52-week high and low
- ADX can remain as an extra trend-strength detail, but it is supplemental because it is not required by the skill scoring table.

### Momentum Indicators

Implement:

- RSI 14 current value
- RSI zone: oversold, bearish, bullish, overbought
- RSI trend: higher lows, lower highs, flat
- RSI bullish and bearish divergence
- MACD line
- MACD signal line
- MACD histogram
- MACD histogram direction: expanding, contracting, flat
- MACD zero-line bias
- recent MACD bullish and bearish crossovers
- MACD bullish and bearish divergence
- stochastic oscillator %K
- stochastic oscillator %D
- stochastic position: overbought, oversold, bullish, bearish, neutral
- stochastic %K/%D bullish and bearish crossovers
- stochastic bullish and bearish divergence
- combined momentum verdict from RSI, MACD, and stochastic
- ROC can remain as an extra detail for continuity, but it is supplemental to the skill model.

### Bollinger Bands And Volatility

Implement:

- upper, middle, and lower Bollinger Band values
- current price position within the bands
- Bollinger Band bandwidth
- squeeze status using a historical bandwidth percentile
- band walk detection on upper and lower bands
- mean reversion warning when price is at an extreme band with momentum divergence
- ATR value
- ATR percent of price

### Volume And Accumulation

Implement:

- current daily volume as of the backtest cycle
- 20-day average volume
- 50-day average volume
- volume trend: increasing, decreasing, flat
- volume spikes over the last 10 sessions
- volume dry-up
- climax volume
- breakout volume expansion
- consolidation volume contraction
- up-day volume versus down-day volume
- OBV value and direction
- OBV divergence versus price
- Accumulation/Distribution line value and direction
- Accumulation/Distribution divergence versus price
- volume verdict: accumulation, distribution, neutral, inconclusive

### Support, Resistance, And Fibonacci

Implement:

- at least 3 support levels when enough history exists
- at least 3 resistance levels when enough history exists
- level basis for each level: prior high/low, moving average, Fibonacci, round number, or pivot cluster
- level strength: strong, moderate, weak
- level orientation: ascending, descending, horizontal
- confluence zones where levels overlap
- recent significant swing high and swing low
- Fibonacci retracement levels from that swing
- nearest Fibonacci level
- Fibonacci direction: support or resistance
- Fibonacci proximity percent

Use a Flux-style pivot/retest/break lifecycle as the base support/resistance engine, implemented independently in Python rather than copied from Pine Script.

Important note: the TradingView script provided by the user is marked MPL-2.0. Use it as design inspiration only unless we intentionally include attribution and license handling. The bot implementation should be our own deterministic version.

V1 support/resistance behavior:

- detect confirmed pivot highs and pivot lows with a configurable pivot window, defaulting near 15 bars
- create resistance from confirmed pivot highs
- create support from confirmed pivot lows
- avoid duplicate active levels using an ATR-based spacing rule
- track retests where price touches a level and closes back on the valid side
- increment level strength for valid retests, with a cooldown so one noisy area does not overcount
- mark levels broken by close by default
- support optional wick-based invalidation for comparison tests
- track break volume ratio versus 20-day average volume
- mark breakouts/breakdowns as volume-confirmed only when volume is meaningfully above average
- allow broken resistance to become potential support after a successful retest
- allow broken support to become potential resistance after a successful retest
- combine near-duplicate levels from daily and weekly bars when cache data supports it
- record whether a level is active, broken, flipped, retested, or invalidated

V2 support/resistance candidates:

- intraday multi-timeframe levels
- tuned false-break filters by volume regime
- diagonal trendline support/resistance
- channel and wedge boundary scoring
- using skill-derived levels directly for stop/target exits

Do not let the V2 pieces affect the first comparison backtests until V1 support/resistance contribution logs show they add value.

### Chart Patterns

Implement detectors for all pattern families named in the skill. Pattern detection should be deterministic and conservative: record candidates with confidence, but only award or deduct scoring points when confidence and completion thresholds are met.

Bullish patterns:

- bull flag
- bullish pennant
- cup and handle
- inverse head and shoulders
- double bottom
- triple bottom
- ascending triangle
- rounding bottom / saucer
- VCP or tight consolidation after breakout

Bearish patterns:

- bear flag
- bearish pennant
- head and shoulders
- double top
- triple top
- descending triangle
- rising wedge
- distribution dome / rounding top

For each detected pattern, record:

- pattern name
- bullish or bearish bias
- continuation or reversal type
- confidence
- completion percent
- breakout or breakdown level
- measured move target
- volume confirmation status
- whether it affects the pattern score

If no pattern is detected, record `no_pattern` or `base_building` as neutral instead of bearish.

### Relative Strength

Implement:

- 1-month stock performance versus SPY
- 3-month stock performance versus SPY
- 6-month stock performance versus SPY
- sector ETF mapping where available
- 3-month stock performance versus sector ETF
- leading or lagging sector verdict
- relative strength line versus SPY
- RS line new-high detection

Backtests must compute these only from bars available at the simulated timestamp.

### Trading Setup Outputs

The skill produces a report-style trading setup. The bot should not automatically trade from these levels at first, but the backtester should store them for analysis:

- entry zone
- suggested stop loss from support/ATR/pattern structure
- target 1
- target 2
- risk/reward to target 1
- technical strengths list
- technical weaknesses list
- bullish scenario summary
- bearish scenario summary

These outputs can later help decide whether the queue and stop logic should use skill-derived levels.

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

- price vs EMA 20, EMA 50, and EMA 200
- moving average alignment
- moving average slope
- golden cross and death cross status
- higher highs / higher lows or lower highs / lower lows
- distance from 52-week high/low when available
- ADX as trend-strength confirmation

### Momentum

Use:

- RSI zone and RSI trend
- RSI divergence
- MACD line/signal/histogram
- MACD histogram expansion or contraction
- MACD zero-line bias and recent crossovers
- MACD divergence
- stochastic %K/%D, overbought/oversold status, crossovers, and divergence
- rate of change as a legacy continuity detail

### Volume

Use:

- current volume vs 20-day and 50-day averages
- up-day volume vs down-day volume
- OBV direction
- OBV divergence
- accumulation/distribution line direction
- accumulation/distribution divergence
- volume spike, dry-up, climax, breakout-expansion, and consolidation-contraction warnings

### Pattern Quality

Implement all chart-pattern families named in the skill, but score them conservatively:

- bull flag / bear flag
- bullish and bearish pennants
- cup and handle
- head and shoulders / inverse head and shoulders
- double and triple tops
- double and triple bottoms
- ascending and descending triangles
- rising wedge
- rounding bottom / saucer
- distribution dome / rounding top
- VCP or tight consolidation after breakout
- breakout above resistance, breakdown below support, support bounce, and base building
- Fib support/resistance confluence

Pattern labels should include confidence and completion percent. Low-confidence patterns should be logged for review but not allowed to swing the score aggressively.

### Relative Strength

Use:

- 1-month stock return vs SPY
- 3-month stock return vs SPY
- 6-month stock return vs SPY, if enough data exists
- sector ETF comparison when sector can be safely known
- RS line new-high detection

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

Add the new technical scoring model while keeping the old score active in live trading.

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

For the backtester trial, add a separate setting so we can run a true replacement test without changing live:

```yaml
backtest:
  technicals_scoring_model: skill_v1
```

### Phase 3: Backtester-First Replacement Test

In backtests, run the same baseline set with `skill_v1` as the active technical score. The old technical score should still be recorded in `details.legacy_score_bot` for comparison, but the backtest decisions should use the new skill score when this setting is enabled.

Compare:

- legacy technical score vs skill technical score
- whether skill score better predicts forward 5-day returns
- whether skill score improves BUY selectivity
- whether fewer weak trades pass the min-tech gate
- whether queue triggers improve or become too conservative
- whether stop-outs fall because weak technical setups are filtered earlier

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
- `src/analysis/technical_skill.py`
- `config/settings.yaml`
- `src/learning/indicator_tracker.py`
- `src/backtester/reporter.py`
- `src/backtester/engine.py`
- `src/backtester/data_cache.py`
- `dashboard.py`
- `tests/test_smoke.py`
- `tests/test_technicals_skill.py`
- `CHANGELOG.md`

## Suggested Implementation Shape

Add a new pure helper module, likely `src/analysis/technical_skill.py`, and have `technical_signal()` call it based on config.

Recommended structure:

- keep `technical_signal(broker, symbol, regime=None)` as the public entry point
- move legacy scoring into a `legacy` branch or helper
- add `compute_skill_technical_score(bars, symbol, market_bars=None, sector_bars=None, as_of=None)`
- return a structured result with `score_0_100`, `score_bot`, five dimension scores, raw indicator values, pattern candidates, levels, setup outputs, and data gaps
- store both old and new scores during backtests until we finish comparison
- keep existing Fib detail keys so queue routing and reports do not break
- ensure every numeric output is finite and every sub-score is clipped to `0..20`

Implementation order:

1. Data plumbing: make sure backtester can provide enough daily bars for 200-day EMA, 52-week high/low, 6-month relative strength, SPY, and sector ETF comparisons.
2. Core indicators: EMA 20/50/200, RSI, MACD, stochastic, Bollinger Bands, ATR, volume averages, OBV, Accumulation/Distribution, ROC compatibility.
3. Levels: pivots, support/resistance ranking, confluence zones, Fibonacci levels, 52-week high/low.
4. Relative strength: SPY comparisons, sector ETF comparisons, RS line new highs.
5. Pattern detectors: implement all skill pattern families with conservative confidence scoring.
6. Scoring: translate the skill's five 0-20 sub-scores into the bot's `[-1, 1]` score.
7. Backtest integration: enable `backtest.technicals_scoring_model: skill_v1` and record both legacy and skill scores.
8. Reporting and logging: add skill dimensions, data gaps, pattern summaries, score buckets, and outcome attribution to backtest reports.
9. Tests: add synthetic OHLCV unit tests for indicators, scoring bounds, missing data, and representative patterns.

## Logging And Evaluation Plan

The new technical scorer must be observable enough that we can tell what helped, what hurt, and what was just noise. The backtester should log both the final score and the evidence behind the score for every symbol that reaches the decision pipeline.

### Where To Log

Use the existing backtest artifacts first:

- `data/backtest_decisions.jsonl` for the latest run's per-decision rows
- `data/archive/backtest_decisions_master.jsonl` for long-term decision history
- `data/backtest_results.json` for the latest run summary
- archived `data/archive/backtest_results_*.json` files for run comparisons
- `data/backtest_history.json` for run metadata
- dashboard indicator pages for score/outcome analysis

If the skill payload becomes too large for the normal decision rows, add a compact summary to the normal row and write the full payload to a run-scoped JSONL file:

- `data/archive/technical_skill_details_<run_id>.jsonl`

Each full-detail row should include `run_id`, `symbol`, `as_of`, and enough fields to join it back to the decision row.

### Per-Decision Fields To Record

For every symbol/date scored in the backtester, record:

- `technical_model`: `legacy` or `skill_v1`
- `legacy_score_bot`
- `skill_score_bot`
- `skill_score_0_100`
- `skill_signal_label`
- `skill_trend_score`
- `skill_momentum_score`
- `skill_volume_score`
- `skill_pattern_score`
- `skill_relative_strength_score`
- `skill_data_gaps`
- `skill_primary_bullish_factors`
- `skill_primary_bearish_factors`
- `skill_pattern_names`
- `skill_pattern_confidence_max`
- `skill_support_nearest`
- `skill_resistance_nearest`
- `skill_fib_nearest_price`
- `skill_fib_direction`
- `skill_entry_zone`
- `skill_suggested_stop`
- `skill_target_1`
- `skill_target_2`
- `skill_risk_reward_t1`
- `skill_score_contributions`
- `skill_sr_engine_version`
- `skill_sr_nearest_support_strength`
- `skill_sr_nearest_resistance_strength`
- `skill_sr_nearest_support_distance_pct`
- `skill_sr_nearest_resistance_distance_pct`
- `skill_sr_active_support_count`
- `skill_sr_active_resistance_count`
- `skill_sr_retest_count_nearest`
- `skill_sr_break_status_nearest`
- `skill_sr_flipped_level_nearest`
- `skill_sr_break_volume_ratio`
- `skill_sr_points_added`
- `skill_sr_points_subtracted`

Keep existing fields such as `rsi_score`, `macd_score`, `trend_score`, `bb_score`, `obv_score`, `vwap_score`, `fib_score`, `roc_score`, and `rs_etf_score` so old dashboards and queue logic still work during comparison.

### Score Contribution Ledger

Every scoring point added or subtracted should be logged as a compact ledger entry so we can measure whether that rule helped.

Example:

```json
{
  "dimension": "pattern",
  "feature": "near_strong_support",
  "points": 2,
  "direction": "bullish",
  "evidence": "price 1.4% above strong pivot/Fib support",
  "level_id": "support_2",
  "value": 1.4
}
```

Support/resistance contribution names should include:

- `near_strong_support`
- `near_strong_resistance`
- `support_retest_hold`
- `resistance_retest_reject`
- `resistance_break_volume_confirmed`
- `support_break_volume_confirmed`
- `broken_resistance_flipped_support`
- `broken_support_flipped_resistance`
- `fib_support_confluence`
- `fib_resistance_confluence`
- `weekly_daily_level_confluence`

The report should aggregate these contribution names against forward returns, realized P&L, queue outcomes, and stop-outs. This is how we decide whether the Flux-style additions stay, get reweighted, or move to v2.

### Raw Indicator Snapshot

The full-detail skill log should store the raw values behind the score:

- price/change percent
- EMA 20/50/200 values and distances
- golden/death cross status
- 52-week high/low and distances
- RSI value, trend, and divergence
- MACD line, signal, histogram, histogram direction, zero-line bias, crossover, divergence
- stochastic %K/%D, zone, crossover, divergence
- Bollinger upper/middle/lower, bandwidth, squeeze, band walk, mean-reversion warning
- ATR and ATR percent
- volume current, 20-day average, 50-day average, trend, spikes, dry-up, climax, breakout expansion, consolidation contraction
- OBV direction and divergence
- Accumulation/Distribution direction and divergence
- support/resistance levels with basis, strength, orientation
- support/resistance lifecycle status: active, broken, flipped, retested, invalidated
- support/resistance retest times and break time where available
- support/resistance break volume ratio
- support/resistance invalidation mode used: close or wick
- confluence zones
- Fibonacci swing, levels, nearest level, direction, proximity
- pattern candidates with confidence, completion, breakout/breakdown, measured target, volume confirmation
- SPY and sector relative strength values
- RS line new-high status

### Outcome Fields For Measuring What Works

After each run, enrich the decision rows or report calculations with:

- forward 1-day return
- forward 3-day return
- forward 5-trading-day return
- forward 10-trading-day return when available
- max favorable excursion after signal
- max adverse excursion after signal
- whether the trade was entered
- whether it was queued
- whether a queued setup fired, expired, missed, or was skipped
- realized P&L if traded
- exit reason if traded
- whether the trade hit stop before reaching target 1
- hold time

These fields let us distinguish "good signal but bad execution" from "bad signal."

### Reports To Add

The backtest report/dashboard should summarize:

- legacy score versus skill score performance
- total return, win rate, profit factor, drawdown, Sharpe, trade count, and stop-outs by model
- P&L and forward returns by skill score bucket
- P&L and forward returns by each dimension score bucket
- BUY pass/fail rate by data-gap count
- performance when each major indicator is bullish, bearish, or neutral
- performance by chart pattern family
- performance by support/resistance contribution rule
- performance by level lifecycle state: active, retested, broken, flipped, invalidated
- performance by nearest support/resistance strength
- performance by distance to nearest support/resistance
- performance by close-invalidation versus wick-invalidation test runs
- performance by relative-strength regime
- queue fired/expired/skipped/missed counts by technical setup type
- stop-out rate by suggested stop distance and nearest support distance
- top positive contributors and top negative contributors from the skill score

Add an ablation comparison for support/resistance rules:

- `skill_v1_sr_enabled=true`
- `skill_v1_sr_enabled=false`
- `skill_v1_sr_flips_enabled=true/false`
- `skill_v1_sr_break_volume_confirm_enabled=true/false`

This lets us test whether the S/R points improve the model instead of merely making the score feel smarter.

### Success Signals

The overhaul is working if backtests show at least one of these without an unacceptable drawdown or trade-count collapse:

- higher profit factor
- better forward 5-day return prediction
- fewer immediate stop-outs
- fewer low-quality BUYs passing the gate
- better queue fire quality
- better separation between high-score and low-score trades
- clearer evidence about which indicators should receive more or less weight
- support/resistance contribution rules show positive forward-return or P&L separation

### Failure Signals

We should revise or reject the scoring change if backtests show:

- high-score trades do not outperform low-score trades
- pattern labels add noise without improving outcomes
- data gaps are common enough to make the score unreliable
- the new model blocks most trades without improving quality
- stop-outs increase
- relative strength introduces lookahead or cache instability
- sector ETF data gaps distort scores
- support/resistance contribution rules do not improve outcomes versus the ablation runs
- flipped-level logic creates false confidence after failed breakouts

## Implementation Defaults To Use Unless Changed

Based on the current plan, use these defaults for the first implementation:

- make `skill_v1` active for backtest decisions
- keep live trading on legacy technical scoring until backtests prove the change
- record legacy and skill scores side-by-side for comparison
- use conservative chart-pattern scoring
- log all pattern candidates, but only score high-confidence patterns
- expand the backtest cache to include SPY and sector ETFs
- include the Flux-style support/resistance lifecycle in v1
- keep experimental S/R pieces behind v2 feature flags
- log support/resistance score contributions and run ablation comparisons
- keep VWAP, ADX, and ROC as supplemental details, not core skill-score criteria
- record skill-derived stops and targets at first, but do not use them for exits until a later comparison run
- log enough raw indicator and outcome data to show which indicators worked and which did not

## Acceptance Criteria

Implementation should be considered successful only if:

- `technical_signal()` always returns a finite score in `[-1, 1]`
- missing data does not create NaN scores or silent no-trade cycles
- backtests are deterministic
- queue Fib fields remain compatible
- indicator tracking still works
- dashboard still renders technical details
- backtest decisions record legacy and skill technical scores side-by-side
- raw skill indicator details can be traced back to each scored symbol/date
- forward returns, queue outcomes, exits, stop-outs, and P&L can be grouped by skill dimension and indicator state
- old vs new backtest comparison shows a measurable improvement or a clearly useful tradeoff

## Main Risks

- The new score may be more conservative and reduce trade count too much.
- More complex scoring may overfit historical periods.
- Pattern detection can become noisy if added too aggressively.
- Relative strength can introduce lookahead if backtest data handling is not strict.
- Existing thresholds may become miscalibrated.

## Suggested First Implementation Shape

When ready to implement, start with a conservative but complete `skill_v1`:

- implement every indicator family from the skill
- implement the five scoring dimensions
- keep legacy details
- add new dimension details
- keep live trading on legacy with optional shadow logging
- run backtests with `skill_v1` as the active technical score
- record legacy score side-by-side for comparison
- do not change live decision thresholds until after backtest comparison
