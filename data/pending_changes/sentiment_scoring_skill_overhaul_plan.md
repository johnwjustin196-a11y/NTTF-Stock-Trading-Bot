# Pending Change: Sentiment Scoring Skill Overhaul

Date: 2026-04-27

## Purpose

Replace the bot's current narrow `news` score with a broader sentiment scoring model based on `New Skills/trade-sentiment/SKILL.md`. This should not be implemented until baseline backtests have been saved, so we can compare whether the replacement improves live/backtest decision quality.

## Recommendation

Do not run the sentiment skill literally as a WebSearch-heavy live agent inside the bot. The skill is written for long-form human sentiment reports. For the trading bot, translate the framework into deterministic, bot-safe scoring code that preserves backtest reproducibility and avoids lookahead.

## Current Contract To Preserve

Keep the public function shape stable:

```python
news_signal(symbol) -> {
    "symbol": symbol,
    "source": "news",
    "score": float,  # [-1, 1]
    "reason": str,
    "details": dict,
}
```

This avoids breaking:

- live decision logic
- backtester decision logic
- entry queue fast/full rescoring
- gap-up filters
- earnings-blackout overrides
- urgent-news exits
- dashboard views
- LLM advisor prompts
- setup memory and postmortems

Internally, the new scorer can be called `sentiment_v1`, but externally it should still satisfy the current `news` signal contract at first.

## Proposed Scoring Model

Translate the skill's 0-100 Sentiment Score into the bot's existing `[-1, 1]` signal range:

```python
bot_score = (sentiment_score_0_100 - 50) / 50
```

Interpretation:

- `100` becomes `+1.0`
- `75` becomes `+0.5`
- `50` becomes `0.0`
- `25` becomes `-0.5`
- `0` becomes `-1.0`

Use five dimensions:

- News Sentiment: 0-20
- Social Media: 0-20
- Analyst Ratings: 0-20
- Institutional Activity: 0-20
- Insider / Short Interest: 0-20

Each dimension should start near neutral `10/20`, then add bullish evidence and subtract bearish evidence. Missing data should remain neutral and be recorded as a data gap.

## Key Safety Rule

No WebSearch inside live trading decisions or historical backtests.

The skill asks for WebSearch because it is a report-writing skill. The trading bot needs reproducible and time-safe data.

For the bot:

- live mode can use current APIs and cached data
- backtest mode must use only data available as of the simulated date/time
- non-point-in-time-safe data should be neutral in backtests until we have historical snapshots
- no missing dimension should fabricate confidence

## Initial Conservative Implementation Shape

Start with `sentiment_v1` as an improved news/catalyst scorer plus dimension scaffolding.

### News Sentiment Dimension

Improve the current headline scorer by adding:

- headline-level positive/neutral/negative classification
- source and recency weighting
- materiality weighting
- earnings beat/miss/guidance detection
- upgrade/downgrade detection
- lawsuit/regulatory/probe detection
- partnership/product-launch/contract catalyst detection
- negative controversy/PR-crisis detection
- headline deduplication and stale-news filtering

### Social Media Dimension

Default to neutral unless a reliable source is added.

Record:

- `social_score: 10`
- `social_data_gap: true`
- reason such as `social data unavailable; scored neutral`

This avoids fake retail-buzz signals.

### Analyst Ratings Dimension

Use only if we have reliable and preferably timestamped data.

Possible live-only signals:

- consensus rating
- price target upside/downside
- recent upgrades/downgrades
- target revisions

Backtest behavior:

- neutral unless point-in-time analyst data is available

### Institutional Activity Dimension

Treat carefully because 13F data is delayed and can introduce lookahead.

Possible live-only signals:

- institutional ownership range
- recent reported net buying/selling
- notable fund entries/exits

Backtest behavior:

- neutral unless point-in-time historical filings are available

### Insider / Short Interest Dimension

Possible signals:

- recent open-market insider buying
- cluster buying
- discretionary vs 10b5-1 sales when known
- short interest percentage
- days to cover
- short interest trend

Backtest behavior:

- neutral unless historical snapshots are available

## Rollout Plan

### Phase 1: Baseline Before Implementation

Run several current-system backtests before changing scoring.

Suggested baseline set:

- normal backtest with LLM enabled
- no-LLM backtest
- small-cap-heavy period
- volatile/bearish regime period
- recent bullish/uptrend period
- gap-up/catalyst-heavy period if available

Record:

- total return
- win rate
- profit factor
- max drawdown
- Sharpe
- trade count
- average hold time
- gap-up trades taken/skipped
- earnings-blackout overrides
- urgent-news exits
- queue queued/fired/skipped/expired counts
- old news score buckets vs forward returns

### Phase 2: Shadow Mode

Add the new scorer without using it for decisions.

Suggested config:

```yaml
signals:
  news:
    scoring_model: legacy
    shadow_sentiment_score: true
```

In shadow mode:

- decisions still use the legacy news score
- new sentiment score is stored in details
- dimension scores are stored in details
- dashboards/backtest reports can compare old news vs new sentiment

Suggested details fields:

- `sentiment_score_0_100`
- `sentiment_score_bot`
- `sentiment_model`
- `news_dimension_score`
- `social_dimension_score`
- `analyst_dimension_score`
- `institutional_dimension_score`
- `insider_short_dimension_score`
- `data_gaps`
- `headline_scorecard`
- `catalysts`

### Phase 3: Comparison Backtests

Run the same baseline backtest set with shadow mode enabled.

Compare:

- legacy news score vs new sentiment score
- correlation with forward 5-day returns
- BUY performance by sentiment score bucket
- gap-up trade performance
- urgent-news exit quality
- queued-buy rescore quality
- whether the new scorer is too neutral or too noisy

### Phase 4: Controlled Switch

Only after comparison, switch:

```yaml
signals:
  news:
    scoring_model: sentiment_v1
```

Keep the public key as `news` initially. A later refactor can rename UI labels and internal fields from `news` to `sentiment` if useful.

### Phase 5: Tune Thresholds

After switching, revisit:

- `trading.gap.min_news_score`
- `trading.earnings.min_news_score_to_trade`
- urgent-news thresholds
- `signals.weights.news`
- `entry_queue.queue_score_min`
- `trading.buy_threshold`

The new score distribution may differ from the old lexicon/LLM headline score.

## Files Likely To Change Later

- `src/analysis/news_sentiment.py`
- `src/backtester/signals.py`
- `config/settings.yaml`
- `src/analysis/position_reviewer.py`
- `src/trading/decision_engine.py`
- `src/trading/entry_queue.py`
- `src/screener/pre_market.py`
- `src/learning/setup_memory.py`
- `src/learning/reflection.py`
- `dashboard.py`
- `src/backtester/reporter.py`
- `tests/test_smoke.py`
- `CHANGELOG.md`

## Acceptance Criteria

Implementation should be considered successful only if:

- `news_signal()` always returns finite `[-1, 1]`
- no backtest lookahead is introduced
- missing social/analyst/institutional/insider data stays neutral
- urgent-news exits still work
- earnings/gap filters still work
- entry queue rescoring still works
- dashboard can show sentiment dimensions
- backtest comparison shows measurable improvement or a clearly useful tradeoff

## Main Risks

- Social media data can be noisy, manipulated, or unavailable.
- Analyst/institutional/insider data can be stale or not point-in-time safe.
- A richer sentiment score may become too neutral if most dimensions lack reliable data.
- A more aggressive catalyst scorer may overreact to headlines.
- Existing thresholds may become miscalibrated.

## Suggested First Implementation

When ready, implement:

1. Better headline/catalyst scoring.
2. `sentiment_v1` dimension scaffolding.
3. Shadow-mode recording.
4. Backtest dashboard/report comparison.
5. No live decision switch until after baseline comparison.
