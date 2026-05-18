# Swing Trader

[![CI](https://github.com/niyonzimabryan/swingtrader/actions/workflows/ci.yml/badge.svg)](https://github.com/niyonzimabryan/swingtrader/actions/workflows/ci.yml)

AI-assisted swing-trading research and paper-trading operator bot.

This project runs a scheduled research pipeline, sends trade memos to Telegram, and can submit approved trades to an Alpaca paper account. It is built for paper trading first. Do not wire it to live brokerage credentials unless you have reviewed the code and risk controls yourself.

## Legal and safety notice

Swing Trader is paper-trading research software. It is not financial advice, it is not intended for unsupervised live trading, and an operator must review every approved trade. You accept all risk from using it.

Read these before setup:

- [Financial disclaimer](DISCLAIMER.md)
- [MIT license](LICENSE)

## Quick Start

Create a virtualenv and install dependencies:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Start the local setup wizard:

```bash
.venv/bin/python -m scripts.setup_wizard
```

Open:

```text
http://localhost:8765
```

The wizard creates `.env`, validates provider keys, discovers your Telegram chat ID after you message your bot, and keeps Gemini as an optional add-on.

## Required Setup

The normal open-source setup expects every core trading and data key:

- Anthropic API key
- Telegram bot token
- Telegram chat ID
- Alpaca paper API key
- Alpaca paper secret key
- Finnhub API key
- Financial Modeling Prep API key
- Alpha Vantage API key
- FRED API key
- Database URL, usually `sqlite:///swing_trader.db`

Gemini is optional for a bare setup, but recommended. When configured, Gemini 3.1 Pro Preview powers the search-heavy discovery and web-research stages with Google Search grounding, Gemini Flash screens Tier 1 names, and Gemini deep research can run in the background for high-conviction memos. If Gemini is blank, the app can fall back to Anthropic web search.

Firecrawl (`FIRECRAWL_API_KEY`) is also optional. When set, the catalyst agent recovers full article bodies for paywalled news (with archive.is as a second fallback) and the web research agent feeds scraped narrative directly into the LLM prompt. The free tier covers typical use (~50 calls/day). Without the key, agents degrade gracefully — catalyst summaries stay headline-only and web research relies on grounded LLM search alone.

## Validate

Run a local presence check:

```bash
.venv/bin/python -m scripts.doctor --skip-live
```

Run live provider checks:

```bash
.venv/bin/python -m scripts.doctor
```

## Tests

Run the full local test gate:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q agents bot config data database execution memo orchestrator scanning scoring screening scripts tracking utils main.py
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

GitHub Actions runs the same compile and unit-test checks on pull requests and pushes to `main`. See `CONTRIBUTING.md` for the testing expectations around order execution, provider parsing, onboarding, Telegram formatting, and scan gating.

## First Run

Start with the scheduler paused:

```bash
SCHEDULER_ENABLED=false .venv/bin/python main.py
```

In Telegram, send:

```text
/test AAPL
```

After the Telegram flow and paper account checks work, set `SCHEDULER_ENABLED=true` in `.env` if you want scheduled scans.

## Safety Notes

- Use Alpaca paper trading keys by default.
- Keep `.env` private. It is ignored by git.
- The bot only accepts commands from `TELEGRAM_CHAT_ID`.
- Outputs are research automation, not financial advice.
