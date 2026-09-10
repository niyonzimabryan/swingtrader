# Owner setup — the copy-paste path to a testing state

Written 2026-09-10 for Bryan. `docs/ENV_SETUP.md` is the reference for every
variable; `docs/POSTGRES_CUTOVER_RUNBOOK.md` is the authority on the cutover.
This file is the sequence, with the commands, and says which steps the
orchestrating session already did from its own Railway access.

Railway project `swingtrader` (`e556a6d9-2023-4c81-a031-e32e160a33be`),
environment `production`. Services: `swingtrader` (the bot), `Postgres`
(provisioned 2026-09-10, empty, nothing reads it yet), `workspace` (created
2026-09-10 with start command `python -m workspace.server`, healthcheck
`/health`, `SERVICE_ROLE=workspace`, `WORKSPACE_API_ENABLED=false`; **no source
attached yet**).

Already set on the bot service: `SHARADAR_API_KEY` (rotate it at
https://sharadar.com/account when convenient — the value went through a chat),
`FRED_API_KEY`, `FINNHUB_API_KEY`, Alpaca paper keys, Telegram, `DATABASE_URL`
(still SQLite on the `/data` volume), and **`ALLOW_LIVE_TRADING=true`** — that
last one predates this build; Phase 6 treats it as one of three live gates, so
decide deliberately before ever setting `EXECUTION_MODE=live`.

## 1. Connect the workspace service to the repo (dashboard, 1 minute)

The project token cannot link GitHub, so this is yours: Railway → `swingtrader`
→ service `workspace` → Settings → Source → connect
`niyonzimabryan/swingtrader`, branch `main`. Do this **after PR #68 is on
`main`** (the `SERVICE_ROLE` guard): Railway applies the repo's `railway.toml`
start command (`python main.py`) to every service it builds, and #68 makes
`main.py` hand off to the workspace server when `SERVICE_ROLE=workspace`.

Expected first deploy log: the workspace starting, then `/health` → 200 with
`"workspace_api_enabled": false`. If instead you see `Missing required
environment variables: TELEGRAM_BOT_TOKEN`, the guard is not on `main` yet;
wait for #68 and redeploy.

Then give it a public URL: Settings → Networking → Generate Domain. That URL
is `WORKSPACE_BASE_URL`.

## 2. Postgres cutover (laptop, ~15 minutes, the bot is down for the copy)

From the repo on your laptop with `railway` logged in (`railway login`, then
`railway link` to the project). Everything up to step 4 is read-only.

```bash
# Phase B — classify and rehearse (read-only against production)
railway ssh --service swingtrader -- python -m scripts.schema_status sqlite:////data/swing_trader.db
#   expect: state: legacy   (or versioned). If "unknown": STOP, follow what it prints.

# copy the file out as the archive/rollback
railway ssh --service swingtrader -- cat /data/swing_trader.db > ./swing_trader.prod.$(date +%F).db

# the Postgres URL: Postgres service → Variables → DATABASE_PUBLIC_URL, then
# rewrite the scheme:  postgresql://  ->  postgresql+psycopg://
export POSTGRES_URL='postgresql+psycopg://...'

# rehearse against the real file (target must be empty; it is)
python -m scripts.migrate_sqlite_to_postgres \
    --source "sqlite:///$PWD/swing_trader.prod.$(date +%F).db" --target "$POSTGRES_URL"
#   every row "ok"; the report lands in docs/audits/ — commit it.
```

```bash
# Phase C — cutover. Pause the bot first (dashboard: service swingtrader →
# Settings → Sleep / replicas 0), confirm from logs it is down, then:
railway ssh --service swingtrader -- cat /data/swing_trader.db > ./swing_trader.cutover.db
python -m scripts.migrate_sqlite_to_postgres --force \
    --source "sqlite:///$PWD/swing_trader.cutover.db" --target "$POSTGRES_URL"
#   --force only because the rehearsal already filled the target; NEVER after the bot writes to Postgres.
python -m scripts.schema_status "$POSTGRES_URL"        # versioned, at head

railway variables --service swingtrader --set "DATABASE_URL=$POSTGRES_URL" --set "DATA_DIR=/data"
# un-pause the bot; in the logs expect:  schema_ready action=upgraded backend=postgresql
# smoke: /status in Telegram, one /eval, monitors ticking.
```

Use the **private** URL for the two services and the public one only from the
laptop (egress costs). The SQLite file stays on the volume as the archive.

## 3. Workspace on Postgres, token, attach a client

```bash
railway variables --service workspace --set "DATABASE_URL=<private postgres URL, psycopg scheme>" \
    --set "WORKSPACE_API_ENABLED=true" --set "RESEARCH_WORKSPACE_ENABLED=true" \
    --set "WORKSPACE_BASE_URL=https://<the generated domain>"
railway variables --service swingtrader --set "PORTFOLIO_SYNC_ENABLED=true" --set "RESEARCH_WORKSPACE_ENABLED=true"

# issue a token (prints once; only its digest is stored)
DATABASE_URL="$POSTGRES_URL" python -m scripts.workspace_token --issue --label "claude-code" --scopes read,research:write,propose

export WORKSPACE_BASE_URL=https://<domain>
export WORKSPACE_TOKEN=<printed token>
# then docs/WORKSPACE_ACCESS.md: `claude mcp add` / Codex / Cursor; call whoami, portfolio_overview.
```

## 4. Prices and cohorts (Sharadar 10-year Prices tier)

`SHARADAR_API_KEY` is already on the bot. The adapter is being ported to
Sharadar's direct API (PR on `claude/sharadar-direct-api`); until it merges the
backfill fails against the wrong host. After it merges:

```bash
railway variables --service swingtrader --set "PRICE_PLANE_ENABLED=true" --set "PRICE_PLANE_SOURCE=sharadar"
# from the laptop, against Postgres:
DATABASE_URL="$POSTGRES_URL" SHARADAR_API_KEY=... python -m scripts.price_backfill --source sharadar --bulk years=10
DATABASE_URL="$POSTGRES_URL" python -m scripts.audit_delisting_returns
# pick the benchmark row (SPY's security_uid in price_bars), then on the workspace:
railway variables --service workspace --set "COMPARABLE_SETUPS_ENABLED=true" --set "COMPARABLE_BENCHMARK_SECURITY_UID=<uid>"
DATABASE_URL="$POSTGRES_URL" python -m scripts.cohort_smoke     # Spec N §11: one insufficient, one ok, hand-checked
```

## 5. Submitting a trade (paper)

```bash
railway variables --service workspace --set "PHASE6_EXECUTION_ENABLED=true" --set "EXECUTION_APPROVAL_SECRET=<long random>"
railway variables --service swingtrader --set "PHASE6_EXECUTION_ENABLED=true" --set "EXECUTION_APPROVAL_SECRET=<same>" --set "EXECUTION_MODE=paper"
```

From an attached client: `propose_order` with `ticker`, `entry`, `stop`,
`risk_fraction` (no quantity — the service sizes it). A card arrives in
Telegram; approve it there. The bot places the paper entry on Alpaca, polls the
fill, places the `gtc` `stop_market`, reads it back, and the proposal reaches
`protected`. `docs/EXECUTION_LIFECYCLE.md` has every refusal you might see.

**Live** additionally needs, in this order: the Robinhood `gtc stop_market`
probe from `docs/EXECUTION_LIFECYCLE.md` §6 passing on the Agentic account;
`docs/robinhood/tool_schemas.json` committed (you generated it 2026-09-08);
`EXECUTION_MODE=live` on the bot; the kill switch off (`/live_kill off`);
`ALLOW_LIVE_TRADING=true` (already set — see the note at the top).

## 6. Evidence planes and Strategy Lab shadow (no capital involved)

```bash
railway variables --service swingtrader --set "SEC_USER_AGENT=Bryan Niyonzima niyonzimabryan@gmail.com" \
    --set "PLANE_SEC_MINIMAL_ENABLED=true" --set "PLANE_FILINGS_ENABLED=true" \
    --set "PLANE_MACRO_VINTAGE_ENABLED=true" --set "PLANE_NEWS_ENABLED=true"
# record real fixtures once from the laptop and push (docs/ENV_SETUP.md §3, §5):
python -m scripts.record_filings_fixtures && python -m scripts.record_macro_fixtures
railway variables --service swingtrader --set "STRATEGY_LAB_ENABLED=true" --set "STRATEGY_LAB_SHADOW_ENABLED=true"
# /experiments in Telegram after the next scan. STRATEGY_LAB_UNIVERSE_ENABLED only after the price backfill.
```

## Open items nobody can do but you

- Robinhood: fund the Agentic account; re-run `scripts/robinhood_auth.py` when
  the token lapses (~weekly idle); the live stop probe.
- Confirm the Rule 10b5-1 element and name the tracked investors (Spec O).
- Decide `ALLOW_LIVE_TRADING` deliberately.
