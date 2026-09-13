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
| `SHARADAR_API_KEY` | same | from https://sharadar.com/account, after buying the Prices subscription. `NASDAQ_DATA_LINK_API_KEY` is still read as a fallback if a deployment has not renamed the variable yet, but `SHARADAR_API_KEY` is the name going forward. |
| `PRICE_PLANE_FUNDS_ENABLED` | same | `true` **only when you are about to backfill the benchmark** (§7). Off by default; with it off the backfill reads `table=stocks` exactly as before funds existed. |
| `LIQUID_UNIVERSE_TOP_N`, `DELISTING_AUDIT_*` | same | defaults per `docs/PRICE_PLANE.md` |

Owner actions: buy Sharadar Prices (**10-year tier, $19/month** — see `docs/vendors/sharadar.md`) (confirm what "from $19" gates and the
redistribution terms), then `python -m scripts.audit_delisting_returns` before
relying on any cohort, then either:

- `python -m scripts.price_backfill --source sharadar --since 2015-01-01
  --tickers AAPL,MSFT,...` for an incremental or small-universe refresh (pages
  each ticker), or
- `python -m scripts.price_backfill --source sharadar --bulk years=10` to load
  the whole purchased history from Sharadar's bulk zip in one pass — the right
  mode for a full backfill, since paging thousands of names one at a time
  would take hours. `--tickers` narrows a bulk run to a subset **during**
  staging now, not after the zip is parsed. The zip is streamed into an
  on-disk SQLite staging file and derived one ticker at a time rather than
  held in memory whole (`docs/PRICE_PLANE.md`'s "Bulk backfill" section) —
  measured locally at 98 MB peak RSS against a ~5M-row synthetic zip, versus
  3.37 GB for the pre-streaming implementation (which is what got the bot
  container SIGKILLed in production; see
  `docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md` §4).
  A run interrupted for any reason resumes with
  `--resume /tmp/sharadar_bulk_checkpoint.json` (or wherever `--checkpoint`
  pointed it); `--max-rss-mb` (default 1500) aborts cleanly, checkpoint saved,
  well before that.

`data/prices/sharadar.py` targets the direct API
(`https://api.sharadar.com/v1.0`, `docs/vendors/sharadar.md`), ported against
payloads recorded live under `tests/fixtures/sharadar_direct/`.

Neither of those commands loads the **benchmark**. Sharadar keeps ETFs in a
separate table and SPY is only there, so it needs `--asset-class fund` — see §7
below, which is where that step now lives, because it is the comparable-setups
engine that cannot start without it.

## 7. Comparable setups (Phase 3c)

`COMPARABLE_SETUPS_ENABLED=true` on the workspace service. Off means
`compare_setups` and `cohort_detail` are not registered at all. It reads the
price plane and `source_observations` and needs, on the workspace service:

- `COMPARABLE_BENCHMARK_SECURITY_UID` — **required**: the `security_uid` in
  `price_bars` every abnormal return is measured against (a total-return
  benchmark, i.e. the SPY row). Empty refuses every cohort; there is no default
  benchmark on purpose, and a stand-in equity is not a substitute — a cohort
  answer against a benchmark that is not the benchmark is a wrong number
  (Spec N §5.2). **Getting this value is four commands; they are below.**
- `COMPARABLE_PRICE_SNAPSHOT` (default `dev`) — the named price-file vintage; its
  delisting audit (§6) must be recorded or no cohort can reach `vendor_pit`.
- `COMPARABLE_UNIVERSE_SLUG` (default `liquid_us_equity_v1`) — must have
  `universe_membership` rows for the period, or cohorts cap at
  `archival_reconstructed`.
- `COMPARABLE_EXECUTION_POLICY` (default `event_swing_14cal_v1`),
  `COMPARABLE_QUICK_BOOTSTRAP_REPS` (1000), `COMPARABLE_FULL_BOOTSTRAP_REPS`
  (10000) — leave as defaults.
- `COMPARABLE_CIK_MAP` (`TICKER:CIK,...`) — optional; empty refuses every name a
  market-cap decile rather than guessing one. Phase 4's entity plane replaces it.

### 7a. Loading the benchmark, concretely

Sharadar splits its price file: `stocks` (SEP) is operating companies, `funds`
(SFP) is ETFs, and **SPY is only in `funds`** — `stocks?ticker=SPY` comes back
empty. The backfill therefore needs to be told, and the fund path is off by
default, so:

```bash
# On the bot service (the job host), with SHARADAR_API_KEY already set.
railway variables set PRICE_PLANE_FUNDS_ENABLED=true

python -m scripts.price_backfill --source sharadar --since 2016-01-01 \
    --tickers SPY --asset-class fund --snapshot <your snapshot slug>
```

That prints the uid you need:

```
funds loaded — these are the security_uids:
  SPY      sharadar:118691

Set the cohort benchmark to one of them, e.g.
  COMPARABLE_BENCHMARK_SECURITY_UID=sharadar:118691
```

`python -m scripts.benchmark_uid SPY` prints the same thing later without
re-running the backfill. Then, on the **workspace** service:

```bash
railway variables set COMPARABLE_BENCHMARK_SECURITY_UID=sharadar:118691
```

Two things to know before you run it. `--since` wants a session of slack: a
fund's factors are ratios against the previous session *in the slice returned*,
so a distribution that went ex on the first day of the window is not visible in
that window (re-running with an earlier `--since` corrects it — the backfill
overwrites by `(security_uid, session_date)`). And SPY will **not** appear in
`liquid_us_equity_v1`: a fund is never a universe member, on purpose, because a
benchmark inside the universe it benchmarks is a cohort measured against itself.
`docs/PRICE_PLANE.md` has the reasoning and the live measurements behind the
total-return derivation.

### 7b. The smoke run

Then `python -m scripts.cohort_smoke` against the production database. Spec N
§11 asks for one `insufficient` and one `ok` answer hand-verified on real data;
against fixtures that pair is produced by
`tests/test_cohort_smoke_fund_benchmark.py`, and on real data it is what this
run is for. `insufficient` is a valid and frequently-correct answer at a floor
of twenty distinct event dates — read it as a refusal, not a failure.
`docs/COMPARABLE_SETUPS.md` is the reference.

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

## 9. Execution lifecycle (Phase 6)

The proposal → approval → execution path (Spec L §6). Full state machine, guard
table, and the owner's live-probe runbook are in `docs/EXECUTION_LIFECYCLE.md`;
this is the checklist.

Everything defaults **off**. With `PHASE6_EXECUTION_ENABLED=false` the
`propose_order` tool is not registered on the workspace and every approval
callback is refused.

To reach a **paper** testing state (no live capital, Alpaca paper venue, same
lifecycle):

- `PHASE6_EXECUTION_ENABLED=true` on both the workspace and the bot services.
- `EXECUTION_APPROVAL_SECRET=<a strong secret>` — without it no approval card can
  be minted or verified, so nothing can be approved. This is the intended
  failure, not a bug.
- `EXECUTION_MODE=paper` (the default). Approvals route to the Alpaca paper
  adapter.
- Issue a workspace token carrying the `propose` scope (`scripts/`), then call
  `propose_order` from an attached client.

To reach a **live** state — only after the §6 live probe has passed:

- Additionally `ALLOW_LIVE_TRADING=true` and `EXECUTION_MODE=live`. Both are
  required on top of the flag; absence or invalidity of either never means live.
- Fund the Agentic account by hand; `ROBINHOOD_ACCOUNT_BUDGET` caps it in code.
- Confirm the kill switch is off: `/live_kill off` (it is a persistent database
  row and survives restart; `/live_kill on` blocks approval-to-placement).

Optional tuning (defaults in `.env.example` / `docs/EXECUTION_LIFECYCLE.md` §5):
`RISK_FRACTION_HARD_CAP`, `RISK_FRACTION_PERCENTAGE_FLOOR`, `EVIDENCED_RISK_CAP`,
`DISCRETIONARY_RISK_CAP`, `DISCRETIONARY_DAILY_NOTIONAL`, `EVIDENCE_GATE_MODE`
(`advisory` default), `CITATION_MAX_AGE_SESSIONS`, `PROTECTION_WINDOW_SECONDS`,
`APPROVAL_TTL_SECONDS`, `PROPOSAL_MAX_POSITION_PCT`, `PROPOSAL_MAX_SECTOR_PCT`.

The Robinhood `gtc` `stop_market` live probe (`docs/EXECUTION_LIFECYCLE.md` §6)
is an **owner action** and is not run from a build session. Until it passes,
keep `EXECUTION_MODE=live` off.

### 9a. Approving in the coding-agent chat (owner ruling 2026-09-13)

Owner ruling, recorded in Spec K §10 and Spec L §10: Bryan reads the card in his
coding-agent chat and approves there, instead of on a Telegram button. Two flags,
on two different services, and **both default off**. With neither set, nothing
about §9 changes.

| Variable | Service | Default | What it does |
|---|---|---|---|
| `WORKSPACE_OWNER_TOOLS_ENABLED` | workspace | `false` | Registers the ten owner tools (`AGENTS.md` §3) |
| `OWNER_ACTION_POLLER_ENABLED` | bot | `false` | The runtime acts on a recorded decision |
| `OWNER_ID` | both | `TELEGRAM_CHAT_ID` | Who an approval binds to |
| `OWNER_ACTION_POLL_SECONDS` | bot | `20` | Poll interval, clamped to 5–120 |
| `OWNER_ACTION_TTL_SECONDS` | both | `900` | A prepared tier change's lifetime |

The division of labour is the point: an MCP tool **records** a decision into the
`owner_actions` table and places nothing; the poller in the bot container is the
only thing that acts on one, and it acts through the same execution service the
Telegram callback drives. So with the tools on and the poller off, an approval
you record simply sits there — and the tool's own answer says so, in the
response, rather than leaving you to notice.

Three things to get right:

- **`OWNER_ID` must match on both services.** A card minted by one and verified
  by the other fails `owner_mismatch` otherwise. Leaving it unset on both is the
  safe default: it resolves to `TELEGRAM_CHAT_ID`, which every existing card was
  already bound to.
- **`EXECUTION_APPROVAL_SECRET` must already match on both**, as §9 requires. It
  signs the tier-change confirmations too.
- **The `admin` scope is the control.** Eight of the ten tools require it, and
  nothing in the code distinguishes a token you are holding from one left in an
  agent's standing environment. `docs/WORKSPACE_ACCESS.md` §1 is the policy and
  the revocation recipe. `kill_switch` is deliberately reachable on a plain
  `read` token in the engaging direction — you never need the admin token to
  stop the system, only to start it again.

Turning them on, in order: set `OWNER_ACTION_POLLER_ENABLED=true` on the bot
first (there is nothing for it to find yet), then
`WORKSPACE_OWNER_TOOLS_ENABLED=true` on the workspace. Each variable change
redeploys its service.

## 10. Strategy Lab shadow integration (Spec Q, PR 4)

Versioned strategies running in shadow beside the existing scan. Everything
defaults **off**, and with `STRATEGY_LAB_ENABLED=false` the pipeline hook
returns before it imports anything, no scheduled job is registered, the weekly
report gains no section, and the owner-only commands answer "disabled" instead
of reading a table. The domain model, the roster and the scorecard are in
`docs/STRATEGY_LAB.md`.

Two gates, because they turn on different things:

| Flag | What it turns on |
|---|---|
| `STRATEGY_LAB_ENABLED` | the read surface: `/experiments`, `/strategies`, `/strategy <slug>`, `/pause_experiment`, `/resume_experiment`, and the weekly scoreboard section |
| `STRATEGY_LAB_SHADOW_ENABLED` | the one path that **writes**: the post-scan shadow pass and the nightly maturation job at 04:15 ET |

To reach a **shadow** testing state (no broker, no capital, no order):

- `STRATEGY_LAB_ENABLED=true` and `STRATEGY_LAB_SHADOW_ENABLED=true` on the bot
  service. Nothing is needed on the workspace service: the Strategy Lab has no
  MCP tool, by design (Spec Q §4.1 — an agent cannot reach it).
- Leave `STRATEGY_LAB_EXPERIMENT=shadow_roster_v1` alone unless you want a fresh
  experiment. The analysis plan is pre-registered in
  `orchestrator/strategy_lab_shadow.py` and frozen at registration: re-registering
  the same name with a changed plan is refused, and the remedy is a new name
  here, not an edit there.
- The first scan after the flags flip registers the experiment, the four roster
  versions and four shadow arms, then records decisions. Check with
  `/experiments`.
- The nightly 04:15 ET job settles decisions once their forward bars exist. Until
  the price plane is populated (§6) only the compatibility arm
  (`swingtrader_composite_v1`) produces decisions, because it reads the
  pipeline's own `scored_candidates` row and needs no bars; the other three
  abstain with `missing_dependency`, which is recorded rather than hidden.

Cross-sectional arms (`momentum_v1`, `short_term_reversal_v1`) additionally need
`STRATEGY_LAB_UNIVERSE_ENABLED=true`, a populated `universe_membership` table and
`PRICE_PLANE_ENABLED=true` (§6). One universe snapshot is built per scan cutoff
and shared by both arms; leave the flag false until the plane is backfilled,
because a 500-name read against an empty plane buys nothing.

Everything else is tuning, defaults in `.env.example`: the virtual shadow book
(`STRATEGY_LAB_SHADOW_EQUITY`, `STRATEGY_LAB_SHADOW_RISK_BUDGET`,
`STRATEGY_LAB_SHADOW_MAX_OPEN_POSITIONS`,
`STRATEGY_LAB_SHADOW_MAX_POSITION_FRACTION`), the per-scan and per-run caps
(`STRATEGY_LAB_MAX_TICKERS_PER_SCAN`, `STRATEGY_LAB_MATURATION_MAX_SNAPSHOTS`),
the cost model (`STRATEGY_LAB_SLIPPAGE_BPS`, `STRATEGY_LAB_HALF_SPREAD_BPS`,
`STRATEGY_LAB_COMMISSION_BPS` — a missing cost model **blocks** an arm's return
metrics rather than treating costs as zero), the evidence floors
(`STRATEGY_LAB_FLOOR_*`, `STRATEGY_LAB_CONFIDENCE_LEVEL`) and the card's
bootstrap (`STRATEGY_LAB_REPORT_BOOTSTRAP_REPS`, `STRATEGY_LAB_REPORT_SEED`).

**Every arm this integration creates is `shadow`.** Its mode is fixed at creation,
`strategy_lab/shadow.py` refuses any arm whose mode is not `shadow`, and no code
path creates a paper or live arm — only an owner promotion does (§10.1).
`/live_kill` is Phase 6's switch (§9) and is unaffected.

## 10.1 Strategy Lab paper and live tiers (Spec Q, PR 6)

Two more flags, both **false**, added by PR 6 together with the services that read
them. PR 4 deliberately shipped neither, because a flag nothing reads is a flag
nobody can trust.

| Flag | What it turns on |
|---|---|
| `STRATEGY_LAB_PAPER_ENABLED` | the post-scan **paper dispatcher** and the three scheduled execution jobs (`resume` every 30 min in market hours, stale-approval `expire` hourly, `reconcile` at 16:45 ET) |
| `STRATEGY_LAB_LIVE_ENABLED` | the Strategy Lab's **own** live gate, on top of `ALLOW_LIVE_TRADING`, `EXECUTION_MODE=live`, the kill switch, the broker's declared exit capability, and an owner promotion of the one global champion |

Each tier needs every gate below it, and that is enforced rather than advised: a
**live** arm requires `STRATEGY_LAB_PAPER_ENABLED` as well as
`STRATEGY_LAB_LIVE_ENABLED`, because the three jobs that resume, expire and
reconcile an execution are gated on the paper flag — `live on, paper off` would be
live positions nothing recovers after a restart. Paper additionally needs
`PHASE6_EXECUTION_ENABLED=true` (§9) and `EXECUTION_APPROVAL_SECRET` set — without
the secret no approval card can be minted, which is the correct failure.

**A paper arm reaches Alpaca paper and nothing else.** The arm's immutable mode
selects the venue and the venue selects the adapter, so `EXECUTION_MODE=live` and
`BROKER_PRIMARY=robinhood` do not change where a paper order goes; a mismatch is
refused before broker review or placement (Spec Q §12 invariant 11), and
`tests/test_strategy_lab_e2e.py` proves the live adapter sees zero calls in exactly
that configuration.

**A dispatch proposes; it never places.** Each proposal is a `proposed` row plus an
approval card carrying a signed, expiring, single-use, owner-bound reference. The
placement happens only when you tap Approve. A promotion is not an entry approval:
a promoted live arm with ten decisions needs ten approvals.

The paper book is its own virtual budget, independent of the shadow book and of the
production ledger (Spec Q §11): `STRATEGY_LAB_PAPER_EQUITY`,
`STRATEGY_LAB_PAPER_RISK_BUDGET`, `STRATEGY_LAB_PAPER_MAX_OPEN_POSITIONS`,
`STRATEGY_LAB_PAPER_MAX_POSITION_FRACTION`, `STRATEGY_LAB_PAPER_DAILY_NOTIONAL`,
`STRATEGY_LAB_PAPER_MAX_PROPOSALS_PER_RUN`,
`STRATEGY_LAB_PAPER_MAX_SNAPSHOT_AGE_MINUTES` (past which a decision abstains,
because the snapshot's age is the quote's age).

Promotion settings: `STRATEGY_LAB_PROMOTION_FLOOR_SHADOW_MATURED` and
`STRATEGY_LAB_PROMOTION_FLOOR_PAPER_CLOSED` are the per-tier operational minimums
a tier change is gated on and the card prints;
`STRATEGY_LAB_PROMOTION_TTL_SECONDS` is how long a confirmation stays valid; and
`STRATEGY_LAB_LIVE_RISK_BUDGET` defaults to `0.0` **on purpose** — a promoted live
champion with no budget sizes to nothing, so setting a number is its own
deliberate owner step.

Owner-only commands added with the tiers: `/promote_arm <arm> <tier> [reason]`,
`/demote_arm <arm> <tier> [reason]`, `/promotions`. They render a card and require
a confirmation; nothing auto-promotes.

**The procedure, step by step, is `docs/STRATEGY_LAB_RUNBOOK.md`** — including
what a failure looks like, how to stop, and the draft production rollout
checklist. Do not turn the paper flag on from this page; turn it on from there.

## 11. Email notifications and HTML cards (`notify/`)

Every human-facing message — approval cards, scan memos, Strategy Lab
scorecards, pages, the daily digest and the weekly report — can also arrive as a
designed HTML email linking to a full page on the workspace.
**`docs/NOTIFICATIONS.md` is the reference**; this is the variable list.

| Variable | Default | Notes |
|---|---|---|
| `NOTIFY_EMAIL_ENABLED` | `false` | The master flag. Off: no email, no card row, and the `/cards` routes are not registered. |
| `RESEND_API_KEY` | — | Resend key (`re_…`). Already on both Railway services. |
| `PAGER_EMAIL_FROM` | — | Sender; must be on a domain verified in Resend. Already set to `swingtrader@updates.readtop5.com`. |
| `PAGER_EMAIL_TO` | — | Recipient, or a comma-separated list. Already set. |
| `CARD_LINK_SECRET` | — | HMAC key for the card link. **Falls back to `EXECUTION_APPROVAL_SECRET`**, which production already carries, so the feature works the moment the flag goes on. Set it separately when you want the two to rotate independently — a card link does not expire and an approval reference lives thirty minutes. |
| `CARD_CHART_SESSIONS` | `60` | Daily bars captured into a card's chart. |

`WORKSPACE_BASE_URL` (§2) is what builds the link. It is already set on the
workspace service; **set it on the bot service too**, or email sent from the bot
(the digest, the weekly report, the scan summary) carries no link and no chart.
That degrades rather than breaks: the email still has everything the Telegram
message had.

Turning this on adds a channel. It removes nothing: Telegram still gets every
message it got before, and Telegram remains the **only** channel that can carry
an approvable card, because the approval callback arrives in the bot process.
An email has no callback and the card page is read-only, so with email on and
Telegram off, cards arrive and nothing can be approved — the workspace logs
`proposal_card_not_approvable` at startup when that is the state it is in.

To check it end to end: set the flag, redeploy, and run a proposal (§9) or wait
for the 5 PM digest. Then

```sql
SELECT kind, channel, status, provider_id, error, created_at
FROM notifications_sent ORDER BY id DESC LIMIT 10;
```

Rendered examples of every card are committed under `docs/examples/cards/`;
open the `.email.html` files in a browser to see what will arrive.

### 11a. Headless: running with no Telegram at all (`TELEGRAM_ENABLED`)

`TELEGRAM_ENABLED=false` runs the whole trading runtime with no Telegram in it.
Default is `true`, so a deployment that sets nothing behaves exactly as it did.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_ENABLED` | `true` | `false`: no token required, no `Application`, no `MessageQueue`, no polling connection, and no Telegram API call. `bot.telegram_bot` is not even imported. |
| `OWNER_ID` | — | **Required** with `TELEGRAM_ENABLED=false` *and* `PHASE6_EXECUTION_ENABLED=true`. Its only fallback is `TELEGRAM_CHAT_ID`, so without it every approval would be bound to the empty string; the process refuses to start and says so. |
| `NOTIFY_EMAIL_ENABLED` | `false` | Not enforced, but headless with no email channel means nothing reaches a human. Startup logs `no_human_channel` when that is the state. |

**What still runs**, unchanged: `TradingPipeline` and the startup position
reconciliation; `PipelineScheduler`, including scans when `SCHEDULER_ENABLED`
and the daily pre-market self-restart regardless; `OrderMonitor`,
`PositionMonitor` and `MonitorWatchdog`; the 5 PM daily digest and the Sunday
weekly report; Phase 6's `ExecutionService` and the Strategy Lab
`StrategyExecutionService` with its three jobs; and the owner `ApprovalPoller`
behind `OWNER_ACTION_POLLER_ENABLED`. The process is a plain asyncio program
with the same SIGTERM/SIGINT shutdown and the same graceful-restart path.

**What changes.**

- Every notification goes out through `notify/` — every configured channel
  *except* Telegram, whose credentials usually stay set so the switch is
  reversible. Order fills, stop breaches, regime changes and the rest become
  `alert` cards; the scan summary, the digest, the weekly report, the scorecard
  and a page keep the card kinds they already had (§11).
- Proposal cards go by email and say so: the closing note names the
  `approve_order` MCP owner tool and prints the `proposal_uid` it takes. The
  email still cannot approve anything — nothing in an email ever could.
- There is no `/live_kill`. The `kill_switch` MCP owner tool is the switch
  (§9a), so `WORKSPACE_OWNER_TOOLS_ENABLED` matters more than it did.
- The deep-research PDF arrives as an email attachment rather than a Telegram
  document. Over ~8 MB it is dropped with a log line and the email still
  arrives; a signed link is not an option, because the PDF is written to the
  bot container's filesystem and the workspace serving `/cards` cannot read it.

Startup logs one line naming the mode and the channels:
`runtime_mode telegram=false mode=headless channels=email`.

The switch-over sequence is `docs/OWNER_SETUP.md` §5 — email first, then
`OWNER_ID`, then the flag — so a channel is proven before the old one goes away.

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
   then set `COMPARABLE_BENCHMARK_SECURITY_UID` and flip
   `COMPARABLE_SETUPS_ENABLED` (§7).
7. Paper execution: `PHASE6_EXECUTION_ENABLED=true` on both services,
   `EXECUTION_APPROVAL_SECRET` set, `EXECUTION_MODE=paper`, a token with the
   `propose` scope; call `propose_order`, approve the card in Telegram, and
   watch the paper lifecycle reach `protected` (§9). Live stays off until the
   Robinhood stop probe passes.
8. Email cards: `NOTIFY_EMAIL_ENABLED=true` on both services, with
   `WORKSPACE_BASE_URL` set on the **bot** service too so the links work (§11).
   Telegram keeps working; this adds a channel rather than moving one.
9. Strategy Lab shadow: `STRATEGY_LAB_ENABLED=true` and
   `STRATEGY_LAB_SHADOW_ENABLED=true` on the bot service, then `/experiments`
   after the next scan (§10). Turn on `STRATEGY_LAB_UNIVERSE_ENABLED` only once
   the price plane and `universe_membership` are backfilled. No broker, no
   capital and no order is involved at this tier.
10. Strategy Lab paper: only after the shadow observation window (60 days / 100
   matured decisions, Spec Q §10). Paper dispatches through the Phase 6 lifecycle
   to Alpaca paper and nothing else. Promote **one** arm with `/promote_arm`, then
   `STRATEGY_LAB_PAPER_ENABLED=true`, and approve the first card by hand. Follow
   `docs/STRATEGY_LAB_RUNBOOK.md` §5 rather than this list.
11. Strategy Lab live: owner-promoted, one global champion, and gated by
    everything in §9 plus the promotion audit, the paper flag, and a non-zero
    `STRATEGY_LAB_LIVE_RISK_BUDGET`. Not reachable until the real `gtc
    stop_market` probe passes against the live Robinhood account
    (`docs/EXECUTION_LIFECYCLE.md` §6) and the micro-live canary is separately
    authorized. `STRATEGY_LAB_LIVE_ENABLED` and `STRATEGY_LAB_LIVE_RISK_BUDGET`
    both stay at their defaults until then.
