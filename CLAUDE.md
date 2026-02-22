# SwingTrader — Claude Code Project Guide

## Critical: Railway Deployment

- **Never run the bot locally while Railway is active** — Telegram only allows one polling connection. To test locally, stop the Railway service first from the dashboard.
- **Railway project:** `e556a6d9-2023-4c81-a031-e32e160a33be`
- **Auto-deploy:** Connected to GitHub `niyonzimabryan/swingtrader` — `git push` to `main` triggers deploy
- **DB:** SQLite on Railway volume mount at `/data/swing_trader.db` — persists across deploys
- **Env vars:** Set via `railway variables set KEY=VALUE` (never commit .env)
- **Logs:** `railway logs` from the project directory

## Architecture

```
Haiku pre-screen → Sonnet deep analysis → Pattern/Fundamental/Web agents → Scoring engine → Opus final evaluation → Memo generation → Telegram delivery
```

- **Escalation chain:** `utils/escalation_manager.py` (Haiku → Sonnet → Opus prompts)
- **Agents:** `agents/` — catalyst, fundamental, pattern, web_research, macro, discovery, deep_research
- **Memo pipeline:** `memo/generator.py` → `memo/templates/ic_memo.py` → `bot/notifications.py`
- **Telegram bot:** `bot/` — handlers, keyboards, callbacks, message queue
- **Orchestrator:** `orchestrator/pipeline.py` (scan loop), `orchestrator/scheduler.py` (cron)
- **Database:** SQLite via SQLAlchemy, models in `database/models.py`

## Key Patterns

- All agent outputs use `AgentOutput` dataclass (`agents/base_agent.py`) with `raw_data` dict for agent-specific fields
- Sonnet/Opus prompts return JSON parsed via `client.analyze_json()` or `analyze_json_with_thinking_and_fallback()`
- Telegram messages use MarkdownV2 with fallback to plain text
- DB uses `get_session()` context manager from `database/db.py`
