# Environment setup — what to set, per phase, to reach a testing state

Maintained by the build loop as phases land. Every variable here is documented
with its default in `.env.example`; this page is the ordered checklist. Secrets
never go in the repo: local `.env` for a laptop, `railway variables` for prod.

## 0. What is already in place (nothing to do)

`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALPACA_*`,
`FINNHUB_API_KEY`, `FMP_API_KEY`, `FRED_API_KEY`, `ROBINHOOD_MCP_URL`,
`TOKEN_ENCRYPTION_KEY`, `ROBINHOOD_ACCOUNT_NUMBER`, `LANGFUSE_*`. The Robinhood
token store was re-authenticated on 2026-09-08 and holds a refresh token.

## 1. Database (Phase 0a/0b)

| Variable | Where | Value | Notes |
|---|---|---|---|
| `DATABASE_URL` | Railway bot service, workspace service, local `.env` | `postgresql+psycopg://…` after the cutover; `sqlite:///swing_trader.db` until then | Railway publishes `postgresql://`; rewrite the scheme to `postgresql+psycopg://` (psycopg 3). |
| `DATA_DIR` | Railway bot service | `/data` | **Required with a Postgres URL** — the encrypted Robinhood token blob and the backfill queue live here; without it they land on the ephemeral container disk. |

Cutover order is in `docs/POSTGRES_CUTOVER_RUNBOOK.md`: provision Postgres →
`python -m scripts.schema_status` against a copy of the prod SQLite file (expect
`legacy`) → run `scripts/migrate_sqlite_to_postgres.py` during a bot pause → set
the two variables → deploy. Phases 1–4 all work on SQLite locally; Postgres is
only required for the cloud-reachable workspace.

## 2. Workspace service (Phase 0b)

| Variable | Where | Value |
|---|---|---|
| `WORKSPACE_API_ENABLED` | workspace service | `true` (default `false`: `/health` answers, everything else 503) |
| `WORKSPACE_HOST` / `WORKSPACE_PORT` | workspace service | Railway sets `PORT`; see `railway.workspace.toml` |
| `WORKSPACE_BASE_URL` | your laptop shell, Codex/Cursor configs | `https://<workspace-service>.up.railway.app` |
| `WORKSPACE_TOKEN` | your laptop shell, Codex/Cursor configs | output of `python -m scripts.workspace_token --issue --label codex-laptop --scopes read,research:write,propose` (printed once) |
| `WORKSPACE_READ_RATE_LIMIT_PER_MINUTE` / `WORKSPACE_WRITE_RATE_LIMIT_PER_MINUTE` | workspace service | defaults 60 / 10 |
| `WORKSPACE_OAUTH_ENABLED` | workspace service | leave `false`; static bearer is the working path |

Create the second Railway service pointed at `railway.workspace.toml`. Client
attachment (Claude Code, Codex, Cursor, claude.ai connector) is in
`docs/WORKSPACE_ACCESS.md`; `.mcp.json` in this repo expands
`${WORKSPACE_BASE_URL}` and `${WORKSPACE_TOKEN}`, so until both are exported a
session prints `swingtrader-workspace (INVALID_CONFIG)` at startup — harmless.

## 3. Portfolio ledger (Phase 1)

| Variable | Where | Value |
|---|---|---|
| `PORTFOLIO_SYNC_ENABLED` | bot service | `true` to start hourly sync and the 30-day token-refresh log |
| `PORTFOLIO_FRESHNESS_BUDGET_MINUTES` | both | default 60 |
| `PORTFOLIO_SYNC_*` cadence | bot service | defaults fine |

Owner actions: `python -m scripts.record_robinhood_fixtures --out tests/fixtures/robinhood`
on the laptop that holds the token store (masks account numbers), and commit
`docs/robinhood/tool_schemas.json` from the 2026-09-08 dump (not in the repo yet).

## 4. Research workspace (Phase 2)

| Variable | Where | Value |
|---|---|---|
| `RESEARCH_WORKSPACE_ENABLED` | workspace service, bot service | `true` — registers the five research tools and the daily invalidator check |
| `RESEARCH_MIRROR_DIR` | wherever `scripts/sync_research_mirror.py` runs | default `research` |
| `RESEARCH_*` floors | either | defaults per Spec M §6 |

## 5. SEC ingestion (Phase 3a)

| Variable | Where | Value |
|---|---|---|
| `PLANE_SEC_MINIMAL_ENABLED` | bot service (job host) | `true` |
| `SEC_USER_AGENT` | wherever the backfill runs | a real contact address, e.g. `SwingTrader you@example.com` — the SEC requires it |

Owner action (the cloud build environment cannot reach `data.sec.gov`):
`python -m scripts.record_sec_fixtures --tickers AAPL MSFT KO` then
`python -m scripts.sec_backfill --coverage-universe --since 2015-01-01 --report coverage.json`.

## 6. Price plane (Phase 3p)

| Variable | Where | Value |
|---|---|---|
| `PRICE_PLANE_ENABLED` | bot service (job host) | `true` |
| `PRICE_PLANE_SOURCE` | same | `sharadar` (or `fixture` for tests) |
| `NASDAQ_DATA_LINK_API_KEY` | same | from the Sharadar "Prices" subscription |
| `LIQUID_UNIVERSE_TOP_N`, `DELISTING_AUDIT_*` | same | defaults per `docs/PRICE_PLANE.md` |

Owner actions: buy Sharadar Prices (confirm what "from $9" gates and the
redistribution terms), then `python -m scripts.audit_delisting_returns` before
relying on any cohort, then `python -m scripts.price_backfill --source sharadar --since 2015-01-01`.

## 7. Comparable setups (Phase 3c, pending)

`COMPARABLE_SETUPS_ENABLED=true` on the workspace service once merged; it reads
the price plane and `source_observations` and needs nothing else.

## 8. Evidence planes (Phase 4)

Three flags, all default `false`: `PLANE_FILINGS_ENABLED` (Form 4, 13D/G, the
8-K item index, entity history), `PLANE_MACRO_VINTAGE_ENABLED` (ALFRED vintages
and `regime_v1`), `PLANE_NEWS_ENABLED` (timestamped, clustered news). The
filings plane also needs `SEC_USER_AGENT`, which has no default in code and
must be a real contact address (`PLANE_SEC_MINIMAL_ENABLED` from Phase 3a uses
the same one).

`OPENFIGI_API_KEY` is free and optional — without it the CUSIP client runs at
25 requests/minute and 10 jobs per request instead of 25 per 6 seconds and 100.
FRED and Alpaca keys already exist: ALFRED uses `FRED_API_KEY`, and the news
plane reuses `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.

Each plane has a backfill job that prints its own coverage numbers and runs
offline against committed fixtures, so the order is: turn the flag on, run the
job with `--fixtures` to see the shape, then run it for real.

```bash
python -m scripts.filings_backfill --tickers AAPL MSFT --since 2020-01-01
python -m scripts.macro_backfill --regime-inputs
python -m scripts.news_backfill --symbols AAPL --start 2026-01-01
```

Details per plane: `docs/FILINGS_PLANE.md`, `docs/MACRO_PLANE.md`,
`docs/NEWS_PLANE.md`. Note the news constraint before wiring anything to it:
Alpaca's terms bar redistributing the data or any derived products, so nothing
news-derived may leave Postgres.

## 9. Execution (Phase 6, pending)

Nothing until the Robinhood `gtc` `stop_market` probe passes; the Agentic
account budget is loaded by hand (`ROBINHOOD_ACCOUNT_BUDGET` caps it in code).

## Order of operations to a testing state

1. Merge is on `main`; Railway auto-deploys the bot service. Confirm the bot
   restarted cleanly (a `schema_mismatch` log line means run the runbook §2).
2. Provision Postgres, follow `docs/POSTGRES_CUTOVER_RUNBOOK.md`, set
   `DATABASE_URL` + `DATA_DIR`.
3. Create the workspace service, set `WORKSPACE_API_ENABLED=true`,
   `RESEARCH_WORKSPACE_ENABLED=true`, `PORTFOLIO_SYNC_ENABLED=true` (bot), issue
   a token, export `WORKSPACE_BASE_URL` + `WORKSPACE_TOKEN` locally.
4. Attach a client (`claude mcp add` per `docs/WORKSPACE_ACCESS.md`), call
   `whoami`, then `portfolio_overview`. Holdings appear after the first sync.
5. Record real SEC and Robinhood fixtures from the laptop (§3, §5) and push.
6. Buy Sharadar Prices, run the delisting audit and the price backfill (§6),
   then flip `COMPARABLE_SETUPS_ENABLED` when Phase 3c lands.
