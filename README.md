# Stock Trading Bot

A Webull-based algorithmic trading bot that:

- **Analyzes the market pre-market** (4:30 – 9:00 AM ET): dynamic screener, technical indicators, news/sentiment, market breadth, and an LLM synthesis pass.
- **Places trades at 9:30 AM, 12:00 PM, and 2:00 PM ET**, re-evaluating current positions and the market each time.
- **Audits at 4:30 PM ET**: an LLM reviews the day's journaled trades against realized P&L and writes lessons to a persistent notes file that's loaded as context for the next day's decisions.
- **Paper-trades by default**. A config flag flips it into live trading once you're confident.

> **Risk notice.** Trading real capital with any automated system can lose money fast. Keep `trading.live_mode: false` until you've run in paper mode long enough to trust both the code and the strategy. I am not a licensed financial advisor; nothing in this repo is investment advice.

---

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Copy config
cp .env.example .env
# edit .env — fill in keys you have, leave others blank to use sim mode

# 3. Try a dry run in simulation mode (no broker needed)
python -m src.main --mode sim --once pre_market
python -m src.main --mode sim --once decide
python -m src.main --mode sim --once audit

# 4. Run continuously (will wait for the scheduled times)
python -m src.main --mode sim
```

## Broker modes

The `broker.mode` setting in `config/settings.yaml` controls where orders go:

| Mode | What it does | When to use |
|---|---|---|
| `sim` | Fully offline. Fake fills at last quote. No network calls to any broker. | Development, first-time testing, no Webull API approval yet. |
| `webull_paper` | Webull OpenAPI, paper endpoint. Real market data, simulated fills. | Once you have OpenAPI credentials. |
| `webull_live` | Webull OpenAPI, live endpoint. Real money. | Only after extensive paper testing. |

### Getting Webull OpenAPI access

1. Open a Webull brokerage account (if you don't have one).
2. In Webull app → account → **OpenAPI Management**, submit an application describing what you'll build.
3. Once approved, generate an **App Key** and **App Secret** and put them in `.env` as `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
4. SDK docs: https://developer.webull.com/api-doc/ • Python SDK: https://github.com/webull-inc/openapi-python-sdk

Approval is not instant — you can develop and test everything in `sim` mode in the meantime.

## Configuration

All knobs live in `config/settings.yaml`. The most important ones:

- `broker.mode` — `sim` / `webull_paper` / `webull_live`
- `trading.max_positions` — concurrent positions allowed
- `trading.per_trade_pct` — % of equity per new position
- `trading.stop_loss_pct` / `trading.take_profit_pct`
- `screener.*` — price/volume/market-cap filters for the pre-market shortlist
- `signals.weights` — how much each signal source contributes to the final score
- `llm.model` — Anthropic model name for news sentiment + synthesis + reflection

Secrets (API keys) go in `.env`, never in `settings.yaml`.

## How the learning loop works

1. Every trade decision (even HOLD) is appended to `data/journal/YYYY-MM-DD.jsonl` with the full signal packet and the reasoning the LLM gave.
2. At 4:30 PM ET, the reflection step:
   - Pulls that day's journal entries.
   - Joins them with end-of-day prices to compute realized P&L per trade.
   - Asks the LLM to identify what worked, what didn't, and what rule/heuristic to apply tomorrow.
   - Appends a dated section to `data/lessons.md`.
3. On the next pre-market run, `data/lessons.md` is injected into the LLM advisor's system prompt. The bot literally reads its own prior mistakes before deciding.

This is simple by design — no gradient descent, no retraining — but it produces a readable, auditable record of why the bot believes what it believes.

## Project layout

```
config/
  settings.yaml          # all tunable parameters
src/
  main.py                # entry point + CLI
  scheduler.py           # APScheduler wiring
  broker/                # WebullClient + SimBroker
  analysis/              # technicals, news, breadth, llm_advisor
  screener/              # pre-market screen
  trading/               # decision engine, position manager, executor
  learning/              # journal + reflection
  utils/                 # logging, time helpers, config loader
data/                    # created at runtime — journals, lessons, state
tests/                   # smoke tests
Dockerfile
docker-compose.yml
```

## Running in Docker

```bash
docker compose up --build
```

The container runs `src.main` in scheduler mode. Mount a host directory to `/app/data` to persist the journal and lessons across restarts (the compose file already does this).

## Disclaimer

This software is provided as-is, with no warranty. You are responsible for any trades it places on your behalf. Start in `sim` mode. Then run in `webull_paper` for weeks. Only then, maybe, flip the live switch — and even then with small position sizes.
