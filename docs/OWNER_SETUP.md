# Owner setup — the copy-paste path to a testing state

Written 2026-09-10 for Bryan. `docs/ENV_SETUP.md` is the reference for every
variable; `docs/POSTGRES_CUTOVER_RUNBOOK.md` is the authority on the cutover.
This file is the sequence, with the commands, and says which steps the
orchestrating session already did from its own Railway access.

Railway project `swingtrader` (`e556a6d9-2023-4c81-a031-e32e160a33be`),
environment `production`. Services: `swingtrader` (the bot), `Postgres`
(provisioned 2026-09-10, empty, nothing reads it yet), `workspace` (created
2026-09-10 with start command `python -m workspace.server`, healthcheck
`/health`, `SERVICE_ROLE=workspace`, `WORKSPACE_API_ENABLED=false`; linked to
the repo and deployed 2026-09-12 at
`https://workspace-production-6e7b.up.railway.app`, `/health` → 200).

Already set on the bot service: `SHARADAR_API_KEY` (rotate it at
https://sharadar.com/account when convenient — the value went through a chat),
`FRED_API_KEY`, `FINNHUB_API_KEY`, Alpaca paper keys, Telegram, `DATABASE_URL`
(still SQLite on the `/data` volume), and **`ALLOW_LIVE_TRADING=true`** — that
last one predates this build; Phase 6 treats it as one of three live gates, so
decide deliberately before ever setting `EXECUTION_MODE=live`.

## 1. Connect the workspace service to the repo (dashboard, 1 minute) — DONE 2026-09-12

Done: the service builds from `main`, the `SERVICE_ROLE` guard hands off to
the workspace server, `/health` returns 200 with `workspace_api_enabled: false`,
and `WORKSPACE_BASE_URL` is set on the service. Until step 2 the workspace
reads its **own ephemeral SQLite** (no volume): it sees none of the bot's data
and anything written there is lost on redeploy, which is why `WORKSPACE_API_ENABLED`
stays false and no token is issued before the cutover.

Original instructions, kept for a rebuild:

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

## 2. Postgres cutover (laptop, ~15 minutes) — DONE 2026-09-12

Executed 2026-09-12. The bot and the workspace both run on Postgres; parity
reports for the rehearsal and the real run are committed in `docs/audits/`
(`2026-09-12T153015Z-` and `2026-09-12T154146Z-sqlite-to-postgres.md`), every
table `ok`, 61 tables and 1764 rows each time. The SQLite file stays on the
`/data` volume as the archive. Rollback is still one variable:
`railway variables --service swingtrader --set DATABASE_URL=sqlite:////data/swing_trader.db`.

The instructions below are corrected against what actually happened; the
original versions had three traps in them.

### Before you copy-paste anything: three traps

**1. `#` is not a comment in an interactive zsh.** macOS ships zsh, and
`interactive_comments` is off in an interactive shell, so a pasted `#` line is
*executed*. The ones carrying parentheses — `(or versioned)`, `(dashboard: …)`
— then die with `zsh: parse error near ')'`. Every explanatory line has been
moved out of the fenced blocks for that reason: what is inside a block is meant
to be pasted, and nothing else is. If you want the old inline style back, run
`setopt interactive_comments` first.

**2. `railway ssh -- <cmd>` re-parses your command in the container.** The CLI
joins its argv with spaces and the container runs the result through `sh -c`,
so the quotes you typed are consumed by your *local* shell and never reach the
far side. `python -c "import sqlite3; x=f(1)"` arrives as bare words and fails
with ``sh: 1: Syntax error: "(" unexpected``. Three forms that do work:

```bash
railway ssh --service swingtrader -- python -m scripts.schema_status sqlite:////data/swing_trader.db
railway ssh --service swingtrader -- sh -c "\"echo 'a (b) c'\""
```

The first is a module invocation whose arguments contain no shell metacharacters
— always prefer it. The second escapes the inner quotes so they survive the
local shell. For anything longer, base64 the script and decode it on the far
side, which is immune to both layers of quoting:

```bash
B64=$(base64 < ./myscript.py | tr -d '\n')
railway ssh --service swingtrader -- sh -c "\"echo $B64 | base64 -d > /tmp/s.py && python /tmp/s.py\""
```

Verify any new form with `echo` before running it for real.

**3. The ssh channel is a PTY.** `railway ssh -- cat /data/swing_trader.db >
file.db` — the line the previous version of this document told you to run —
**produces a corrupt file**, because the PTY mangles binary and injects CR.
Text survives if you strip CR/LF. Pipe through base64 instead, and note that
macOS `base64` decodes with `-D`, not `-d`:

```bash
railway ssh --service swingtrader -- sh -c "\"gzip -9 -c /tmp/snap.db | base64 -w0\"" > /tmp/snap.b64.raw
tr -d '\r\n' < /tmp/snap.b64.raw > /tmp/snap.b64
base64 -D -i /tmp/snap.b64 -o /tmp/snap.db.gz
gunzip -c /tmp/snap.db.gz > ./swing_trader.prod.$(date +%F).db
sqlite3 ./swing_trader.prod.$(date +%F).db "pragma integrity_check; select version_num from alembic_version;"
```

That round-tripped a 4.4 MB database byte-identically (sha256 compared on both
ends), so no chunking was needed.

### Phase B — classify and rehearse (read-only against production)

Classify the live file first. Expect `state: versioned` (or `legacy` on a
pre-Alembic file). **If it says `unknown`, stop** and follow what it prints.

```bash
railway ssh --service swingtrader -- python -m scripts.schema_status sqlite:////data/swing_trader.db
```

Take a **consistent** snapshot. A raw copy of a live SQLite database with a WAL
is not consistent; `sqlite3.Connection.backup` is. Run it in the container so
nothing binary crosses the PTY:

```bash
cat > /tmp/snapshot.py <<'PY'
import sqlite3
src = sqlite3.connect("file:/data/swing_trader.db?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect("/tmp/snap.db")
src.backup(dst)
dst.commit(); dst.close(); src.close()
print("snapshot ok")
PY
B64=$(base64 < /tmp/snapshot.py | tr -d '\n')
railway ssh --service swingtrader -- sh -c "\"echo $B64 | base64 -d > /tmp/snapshot.py && python /tmp/snapshot.py\""
```

Then rehearse **inside the container, over the private network**, so the
database URL never leaves the project and no data crosses the PTY. The private
URL is on the Postgres service's Variables tab as `DATABASE_URL`; rewrite the
scheme `postgresql://` → `postgresql+psycopg://`.

```bash
railway ssh --service swingtrader -- sh -c "\"cd /app && python -m scripts.migrate_sqlite_to_postgres --source sqlite:////tmp/snap.db --target '<private psycopg URL>' --report-dir /tmp/audits\"" | tr -d '\r' | tee ./rehearsal-report.md
```

Every row must read `ok`. **Capture stdout locally, as above** — the script
also writes the report to `--report-dir` *in the container*, and that path is
wiped by the next redeploy. The printed report is the same text; strip the
trailing `report written to …` line and commit it to `docs/audits/`.

`--report-dir` matters: without it the report lands in the container's
`/app/docs/audits/`, i.e. nowhere you can commit from.

Note there is **no `DATABASE_PUBLIC_URL`** on this project's Postgres service —
only the private `DATABASE_URL`. A public URL needs a TCP proxy enabled in the
dashboard first. Running inside the container avoids needing one at all, and
avoids the egress cost.

### Phase C — cutover

The window exists so nothing is written to SQLite between the snapshot and the
flip. The runbook's instruction is to pause the bot, and that is still the
default. Two things learned on 2026-09-12:

- **`railway scale` is broken** in CLI 4.29.0 — both `railway scale` and
  `railway service scale` panic with `couldn't get regions:
  GraphQLError("Cannot query field \"railwayMetal\" on type \"Region\"")`. There
  is no CLI path to replicas 0. It is a dashboard action: service `swingtrader`
  → Settings → Deploy → Replicas.
- **Do not set `numReplicas = 0` in `railway.toml`.** That file is committed and
  Railway applies it to *every* service built from this repo, so it would take
  the `workspace` service down too, and it needs a push and a rebuild to apply
  and another to revert.

If you would rather not pause, you can **prove** the window was clean instead of
assuming it, which is what was actually done: hash a snapshot before the
migration, and re-hash the live file after it. If the two agree, nothing was
written and the migrated copy was the final state. On 2026-09-12 both were
`23455bf62d0ec26c07772c8f5d76854629c253fe284ebb76ea948b1f8297bf51`. This is only
reasonable while `SCHEDULER_ENABLED=false` and there are no open positions or
orders — check first by taking two snapshots a minute apart and confirming they
match — and it still requires that you send no Telegram command during the run.

```bash
railway ssh --service swingtrader -- sh -c "\"cd /app && python -m scripts.migrate_sqlite_to_postgres --force --source sqlite:////tmp/cut.db --target '<private psycopg URL>' --report-dir /tmp/audits\"" | tr -d '\r' | tee ./cutover-report.md
railway ssh --service swingtrader -- sh -c "\"cd /app && python -m scripts.schema_status '<private psycopg URL>'\""
```

`--force` is correct here **only** because the rehearsal already filled the
target. Never pass it once the bot has written to Postgres.
`schema_status` must say `versioned`, at head.

Then set the variables. Use `--set-from-stdin` so the URL with its password
never enters your shell history or the process list, and `--skip-deploys` on all
but the last so there is one redeploy rather than several:

```bash
printf '%s' '<private psycopg URL>' | railway variables --service swingtrader --set-from-stdin DATABASE_URL --skip-deploys
railway variables --service swingtrader --set DATA_DIR=/data
```

In the logs expect `schema_ready action=upgraded backend=postgresql`.
`action=created` means an empty schema was built — it is pointed at the wrong
database; stop. Then smoke test: `/status` in Telegram, one `/eval`, monitors
ticking, and `/health` on the workspace reporting `"backend": "postgresql"`.

Use the **private** URL for both services. The public one is for the laptop only
and costs egress.

## 3. Workspace on Postgres, token, attach a client — DONE 2026-09-12

Done: both services point at the same private Postgres URL,
`WORKSPACE_API_ENABLED` and `RESEARCH_WORKSPACE_ENABLED` are true on the
workspace, `PORTFOLIO_SYNC_ENABLED` and `RESEARCH_WORKSPACE_ENABLED` are true on
the bot, and a `claude-code` token with `read,research:write,propose` was issued
against Postgres from inside the bot container. `/health` reports
`"backend": "postgresql"` at `0011_comparable_subject_ticker`, and
`GET /v1/whoami` returns the label, the three scopes, and
`"execute_scope_exists": false`. Without a token the same route is `401`.

Two corrections to what is written below:

- **The REST `/v1` namespace exposes only `whoami`** (`workspace/app.py`). Every
  other tool is MCP. `GET /v1/portfolio_overview` is a `404` by design, not a
  fault — call it over `/mcp`.
- **`/mcp` was refusing every request** with `421 Invalid Host header` until
  2026-09-12. `FastMCP`'s `host` defaults to `127.0.0.1` and the SDK
  auto-enables DNS-rebinding protection on that default, allowing only loopback
  `Host` headers; `/health` and `/v1/...` kept working because they are ordinary
  FastAPI routes, so nothing pointed at it. `workspace/app.py` now passes
  `transport_security` explicitly, and `tests/test_workspace_mcp_host.py` sets
  `Host` explicitly so the suite can see it — the existing MCP tests could not,
  because their fixture serves on `127.0.0.1`. **`portfolio_overview` over MCP
  is unverified in production until that fix is on `main`** and the workspace
  service has redeployed from it.

Issue the token **inside the container** so the Postgres URL stays on the
private network, and keep it out of your shell history:

```bash
railway ssh --service workspace -- sh -c "\"cd /app && python -m scripts.workspace_token --issue --label claude-code --scopes read,research:write,propose --database-url '<private psycopg URL>'\""
```

Then, in the shell your client runs in:

```bash
export WORKSPACE_BASE_URL=https://workspace-production-6e7b.up.railway.app
export WORKSPACE_TOKEN=<the token printed above>
curl -s "$WORKSPACE_BASE_URL/health" | jq .
curl -s -H "Authorization: Bearer $WORKSPACE_TOKEN" "$WORKSPACE_BASE_URL/v1/whoami" | jq .
```

Original instructions, kept for a rebuild:


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

## 4. Prices and cohorts (Sharadar 10-year Prices tier) — PARTLY DONE 2026-09-12

`PRICE_PLANE_ENABLED=true` and `PRICE_PLANE_SOURCE=sharadar` are set on the bot,
and there are **real vendor prices in production**: 20,411 bars, 133 corporate
actions and 10 securities for `AAPL, BBIO, CPRT, DAL, GIS, HIMS, HNGE, OSCR,
PANW, VRTX`, back to the tier's floor of 2016-09-12, with the Spec N §4.3
reconstruction identity check passing on every name.

**Three things below do not work and are not the operator's fault.**

**`--bulk years=10` cannot run in this container.** `load_bulk_bars` →
`_read_bulk_csv` materialises every row of the whole-market zip and then builds a
`DailyBar` for each — two full in-memory copies. Instrumented, the child was at
**3.4 GB RSS 15 seconds in** and the container was SIGKILLed (exit 137) shortly
after, three times, bouncing the bot each time. It is not the cgroup's own 8 GB
`memory.max` — `memory.events` never records an `oom_kill`, and the platform
kills earlier; `memory.peak` readings taken afterwards are worthless because the
counter resets when the container restarts. `--tickers` does not help:
`backfill_bulk` filters *after* the full parse. This needs either a streaming
parse or a much larger instance, and it is why the load above went through the
per-ticker slice path instead:

```bash
railway ssh --service swingtrader -- sh -c "\"cd /app && python -u -m scripts.price_backfill --source sharadar --since 2015-01-01 --tickers AAPL,BBIO,CPRT,DAL,GIS,HIMS,HNGE,OSCR,PANW,VRTX\""
```

Those ten names are exactly the tickers in `historical_events` and
`event_outcomes`, which is what the cohort roster needs prices for.

**The benchmark cannot be loaded, so `cohort_smoke` cannot run.** Sharadar splits
equities (`stocks`/SEP) from funds (`funds`/SFP). SPY's only row in `tickers` is
`table=funds` (permaticker 118691); `stocks?ticker=SPY` is empty while
`funds?ticker=SPY` returns real bars. `data/prices/sharadar.py` hardcodes
`table=stocks` in `security_master` and reads `TABLE_STOCKS` in `daily_bars`, so
the adapter is equities-only by construction and `--tickers SPY` fails with
`tickers has no row for 'SPY'; refusing to invent a security id`. Consequently
`COMPARABLE_BENCHMARK_SECURITY_UID` is **not set**, and `cohort_smoke` stops at
`CohortContext.benchmark_security_uid is required` (Spec N §4.2, §5.2).

Adding `funds`/SFP support is a feature with its own price-adjustment semantics
and tests, not a one-line fix, so it was not attempted. Substituting some equity
as a stand-in benchmark was also not done: a cohort answer computed against a
benchmark that is not the benchmark is a wrong number, and wrong numbers are the
one thing this system is built to refuse.

**`scripts.audit_delisting_returns` used to abort on its first case, `RSH`.**
Fixed (`docs/investment-workspace/handoff/briefs/delisting-audit-symbols.md`):
the audit now resolves each case's vendor symbol before asking for bars —
`case.vendor_symbols["sharadar"]` for the seven confirmed remaps and the four
that resolve under their original ticker, and a best-effort name/`relatedtickers`
search otherwise — and reports `unresolved` (no symbol found) and
`out_of_window` (delisting predates the tier) as their own classes, distinct
from `missing`. It refuses to run (exit 2) below
`delisting_audit_min_testable_cases` (default 10) testable cases, so a run
that can only ask a handful of its twenty questions fails loudly instead of
silently passing. **Run `--resolve` before the audit itself:**

```bash
DATABASE_URL="$POSTGRES_URL" SHARADAR_API_KEY=... \
  python -m scripts.audit_delisting_returns --source sharadar --resolve
```

It prints, for every case, how it resolved (or a ready-to-paste
`vendor_symbols` line when it found a name-matched candidate), and needs a
live key — a cloud worker session normally has none, so this step is the
owner's to run, followed by a PR filling in whatever `--resolve` found. Then
run the audit itself the same way as before:

```bash
DATABASE_URL="$POSTGRES_URL" python -m scripts.audit_delisting_returns --source sharadar
```

Two independent causes remain, both verified live against the vendor on
2026-09-12 (`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md`):

- *Symbol.* `data/prices/delisting_audit_list.py` names companies by their
  pre-bankruptcy symbol; Sharadar keys many of them by a post-bankruptcy `Q`
  symbol. `RSH` → `RSHCQ` ("RADIOSHACK CORP"), and `SHLDQ`, `BBBYQ`, `SIVBQ`,
  `FTRCQ`, `RADCQ`, `BIGGQ` all exist and are already filled in. `JCP` →
  `JCPNQ` does **not** resolve by ticker or by company name, so the remap
  needs per-case judgement rather than a suffix rule; nine cases
  (`WLT, ZQK, CIE, WIN, DEAN, JCP, LK, CHK, PRTY`) still carry no
  `vendor_symbols` entry — `--resolve` is where to start on the rest.
- *Window.* Even with the right symbol, `stocks?ticker=RSHCQ` returns no rows:
  the 10-year tier starts 2016-09-12, so the 2015 cases (`RSH`, `WLT`, `ZQK`)
  need the `full` tier and now classify `out_of_window` by construction under
  the 10-year one. The 2023–2024 cases do have prices — `SIVBQ` and `BBBYQ`
  both return real bars.

This matters beyond the script: the audit list is the survivorship-bias check,
so a symbol that silently resolves to nothing is the failure mode it exists to
catch — which is exactly why an unresolved symbol is now its own class rather
than indistinguishable from "the vendor has it and it's empty".

Run the backfill **inside the bot container** — it already holds
`SHARADAR_API_KEY` and, after §2, the Postgres `DATABASE_URL`, and there is no
public Postgres URL for the laptop to use.

**Do not background it.** `nohup … &` and `setsid … &` both look like they work
and both silently lose the job: Railway kills the entire exec-session process
tree when `railway ssh` disconnects. Proved with a control — two `sleep 90`
markers, one under `nohup` and one under `setsid`, were both dead after the
session closed, and the first backfill attempt died the same way after
downloading 351 MB of `stocks.zip` and writing zero rows. It is not an OOM
(`memory.events` showed `oom_kill 0` against the 8 GB cgroup limit).

So hold the session open in the foreground for the whole run, and keep the
output on the laptop where a redeploy cannot wipe it:

```bash
railway ssh --service swingtrader -- sh -c "\"cd /app && python -m scripts.price_backfill --source sharadar --bulk years=10\"" 2>&1 | tee ./backfill.log
```

Watch progress from a second terminal by counting rows rather than by reading
the log — the script prints its summary only at the end:

```bash
railway ssh --service swingtrader -- sh -c "\"cd /app && python -c \\\"from sqlalchemy import create_engine,text; print(create_engine('<private psycopg URL>').connect().execute(text('select count(*) from price_bars')).scalar())\\\"\""
```

Note the shape of the work: `backfill_bulk` downloads both zips (fast, about a
minute), then parses **the entire `stocks.zip` into memory** before it writes
anything, and does the whole load in one session. So `price_bars` stays at 0 for
a long time and then moves — zero rows is not evidence of a stall until the
parse is done.

**Set every variable you need on the bot service *before* you start it.** Any
variable change redeploys the service and kills whatever is running in its
container, including a half-finished backfill.

Original instructions:


`SHARADAR_API_KEY` is already on the bot. The adapter targets Sharadar's
direct API (`https://api.sharadar.com/v1.0`, merged as #70); `docs/vendors/sharadar.md`
is the vendor reference. Then:

```bash
railway variables --service swingtrader --set "PRICE_PLANE_ENABLED=true" --set "PRICE_PLANE_SOURCE=sharadar"
# from the laptop, against Postgres:
DATABASE_URL="$POSTGRES_URL" SHARADAR_API_KEY=... python -m scripts.price_backfill --source sharadar --bulk years=10
DATABASE_URL="$POSTGRES_URL" SHARADAR_API_KEY=... python -m scripts.audit_delisting_returns --source sharadar --resolve
DATABASE_URL="$POSTGRES_URL" python -m scripts.audit_delisting_returns --source sharadar
# pick the benchmark row (SPY's security_uid in price_bars), then on the workspace:
railway variables --service workspace --set "COMPARABLE_SETUPS_ENABLED=true" --set "COMPARABLE_BENCHMARK_SECURITY_UID=<uid>"
DATABASE_URL="$POSTGRES_URL" python -m scripts.cohort_smoke     # Spec N §11: one insufficient, one ok, hand-checked
```

## 5. Submitting a trade (paper) — FLAGS SET 2026-09-12, TRADE NOT SUBMITTED

`PHASE6_EXECUTION_ENABLED=true` and a fresh 64-character hex
`EXECUTION_APPROVAL_SECRET` are set on **both** services, and
`EXECUTION_MODE=paper` on the bot. `EXECUTION_MODE` is **not** `live` and
`ALLOW_LIVE_TRADING` was left exactly as it was found.

The proposal itself is yours to make: it arrives in Telegram as a card for you
to approve, and an agent must not both propose and approve. From an attached
client, `propose_order` with `ticker`, `entry`, `stop`, `risk_fraction` — and no
quantity, because the execution service sizes it.

### Where the card goes

The card is sent by the **workspace** process, which is a separate process from
the bot, so the workspace needs its own delivery credentials. There are two
channels and they are not interchangeable.

**Telegram is the only channel that can carry an approvable card**, because the
callback you tap arrives in the *bot* process, which is the one polling
Telegram. The workspace service therefore needs `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` too. Without them the workspace logs
`proposal_card_channel_unconfigured` at startup and every `propose_order`
creates a `proposed` row that nobody can approve. Sending messages does not
conflict with the bot's polling (only `getUpdates` is exclusive). Use Railway
variable references so the values are not copied:

```bash
railway variables --service workspace --set 'TELEGRAM_BOT_TOKEN=${{swingtrader.TELEGRAM_BOT_TOKEN}}' --set 'TELEGRAM_CHAT_ID=${{swingtrader.TELEGRAM_CHAT_ID}}'
```

Then confirm the warning is gone from `railway logs --service workspace` after
the redeploy.

**Email is how you read it.** Since you do not use Telegram day to day, turn the
email channel on and the same card arrives in your inbox as designed HTML, with
a link to the full page on the workspace — the chart, the complete risk math,
the cohort evidence with its warnings verbatim, the thesis and its invalidators,
the exposure impact, and the provenance of every number. Railway already carries
`RESEND_API_KEY`, `PAGER_EMAIL_FROM` and `PAGER_EMAIL_TO` on both services, so
this is one flag:

```bash
railway variables --service workspace --set "NOTIFY_EMAIL_ENABLED=true"
railway variables --service swingtrader --set "NOTIFY_EMAIL_ENABLED=true" --set 'WORKSPACE_BASE_URL=${{workspace.WORKSPACE_BASE_URL}}'
```

`WORKSPACE_BASE_URL` on the bot service is what makes the links in email sent
*from the bot* (the digest, the weekly report, the scan summary) work. Without
it those emails still arrive; they just carry no link and no chart.

The card link is signed with `CARD_LINK_SECRET`, falling back to the
`EXECUTION_APPROVAL_SECRET` you already set, so nothing else is needed. The page
is read-only and the link is its credential — it exposes only what the email
already contains. `docs/NOTIFICATIONS.md` argues that trade in full, and
`docs/examples/cards/` has a rendered example of every card to open in a
browser first.

**The email cannot approve anything**, and that is deliberate: an email has no
callback and the card page has no route that writes. What you approve *with* is
either the Telegram button or the `approve_order` MCP owner tool in your
coding-agent chat (§5a below, and `docs/ENV_SETUP.md` §9a); the email and the
page are how you read the card, never how you release it.

### 5a. Going headless: switching Telegram off

Once email is proven and the owner tools are on, `TELEGRAM_ENABLED=false` takes
Telegram out of the bot process entirely — no token required, no polling
connection, and a plain asyncio runtime that still runs the scheduler, the
monitors, the digests, Phase 6 and the approval poller. `docs/ENV_SETUP.md` §11a
is the reference; this is the order to do it in, and the order matters because
each step proves the next one has somewhere to land.

**1. Prove the email channel first.** Do not skip this. Turn `NOTIFY_EMAIL_ENABLED`
on (above), wait for the 5 PM digest or run a `propose_order`, and confirm the
message actually arrives in your inbox. Then check the delivery log:

```sql
SELECT kind, channel, status, provider_id, error, created_at
FROM notifications_sent ORDER BY id DESC LIMIT 10;
```

A `sent` row on `channel='email'` is the proof. Turning Telegram off before you
have one leaves the runtime with no way to reach you at all.

**2. Set `OWNER_ID` explicitly**, to the `TELEGRAM_CHAT_ID` you have been using —
so approvals minted before the switch still verify — on **both** services:

```bash
railway variables --service swingtrader --set 'OWNER_ID=${{swingtrader.TELEGRAM_CHAT_ID}}'
railway variables --service workspace   --set 'OWNER_ID=${{swingtrader.TELEGRAM_CHAT_ID}}'
```

It must be byte-identical on the two, or a card minted by one fails
`owner_mismatch` on the other. With Phase 6 on and no owner id resolvable, the
bot refuses to start and logs `owner_id_required_headless` — a refusal, not a
silent default, because an approval bound to the empty string is bound to
nobody.

**3. Turn the owner tools on** (`WORKSPACE_OWNER_TOOLS_ENABLED=true` on the
workspace, `OWNER_ACTION_POLLER_ENABLED=true` on the bot — `docs/ENV_SETUP.md`
§9a) and record one decision through them while Telegram is still up, so you
have seen the path work before it is the only one.

**4. Then, and only then, switch Telegram off:**

```bash
railway variables --service swingtrader --set "TELEGRAM_ENABLED=false"
```

Leave `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set. They cost nothing, no
Telegram call is made while the flag is false, and leaving them is what makes
the switch reversible with one variable.

**What to check after the redeploy.** `railway logs --service swingtrader` should
show `runtime_mode telegram=false mode=headless channels=email` and then
`starting_headless_runtime`. A `no_human_channel` warning means step 1 was
skipped — nothing will reach you and the structured log is the only record.

**What changes for you.** There is no `/live_kill`: `kill_switch` is an MCP owner
tool and engaging it needs no confirmation from anyone. Proposal emails name
`approve_order(proposal_uid="…")` and print the uid. The deep-research PDF
arrives as an attachment instead of a Telegram document.

The **workspace** service keeps its `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
independently of this flag — that is a separate process with its own variables,
and `TELEGRAM_ENABLED` is read by the bot. Clear them there if you want the
workspace to stop sending Telegram cards too.


```bash
railway variables --service workspace --set "PHASE6_EXECUTION_ENABLED=true" --set "EXECUTION_APPROVAL_SECRET=<long random>"
railway variables --service swingtrader --set "PHASE6_EXECUTION_ENABLED=true" --set "EXECUTION_APPROVAL_SECRET=<same>" --set "EXECUTION_MODE=paper"
```

From an attached client: `propose_order` with `ticker`, `entry`, `stop`,
`risk_fraction` (no quantity — the service sizes it). A card arrives — in
Telegram, in your inbox, or both — and you approve it on the Telegram button or
with the `approve_order` owner tool. The bot places the paper entry on Alpaca,
polls the fill, places the `gtc` `stop_market`, reads it back, and the proposal
reaches `protected`. `docs/EXECUTION_LIFECYCLE.md` has every refusal you might
see.

**Live** additionally needs, in this order: the Robinhood `gtc stop_market`
probe from `docs/EXECUTION_LIFECYCLE.md` §6 passing on the Agentic account;
`docs/robinhood/tool_schemas.json` committed (you generated it 2026-09-08);
`EXECUTION_MODE=live` on the bot; the kill switch off (`/live_kill off`, or
`kill_switch("off")` headless); `ALLOW_LIVE_TRADING=true` (already set — see the
note at the top).

## 6. Evidence planes and Strategy Lab shadow (no capital involved) — FLAGS DONE 2026-09-12, FIXTURES NOT RECORDED

`SEC_USER_AGENT`, `PLANE_SEC_MINIMAL_ENABLED`, `PLANE_FILINGS_ENABLED`,
`PLANE_MACRO_VINTAGE_ENABLED`, `PLANE_NEWS_ENABLED`, `STRATEGY_LAB_ENABLED` and
`STRATEGY_LAB_SHADOW_ENABLED` are all true on the bot.
`STRATEGY_LAB_UNIVERSE_ENABLED` is deliberately still unset.

**The fixture line below does not work as written, and the macro fixtures must
not be replaced.** Three separate problems, all found on 2026-09-12:

1. `scripts.record_macro_fixtures` **requires `--series`**; bare, it exits on a
   missing argument.
2. Most of the series in `tests/fixtures/macro/README.md` **cannot be recorded
   at all**. `SP500` is not in ALFRED (`400 the series does not exist in
   ALFRED`), and `VIXCLS`, `DGS10`, `DGS3MO` and `DGS2` each exceed ALFRED's
   2000-vintage-date ceiling for this file type (3931 to 5108 vintages), because
   `get_series_all_releases` requests the full real-time period. Only `CPIAUCSL`
   and `USREC` record successfully.
3. Recording those two and committing them **would break
   `tests/test_macro_plane.py::test_usrec_only_via_vintage`**, which asserts
   `set(series_as_of(USREC, 2024-06-30).values()) == {0.0}`. Real USREC carries
   579 rows valued `1.0` at that vintage — every NBER recession back to 1854.
   The synthetic fixture's invented 2024 recession is the thing under test; real
   data does not contain it. Verified by recording to a scratch directory and
   checking, not by reasoning about it.

So the macro fixtures stay synthetic until someone re-writes those assertions
against real vintages, which is a change to the tests and belongs in its own
pull request. The one claim worth keeping: the synthetic set says January 2024
CPI first printed on 2024-02-13, and ALFRED agrees.

If you do record anything, write it to a scratch `--out` directory first and
diff it before it goes near `tests/fixtures/`.

**One more trap for any laptop-side script:** `config/settings.py` calls
`load_dotenv(override=True)`, so the repo's gitignored `.env` **overrides your
exported environment**. It has `FRED_API_KEY=` empty and `DATABASE_URL` pointing
at SQLite, which is why a correctly-exported key still fails with `FRED_API_KEY
is unset`. Either put the value in `.env` (it is gitignored) or neutralise the
loader for the run.

Original instructions:


```bash
railway variables --service swingtrader --set "SEC_USER_AGENT=Bryan Niyonzima niyonzimabryan@gmail.com" \
    --set "PLANE_SEC_MINIMAL_ENABLED=true" --set "PLANE_FILINGS_ENABLED=true" \
    --set "PLANE_MACRO_VINTAGE_ENABLED=true" --set "PLANE_NEWS_ENABLED=true"
# record real fixtures once from the laptop and push (docs/ENV_SETUP.md §3, §5):
python -m scripts.record_filings_fixtures && python -m scripts.record_macro_fixtures
railway variables --service swingtrader --set "STRATEGY_LAB_ENABLED=true" --set "STRATEGY_LAB_SHADOW_ENABLED=true"
# /experiments in Telegram after the next scan — or experiments_status over MCP,
# which is what you use once TELEGRAM_ENABLED=false (§5a).
# STRATEGY_LAB_UNIVERSE_ENABLED only after the price backfill.
```

## Open items nobody can do but you

- Robinhood: fund the Agentic account; re-run `scripts/robinhood_auth.py` when
  the token lapses (~weekly idle); the live stop probe.
- Confirm the Rule 10b5-1 element and name the tracked investors (Spec O).
- Decide `ALLOW_LIVE_TRADING` deliberately.
