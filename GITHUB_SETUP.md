# GitHub Setup — Instructions for AI Assistant

This document tells an AI assistant exactly how to push this project to GitHub
and keep it in sync. Read this file top to bottom before taking any action.

---

## What this project is

A Python algorithmic trading bot that runs locally on Windows. It connects to
Alpaca paper/live trading, uses a local LLM (LM Studio) for trade analysis, and
runs on a Windows Task Scheduler. The code lives in this folder; secrets and
runtime data stay local only.

## What NEVER goes to GitHub

- `.env` — Alpaca API keys and any other secrets
- `data/` — journals, trade outcomes, broker state, market cache (runtime files)
- `logs/` — bot log files
- `venv/`, `.venv/` — Python virtual environments

All of these are already covered by `.gitignore`. Do not override or bypass it.

---

## One-time setup (first push)

Run these commands from the project root:
`c:\Users\lordo\OneDrive\Documents\Claude\Projects\Stock trading Bot`

### Step 1 — Create the GitHub repo (manual, done by the user)

The user must do this in a browser:
1. Go to https://github.com/new
2. Name it something like `stock-trading-bot`
3. Set it to **Private**
4. Do NOT initialize with a README, .gitignore, or license (the project already has these)
5. Copy the repo URL — it will look like `https://github.com/USERNAME/stock-trading-bot`

### Step 2 — Initialize git locally

```bash
git init
git branch -M main
```

### Step 3 — Stage and commit everything (respecting .gitignore)

```bash
git add .
git status
```

Review the output. Confirm that `data/`, `logs/`, and `.env` do NOT appear
in the staged files list. If they do, stop and investigate the `.gitignore`.

```bash
git commit -m "Initial commit — stock trading bot"
```

### Step 4 — Add the remote and push

Replace `REPO_URL` with the URL from Step 1:

```bash
git remote add origin REPO_URL
git push -u origin main
```

---

## Ongoing workflow (after initial push)

After making changes to the code:

```bash
git add .
git commit -m "Brief description of what changed"
git push
```

To pull updates on another machine or after a remote change:

```bash
git pull
```

---

## Reinstalling the bot on a new machine

1. Clone the repo:
   ```bash
   git clone https://github.com/USERNAME/stock-trading-bot
   cd stock-trading-bot
   ```
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the project root with the Alpaca API keys:
   ```
   ALPACA_API_KEY=your_key_here
   ALPACA_SECRET_KEY=your_secret_here
   ```
4. Verify `config/settings.yaml` has the correct broker mode (`alpaca_paper` or `alpaca_live`).
5. Run the bot:
   ```bash
   python -m src.main
   ```

---

## File layout (reference)

```
.
├── config/
│   └── settings.yaml        # All bot settings (safe to commit)
├── scripts/                 # One-off utilities (backfill, etc.)
├── src/                     # All bot source code
│   ├── analysis/
│   ├── backtester/
│   ├── broker/
│   ├── learning/
│   ├── scheduler/
│   ├── signals/
│   └── utils/
├── .env                     # SECRETS — never commit
├── .gitignore
├── requirements.txt
└── GITHUB_SETUP.md          # This file
```
