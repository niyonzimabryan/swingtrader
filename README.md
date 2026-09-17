# Swing Trader

[![CI](https://github.com/niyonzimabryan/swingtrader/actions/workflows/ci.yml/badge.svg)](https://github.com/niyonzimabryan/swingtrader/actions/workflows/ci.yml)

**Status: frozen.** This repo is not maintained. It is kept public as a record of
what I built and what I learned building it. Read on for why, or skip to
[Running the legacy bot](#running-the-legacy-bot) if you just want to see it work.

Read before touching any of the trading code: [Financial disclaimer](DISCLAIMER.md),
[MIT license](LICENSE).

## What this is

Two things, tagged together at `workspace-final`.

The first, and the one worth reading, is a **paper-trading swing bot**: a
scheduled scan finds candidate US equities, a set of research agents
(catalyst, fundamental, pattern, macro, web-research) build a case on each
one, a scoring engine ranks them, and a Telegram bot sends you the memo and
waits for a tap — approve, reject, or watchlist. Approved trades route to
Alpaca paper by default. Nothing places an order without a human in the loop.

The second is an **investment research workspace** built on top of it from
2026-09-05: a hosted FastAPI + MCP service (Specs K–Q, `specs/investment-workspace/`)
so any coding-agent session — Claude Code, Codex, Cursor — could attach over
MCP and ask "what do I own, what's my thesis, how have setups like this
performed," backed by a real ledger, a comparable-setups engine, and a
point-in-time evidence store. It shipped almost entirely behind flags that
default off, so it never actually ran unattended in production.

`docs/SYSTEM_OVERVIEW.md` is the full write-up — architecture, data model, the
honesty section on what was and wasn't verified in production. This README is
the summary and the verdict; that document is where the detail lives.

## What worked

The core loop — scan, research, score, memo, human approval — is the part I'd
keep. It's legible: you can read a memo and see why the bot thinks what it
thinks, and nothing executes until you say so. The Alpaca paper integration
was solid and the Telegram approval flow was genuinely pleasant to use. The
point-in-time discipline in the evidence store (`known_at_utc` vs. `valid_at`,
never scoring a setup on data that wasn't actually knowable yet) was the right
instinct, even where it ended up over-engineered for what I needed.

## What I over-built

The honest read: I never gave myself a stopping rule. This repo grew to about
150,000 lines of Python, 49,000 of them tests, across roughly a week of
orchestrated-build sessions that ran CI 273 times. The investment workspace —
Specs K through Q, a bitemporal ledger, a comparable-setups statistical
engine, a strategy lab with promotion tiers and shadow/paper/live arms — is
real, tested, and mostly correct, and none of it was necessary for one
person's paper-trading bot. Velocity compounds if nothing checks it: each PR
made the next one easier to justify, and the size of the thing stopped being a
signal I was paying attention to. The root of the repo shows the same pattern
in miniature — a PRD, an architecture-triggers doc, a running scratchpad —
artifacts of moving fast with no one asking "does this still need to exist."

## What I'd do differently

Scope the research workspace to the two or three questions I actually asked
in practice, before writing the general engine that could answer any of them.
Put a size or cost budget on an orchestrated build up front, the same way the
trading code puts a cap on position risk — "stop and reassess at N PRs" would
have caught this weeks earlier than reading the CI bill did. And separate
"interesting infrastructure to build" from "infrastructure this project
needs" earlier and more ruthlessly; the comparable-setups engine is good work
that belongs in a research tool, not bolted onto a Telegram trading bot.

## Successor

The research-partner half of this — reading filings, building a thesis,
learning the material — continues in a new, much smaller repo: **researchbench**.
No database, no hosted service, no MCP server. Markdown and a couple of CLIs,
with Claude as a tutor rather than a workspace client.

## Running the legacy bot

```bash
git clone https://github.com/niyonzimabryan/swingtrader.git
cd swingtrader
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m scripts.setup_wizard        # writes .env, needs Anthropic + Alpaca paper + a Telegram bot
.venv/bin/python -m scripts.doctor --skip-live
SCHEDULER_ENABLED=false .venv/bin/python main.py
```

Then message your bot: `/eval AAPL Strong services growth and buyback support`.
Don't run this locally against a Telegram bot that's already polling elsewhere
— Telegram allows one polling connection per bot, and if `TELEGRAM_ENABLED`
is on, a second process steals it.

Everything else — the workspace layer, the strategy lab, environment
variables, cost expectations, the architecture diagrams, the operations
runbook, and the honesty section on what was and wasn't verified in
production — is in [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md).
Older working notes are archived under [`docs/archive/`](docs/archive/).

## License and disclaimer

MIT License, see [LICENSE](LICENSE). This is not financial, investment, tax,
or legal advice — it's paper-trading research software that I built for
myself. Read [DISCLAIMER.md](DISCLAIMER.md) before using any of it.
