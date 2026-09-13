# Investment Workspace build — handoff

**Purpose.** Everything a person (or a fresh agent session in Cursor, Codex, or
Claude Code) needs to finish the build if the orchestrating session stops. It is
updated in every integration commit; the "Last updated" line says how fresh it
is. If it is more than a few hours old, trust `git log origin/main` and the
open-PR list over this file.

**Last updated:** 2026-09-13 15:50 UTC — §7 rewritten after executing the
owner turn-on sequence steps 1–5 against production (headless is live).
Prior update 07:40 UTC, by the orchestrating session
(`session_01F6Ca8hXxdGkaYhPQ6id9Q4`).
Bryan's laptop (was 2026-09-12 06:15 UTC, orchestrating session
`session_01F6Ca8hXxdGkaYhPQ6id9Q4`).

> **Read this first:**
> [`OWNER_SETUP_EXECUTION_2026-09-12.md`](OWNER_SETUP_EXECUTION_2026-09-12.md) —
> `docs/OWNER_SETUP.md` §2–§6 were executed against production on 2026-09-12.
> The Postgres cutover is **done**; the workspace has a token; real Sharadar
> prices are loaded. Three things are blocked and one production bug was found
> and fixed on this branch: **the MCP tool surface was returning `421` to every
> request and is still down in production until this PR merges and the workspace
> redeploys from `main`.** That report is the authority on what is and is not
> verified; §8 below is the dated summary.

## 1. Where main is

| | |
|---|---|
| `main` head | #74 (Strategy Lab 6) merged on top of `816e499`; this docs PR next |
| Alembic head | `0011_comparable_subject_ticker` (single); `0012_notify_email_cards` on `claude/notify-email-cards` |
| Tests | 1,793 on SQLite (3 Postgres-only skips); CI: 4 shards per engine, ~8 min |
| CI | `.github/workflows/ci.yml` — sqlite + postgres matrix, Python **3.12** |

Merged, in order: Phase 0a/0b, 3a, 3b-core, 3p, 1, 2, 4, P, Strategy Lab 1 (#54),
ENV_SETUP (#52), rulings (#55), **Phase 3c (#56), Phase 6 (#57), Strategy Lab 2
(#58), rulings (#59), handoff (#62), Strategy Lab 3 (#60), Sharadar reference (#63), evidenced-budget closure (#61), Strategy Lab 4 (#64), orchestrated-build skill (#67), OWNER_SETUP (#69), mirror-test fix (#71), CI sharding (#66), Sharadar direct-API port (#70), Strategy Lab 5 (#65), service-role guard (#68), rulings PRs 2–5 (#73), Strategy Lab 6 (#74)**.

## 2. What is in flight (the notifications sprint, spawned 2026-09-13 02:15Z)

Owner decisions that started it (2026-09-13): Bryan does not use Telegram and
will not adopt it; every human-facing message goes to email (Resend, from
`swingtrader@updates.readtop5.com`, keys already on both Railway services);
approval happens **in his coding-agent chat** through an `admin`-scoped MCP
tool, with no confirmation code (he declined one; single user, private
deployment; the specs' "never a tool an agent can call" is overridden by owner
ruling, recorded in Spec L §10 and Spec K §10 by #82); owner mutations (kill
switch, promotions) also go over MCP; cards are designed HTML emails with a
signed full-page view served by the workspace.

| PR / branch | What | Session | Model | State |
|---|---|---|---|---|
| #80 `claude/system-overview-doc` | `docs/SYSTEM_OVERVIEW.md`; published to Google Drive: https://docs.google.com/document/d/1_dy3kIiaM3VwkivuIPjbnH1p1OKdI6w5ohXJ3CxpJL0/edit | `session_01D8jUpshohWT44o1L6gLPHd` | sonnet | **merged** |
| #81 `claude/notify-email-cards` | `notify/` package, Resend channel behind `NOTIFY_EMAIL_ENABLED`, HTML card renderer + PNG chart, signed `/cards/<uid>` page, `cards` + `notifications_sent` tables (`0012_notify_email_cards`) | `session_01NSbbAvFecNAkxaZSQ6v8P9` | opus | **merged** |
| #82 `claude/owner-tools-mcp` | `OWNER_ID`; ten `admin`/`read` owner tools behind `WORKSPACE_OWNER_TOOLS_ENABLED`; `orchestrator/approval_poller.py` behind `OWNER_ACTION_POLLER_ENABLED`; `owner_actions` table (`0012_owner_control_surface`); Spec K §10 + L §10 rulings; merge revision `0013_merge_notify_owner` added by the orchestrator | `session_01A4B5sYhhr8ZDnRFHkVq4xY` | opus | **merged** |

| `claude/headless-runtime` | `TELEGRAM_ENABLED` (default true); headless `main.py` with scheduler, monitors, execution services and the approval poller; channel-agnostic `NotificationManager` over `notify/`; headless email card sender; `OWNER_ID` refusal | `session_01Fe9DRSdz2HRirVDsj3yogd` | opus | **merged (#85)** |

Alembic head on `main`: `0013_merge_notify_owner` (single). **The notifications
sprint is complete**: #80 (overview), #81 (email + cards), #82 (owner tools +
poller), #85 (headless runtime) are on `main`. No worker is building. Nothing
in production changed yet — every flag is still off; the owner turn-on sequence
is in §7 below and in `docs/OWNER_SETUP.md` §5.

Note from #85: `main.py` on `main` between #82 and #85 had a function-local
`import os` that would have crashed startup the moment
`OWNER_ACTION_POLLER_ENABLED=true` was set; #85 removed it. Set that flag only
on a bot deploy that includes #85 (check `railway logs` for `runtime_mode`).

Previously described as in flight: the headless runtime — `TELEGRAM_ENABLED=false` runs scheduler,
monitors, and the approval poller with no Telegram token, and
`NotificationManager` routes through `notify/`. Then Bryan's final steps:
`NOTIFY_EMAIL_ENABLED`, `OWNER_ACTION_POLLER_ENABLED` (bot), then
`WORKSPACE_OWNER_TOOLS_ENABLED` (workspace), an `admin` token, attach, first
paper trade (`docs/ENV_SETUP.md` §9a and §11, `docs/OWNER_SETUP.md` §5).

## 2b. Research-engine sprint — COMPLETE 2026-09-13 (#89, #88, #90 merged)

The three engineering blockers on the research engine, each with a brief under
`briefs/`, none overlapping the headless-runtime worker (they touch
`data/prices/`, `scripts/`, `comparables/` readers and their tests; not
`main.py` or `bot/`):

| Brief | Fixes | Model | Session |
|---|---|---|---|
| `briefs/sharadar-funds-benchmark.md` | SPY as the benchmark: `asset_class` on securities (`0014`), `funds`/SFP in the adapter with the §4.3 three-series contract, funds excluded from universes, backfill + uid printout, Spec N ruling | opus | `session_01A5zDwgGTNpxEauSWa6MqK1` — **merged #89** |
| `briefs/bulk-backfill-streaming.md` | `--bulk years=10` without OOM: stream → SQLite staging → per-ticker derive/upsert, `--tickers` during staging, `--resume`, an RSS guard with a measured bound | sonnet | `session_01RChYn3xaQXfyrxmwtWwYRW` — **merged #90** |
| `briefs/delisting-audit-symbols.md` | The survivorship audit resolves Sharadar's `Q` symbols, adds `unresolved` and `out_of_window` classes, refuses below a testable minimum, `--resolve` mode for the owner's keyed agent | sonnet | `session_01CBkhcRBftv3LNtc2ubLmYj` — **merged #88** |

Alembic head after the sprint: `0014_securities_asset_class` (single). Owner
steps now (`docs/OWNER_SETUP.md` §4, `docs/ENV_SETUP.md` §7a): set
`PRICE_PLANE_FUNDS_ENABLED=true` on the bot *before* starting any backfill (a
variable change redeploys and kills running work); backfill SPY with
`--asset-class fund`, read the printed uid, set
`COMPARABLE_BENCHMARK_SECURITY_UID` on the workspace, run `cohort_smoke`; run
`--bulk years=10 --max-rss-mb 1500` from the container (streamed; measured 98 MB
peak on a synthetic 5M-row zip vs 3.37 GB before; `--resume` on a drop); run
the audit's `--resolve` with the key and open the follow-up PR filling the nine
unmapped `vendor_symbols`.

Not verified by the workers: no live Sharadar bulk zip (free key 401s on bulk);
no fund split ever observed (SPY has none); whether `actions` carries fund
distributions (free key 403s); the resolver against the nine unmapped cases;
whether the staging/checkpoint file survives a `railway ssh` drop.

## 3. What is left, in order

1. The owner summary (§7 below is the skeleton; delivered in chat by the
   orchestrator once this PR merges).
2. Owner-side, in `docs/OWNER_SETUP.md` order: link the repo to the Railway
   `workspace` service (safe now that #68 is on `main`), the Postgres cutover,
   workspace variables + token, Sharadar backfill, paper trade submission.

Known follow-ups not blocking a usable system: rename `Settings.nasdaq_data_link_api_key`
(the adapter reads `SHARADAR_API_KEY` first); `memos` has no run id (PR 4 pairs
memo to ledger row by time); CSCV/PBO in `comparables/inference.py`; the
`analog_ranker` post-event feature; `docs/robinhood/tool_schemas.json` still
uncommitted (owner).

## 4. How to integrate a PR (the recipe that has worked ten times)

```
git fetch origin main                                  # ALWAYS first
git worktree add /tmp/wt-<name> origin/<branch>
cd /tmp/wt-<name> && git checkout -B <branch> origin/<branch>
git merge origin/main --no-edit
```

Resolve conflicts by these rules, never by blind textual union:

- `database/models.py`: rebuild as **main's file plus the branch's appended
  block** (every phase appends classes at the end; `git diff <base> <branch> --
  database/models.py` gives the block). Patch any new imports at the top.
- `workspace/tools.py`: keep main's `register()` / `registered_tools()` shape
  (`names = REGISTERED_TOOLS; if <flag>: names += ...`) and splice the branch's
  flag-gated registration in as one more `if`. Append the branch's helper
  functions at the end.
- `workspace/app.py`: keep main's shape; add the branch's health-field lines.
- `database/schema.py`: keep the replay-based adoption on main.
- `config/settings.py`, `.env.example`, `todoscratchpad.md`, docs: union.
- Two Alembic heads → `alembic merge -m "..." --rev-id 00NN_merge_<phase>
  <head1> <head2>`, rename the generated file to `00NN_merge_<phase>.py`, and
  list it in `migrations/README.md`.
- Tests that hard-code a migration head: make them graph-aware (see
  `tests/test_strategy_lab_migration.py`).

Validate on **Python 3.12** (CI's version; `sum()` of floats differs from 3.11
and has already flipped one test):

```
python3.12 -m venv /tmp/venv312 && /tmp/venv312/bin/pip install -r requirements.txt
/tmp/venv312/bin/python -m compileall -q .
/tmp/venv312/bin/python -m unittest discover -s tests -p "test_*.py"   # ~14 min; expect 3 skips
```

Push the merge commit to the PR branch, wait for CI (sqlite + postgres) to be
green, merge with a merge commit (not squash — the merge revisions matter).
Never weaken a test to get green.

## 5. Resuming a child session that hit the usage limit

Cloud sessions cannot message the parent. A child that shows
`post_turn_summary: "You've hit your session limit · resets HH:MM"` is idle,
not dead. From any session with the `Claude_Code_Remote` MCP: create a Routine
bound to it (`create_trigger` with `persistent_session_id`, either poke-only
and then `fire_trigger`, or `run_once_at` the reset time) whose prompt says:
run `git status` and `git log --oneline -5` first; if the container was
reclaimed, `git fetch` + checkout the branch and rebuild only what is not
pushed, from the transcript; commit and push after each coherent unit; then
finish the brief and open the PR. Without that MCP: open a fresh session with
the brief and tell it the branch already carries N commits.

## 6. Gotchas learned tonight

- Pushes to a PR branch trigger the `pull_request` workflow whoever pushes
  them (earlier note to the contrary was wrong); `workflow_dispatch` is also
  enabled. Local validation on 3.12 stays the first gate.
- The Postgres CI job was cancelled at exactly the 20-minute cap on #60; the
  cap is 40 now. The real fix is the CI-speed PR.
- `pkill -f` with the test command string kills your own shell.
- The PR bodies say "do not merge / for review" — that is the children's
  default courtesy; the owner's standing instruction is to merge everything
  that validates.

## 7. Owner actions — the turn-on sequence (2026-09-13) — STEPS 1–5 EXECUTED

**Executed against production on 2026-09-13, 15:50 UTC**, by a Claude Code
session with Railway access, on Bryan's instruction. The bot is now **headless**:
no Telegram, email as the only human channel, approvals over MCP. Steps 6 and 7
are the owner's and are **not** done.

| # | Change | Service | Proof in the logs |
|---|---|---|---|
| 1 | `NOTIFY_EMAIL_ENABLED=true` | both | workspace `workspace_starting approval_card_channel=email card_page=True`; bot `runtime_mode channels="telegram,email"` |
| 1 | `WORKSPACE_BASE_URL=${{workspace.WORKSPACE_BASE_URL}}` | bot | resolved to `https://workspace-production-6e7b.up.railway.app` |
| 2 | `OWNER_ID=${{swingtrader.TELEGRAM_CHAT_ID}}` | both | byte-identical on the two (sha256 compared, value never printed) |
| 3 | `OWNER_ACTION_POLLER_ENABLED=true` | bot | `approval_poller_wired interval_seconds=20` → `approval_poller_started` → `approval_poller_ready` |
| 4 | `WORKSPACE_OWNER_TOOLS_ENABLED=true` | workspace | `/health` `mcp.tools` went 13 → **23**; all ten owner tools registered |
| 5 | `TELEGRAM_ENABLED=false` | bot | `runtime_mode telegram=false mode="headless" channels="email"`, `starting_headless_runtime`, `proposal_card_email_registered_headless approval_route="mcp"` |

No `no_human_channel`, no `owner_id_required_headless`, no
`notify_email_channel_unconfigured`. `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` were **left set** on the bot, so the switch reverts with one
variable: `railway variables --service swingtrader --set TELEGRAM_ENABLED=true`.

**Hard limits honoured, verified by re-reading both services afterwards:**
`EXECUTION_MODE=paper` (never touched), `ALLOW_LIVE_TRADING=true` left exactly
as found (it predates this build), `STRATEGY_LAB_LIVE_ENABLED`,
`STRATEGY_LAB_LIVE_RISK_BUDGET` and `STRATEGY_LAB_PAPER_ENABLED` all still
unset. No secret was printed to a transcript or written to the repo.

### Two things the next session must know

**1. The email channel is configured but has never actually delivered.** This is
the one gap in the sequence. `docs/OWNER_SETUP.md` §5a step 1 says to prove it
*before* flipping Telegram off; the session tried to send a test through
`notify/` from inside the workspace container and the sandbox permission
classifier blocked the command. Bryan chose to flip anyway, knowing it reverts
with one variable. So **`notifications_sent` has no `sent` row on
`channel='email'` yet**, and the first real proof will be the first proposal
card or the 5 PM digest. If nothing arrives, that is where to look first:

```sql
SELECT kind, channel, status, provider_id, error, created_at
FROM notifications_sent ORDER BY id DESC LIMIT 10;
```

`RESEND_API_KEY`, `PAGER_EMAIL_FROM` (`swingtrader@updates.readtop5.com`) and
`PAGER_EMAIL_TO` (`niyonzimabryan@gmail.com`) are all set on both services and
`email_configured()` passes, so the configuration is not the suspect — delivery
is unverified, not known-broken.

**2. `proposal_card_not_approvable` on the workspace is expected, not a fault.**
It fires because the workspace has no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`,
so a card it mints cannot carry an inline approve button. That is the intended
headless shape: the approval path is the `approve_order` MCP owner tool, which
step 4 registered. Do **not** "fix" it by putting Telegram credentials on the
workspace.

### Step 6 — the token (owner's, not done)

Deliberately left to Bryan. `docs/WORKSPACE_ACCESS.md` §1 and `AGENTS.md` §4
both say an `admin` token is the owner's and is **never** placed in an agent's
environment, and printing one into a chat transcript is the thing that policy
exists to prevent. Issue it into a shell you are sitting in front of:

```bash
railway ssh --service workspace -- sh -c "\"cd /app && python -m scripts.workspace_token --issue --label bryan-admin --scopes read,research:write,propose,admin\""
```

Note the existing token inventory (`--list`, no secrets): one active token,
`claude-code`, `read,research:write,propose`, last used 2026-09-12 15:46 UTC.
Its secret is unrecoverable — printed once, only the digest is stored — so a
session that needs one issues a fresh agent-scoped token, never an admin one.

**All three calls in step 7 need only the `read` scope** (`whoami`,
`portfolio_overview` and `proposals_pending` are all `READ` in
`workspace/scopes.py::TOOL_SCOPES`), so an admin token is *not* required to
verify the attachment. It is required only for `approve_order` and the seven
other admin tools.

### Step 7 — attach and verify (not done)

No agent session can do this for you without a token in its environment. The
exports, then a **fresh** session, because `.mcp.json` is expanded at startup:

```bash
export WORKSPACE_BASE_URL=https://workspace-production-6e7b.up.railway.app
export WORKSPACE_TOKEN=<the token printed above>
```

Then open the repo and call `whoami`, `portfolio_overview`, `proposals_pending`.
A session started without those two prints `swingtrader-workspace
(INVALID_CONFIG)` and has no `mcp__swingtrader-workspace__*` tools at all —
which is exactly the state the executing session was in, and why step 7 is open.

### Step 8 — the first paper trade (Bryan's alone)

`propose_order` → read the card in the inbox → `approve_order(proposal_uid=...)`.
An agent must not both propose and approve. The poller (20 s) picks the decision
up and the execution service places on Alpaca paper. This is also what finally
proves the email channel.

Still owner-only and untouched: the Robinhood Agentic account and the live
`gtc stop_market` probe (`docs/EXECUTION_LIFECYCLE.md` §6); the Rule 10b5-1
element and tracked investors (Spec O); the `ALLOW_LIVE_TRADING` decision;
rotating the Sharadar key; `SCHEDULER_ENABLED` (still `false`, so scans and
therefore Strategy Lab shadow are off). Next engineering sprint: §2b.

## 8. Change log of this file
- 2026-09-13 15:50Z — **the notifications sprint was turned on in production.**
  `docs/OWNER_SETUP.md` §5/§5a steps 1–5 executed: email on both services,
  `OWNER_ID` matched across both, the approval poller wired, the ten owner tools
  registered (13 → 23 MCP tools), and `TELEGRAM_ENABLED=false`. The bot logs
  `runtime_mode telegram=false mode=headless channels=email`. Email delivery is
  configured but **not yet proven** — see §7. Steps 6–8 (token, attach, first
  paper trade) remain the owner's.

- 2026-09-10 05:20Z — created, after #56–#59 merged; #60/#61 open; SL5 and
  CI-speed building.
- 2026-09-10 05:35Z — #62 (this file) and #60 merged; SL4 spawned; #61 waiting
  on a Postgres CI run under the 40-minute cap.
- 2026-09-10 07:10Z — `data/prices/sharadar.py` ported from Nasdaq Data Link to
  Sharadar's direct API (`https://api.sharadar.com/v1.0`), against payloads
  recorded live with the public `test-api-key` under
  `tests/fixtures/sharadar_direct/`. Added `SharadarPricePlane.bulk_download`
  and `--bulk years=5|10|full` on `scripts/price_backfill.py` for the 10-year
  Prices tier. `PRICE_PLANE_SOURCE=sharadar` no longer needs the port; it
  needs `SHARADAR_API_KEY` (`docs/ENV_SETUP.md` §6).
- 2026-09-10 11:30Z — #74 (Strategy Lab 6) merged; the build queue is empty.
  PR 6's rulings appended to Spec Q §21. What remains is owner-side
  (`docs/OWNER_SETUP.md`), plus the non-blocking follow-ups in §3.
- 2026-09-12 06:15Z — Bryan linked the Railway `workspace` service to the repo;
  first deploy SUCCESS, `/health` 200 at
  `https://workspace-production-6e7b.up.railway.app` (ephemeral SQLite until the
  Postgres cutover). `WORKSPACE_BASE_URL` set on the service. Next owner step:
  `docs/OWNER_SETUP.md` §2.
- 2026-09-12 15:44Z — **OWNER_SETUP §2 done: the Postgres cutover is executed.**
  Rehearsal and cutover both PASS, 61 tables / 1764 rows, every row `ok`; reports
  committed under `docs/audits/`. The bot logs `schema_ready action=upgraded
  backend=postgresql` and the workspace `/health` reports
  `"backend": "postgresql"` at `0011_comparable_subject_ticker`. The SQLite file
  stays on the `/data` volume as the archive and rollback is one variable. The
  bot was **not** paused: `railway scale` panics on CLI 4.29.0, so the window was
  *proved* clean instead — a snapshot hashed before the migration and the live
  file re-hashed after it were byte-identical
  (`23455bf6…bf51`), with `SCHEDULER_ENABLED=false` and no holdings or open
  orders. `docs/OWNER_SETUP.md` §2 and the runbook now carry the three shell
  traps that cost the most time (zsh `#`, `railway ssh` re-parsing argv through
  `sh -c`, and the PTY corrupting binary output).
- 2026-09-12 15:46Z — **OWNER_SETUP §3 done: workspace on Postgres, token
  issued.** `claude-code`, scopes `read,research:write,propose`. `GET /v1/whoami`
  returns the label, the scopes, and `"execute_scope_exists": false`; no token is
  `401`. Note the REST `/v1` namespace serves **only** `whoami` — every other
  tool is MCP, so `/v1/portfolio_overview` is a `404` by design.
- 2026-09-12 15:50Z — **Production bug found and fixed: the MCP surface was
  unreachable.** Every request to `/mcp`, GET or POST and with a valid token,
  returned `421 Invalid Host header`. Not Railway: `workspace/app.py` built
  `FastMCP(...)` with no `host`, so it defaulted to `127.0.0.1`, and mcp 1.28.1
  auto-enables DNS-rebinding protection on a loopback host with
  `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`. `/health` and
  `/v1/...` kept answering because they are ordinary FastAPI routes, so nothing
  pointed at the cause. The suite could not see it either — `workspacefixture.py`
  serves on `127.0.0.1`, so every existing MCP test sends a loopback `Host`.
  Fixed by passing `transport_security` explicitly (allowlist derived from
  `WORKSPACE_BASE_URL`, protection still on; explicitly off only when there is no
  base URL to name). New `tests/test_workspace_mcp_host.py` sets `Host`
  explicitly; verified it fails on the unpatched app and passes on the patched
  one. **`portfolio_overview` over MCP stays unverified in production until this
  is on `main` and the workspace has redeployed.**
- 2026-09-12 15:56Z — **§4 flags set, backfill started; §5 and §6 flags set.**
  Bot now carries `PRICE_PLANE_ENABLED`, `PRICE_PLANE_SOURCE=sharadar`,
  `PHASE6_EXECUTION_ENABLED`, `EXECUTION_MODE=paper`, a fresh
  `EXECUTION_APPROVAL_SECRET` (also on the workspace), `SEC_USER_AGENT`, all four
  `PLANE_*` flags, `STRATEGY_LAB_ENABLED` and `STRATEGY_LAB_SHADOW_ENABLED`.
  `STRATEGY_LAB_UNIVERSE_ENABLED` deliberately still unset; `EXECUTION_MODE` is
  not `live`; `ALLOW_LIVE_TRADING` untouched; `STRATEGY_LAB_LIVE_*` untouched.
  `scripts.price_backfill --source sharadar --bulk years=10` runs detached in the
  bot container. Both zips downloaded in under a minute (`stocks.zip` 351 MB,
  `actions.zip` 5 MB); ingest is the long part. **Any variable change redeploys
  the service and kills it** — set variables before starting long work.
- 2026-09-12 — **§6 fixtures NOT recorded, deliberately.** `record_macro_fixtures`
  requires `--series`; `SP500` is not in ALFRED and `VIXCLS`/`DGS10`/`DGS3MO`/
  `DGS2` each blow ALFRED's 2000-vintage ceiling, so only `CPIAUCSL` and `USREC`
  can be recorded at all — and committing those would break
  `test_usrec_only_via_vintage`, which asserts no recession at the 2024-06-30
  vintage while real USREC carries 579 `1.0` rows there. Re-writing those
  assertions against real vintages is its own pull request. Also note
  `config/settings.py` calls `load_dotenv(override=True)`, so the gitignored
  `.env` overrides exported variables for every laptop-side script.
- 2026-09-12 16:25Z — **§4 partly done: real prices in production, cohorts still blocked.**
  20,411 bars / 133 corporate actions / 10 securities for the ten names in
  `historical_events` (`AAPL, BBIO, CPRT, DAL, GIS, HIMS, HNGE, OSCR, PANW,
  VRTX`), back to the tier floor of 2016-09-12, reconstruction identity check
  passing. Three blockers found, none of them operator error:
  (a) **`--bulk years=10` is not runnable in this container.** `load_bulk_bars`
  materialises the whole-market zip twice over; instrumented, it hit 3.4 GB RSS
  at t=15 s and the container was SIGKILLed (exit 137) three times, bouncing the
  bot. Not the cgroup's 8 GB `memory.max` — `oom_kill` stays 0 and the platform
  kills earlier, and `memory.peak` read afterwards is meaningless because it
  resets on restart. `--tickers` cannot help: `backfill_bulk` filters after the
  parse. Needs a streaming parse or a bigger instance. The slice path was used
  instead and is memory-safe.
  (b) **No benchmark, so `cohort_smoke` cannot run.** Sharadar splits equities
  (`stocks`/SEP) from funds (`funds`/SFP); SPY's only `tickers` row is
  `table=funds` and `data/prices/sharadar.py` hardcodes `table=stocks`, so the
  adapter is equities-only and SPY is unreachable.
  `COMPARABLE_BENCHMARK_SECURITY_UID` is unset and the smoke stops at
  `CohortContext.benchmark_security_uid is required`. Adding SFP support is a
  feature with its own adjustment semantics; a stand-in benchmark was refused on
  purpose.
  (c) **`audit_delisting_returns` aborts on `RSH`.** Two verified causes: the
  audit list uses pre-bankruptcy symbols where Sharadar keys the `Q` symbol
  (`RSH`→`RSHCQ`, plus `SHLDQ`/`BBBYQ`/`SIVBQ`/`FTRCQ`/`RADCQ`/`BIGGQ`; `JCPNQ`
  does not resolve, so it needs judgement, not a suffix rule — only 4 of 20
  resolve as written), and the 10-year window has no prices for the 2015 cases
  even under the right symbol. That list is the survivorship-bias check, so a
  symbol resolving to nothing is precisely the failure it exists to catch.
- 2026-09-12 — **§5 flags set, no trade submitted.** `PHASE6_EXECUTION_ENABLED`
  on both services with a shared fresh `EXECUTION_APPROVAL_SECRET`, and
  `EXECUTION_MODE=paper` on the bot. `EXECUTION_MODE` is not `live`,
  `ALLOW_LIVE_TRADING` was left as found, and `STRATEGY_LAB_LIVE_*` was not
  touched. The proposal is the owner's to make — an agent must not both propose
  and approve — and it needs the MCP fix deployed first.
- 2026-09-12 18:05Z — **#77 merged by the orchestrator; the MCP fix is live.**
  The workspace redeployed from `main` and an unauthenticated `initialize` on
  `/mcp` now returns `401`, not `421`; `/health` still reports `postgresql` at
  `0011`. Remaining before the first paper trade: `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` on the **workspace** service (OWNER_SETUP §5, one
  command), then `propose_order` from an attached client. Open engineering
  follow-ups from #77, none blocking paper: a streaming parse for
  `--bulk years=10` (the whole-market zip OOMs the bot container); SFP/funds
  support in `data/prices/sharadar.py` so SPY can be the benchmark and
  `cohort_smoke` can run; the delisting audit list keyed by Sharadar's `Q`
  symbols; macro fixture tests rewritten against real vintages.
- 2026-09-13 02:20Z — Notifications sprint spawned (three workers above) after
  Bryan's decisions: no Telegram, email via Resend, approval and owner
  mutations over MCP without codes, HTML cards. Headless runtime PR follows
  the first two merges. After the overview doc merges the orchestrator
  publishes it to Google Drive from its own connector (workers have none).
  Owner-side, still open before the first paper trade: nothing until these
  merge; then `NOTIFY_EMAIL_ENABLED=true` and `WORKSPACE_OWNER_TOOLS_ENABLED=true`
  on the services, an `admin`-scoped token re-issued, and the client attached.
- 2026-09-13 — **`notify/` built on `claude/notify-email-cards` (not yet merged).**
  A delivery layer both processes can import: a Resend email channel behind
  `NOTIFY_EMAIL_ENABLED` (default off), the existing Telegram senders wrapped as
  a channel, one HTML card renderer with five card kinds, and a signed read-only
  page at `/cards/{uid}` on the workspace (HMAC over the uid under
  `CARD_LINK_SECRET`, falling back to `EXECUTION_APPROVAL_SECRET`; non-expiring;
  one 404 for every failure so it is not an enumeration oracle). Alembic
  `0012_notify_email_cards` adds `cards` and `notifications_sent` off `0011`.
  Telegram delivery is **unchanged and still the only approvable channel** — an
  email has no callback and the card page has no route that writes; removing
  Telegram belongs to the headless-runtime change. `docs/NOTIFICATIONS.md` is
  the reference and `docs/examples/cards/` has a rendered example of each card.
  Owner action once merged: `NOTIFY_EMAIL_ENABLED=true` on both services, and
  `WORKSPACE_BASE_URL` on the **bot** service so links in email from the bot
  work (`docs/OWNER_SETUP.md` §5). Not verified: any real Resend send, and the
  page against a real Postgres — both need production credentials.
- 2026-09-13 04:05Z — #80 merged and published to Google Drive; #81 merged
  (CI green on its head; 156 targeted tests re-run on the merged tree). #82
  integrated by the orchestrator: union-resolved `config/settings.py`,
  `database/models.py`, `migrations/README.md` (both branches appended blocks
  at the same spot), HANDOFF taken from `main`, and `0013_merge_notify_owner`
  added over the two `0012_*` heads. Full 3.12 suite run on the merged tree
  before pushing.
- 2026-09-13 04:30Z — #82 merged (full 3.12 suite on the integrated tree:
  1,951 tests, 3 skips; CI green). Headless-runtime worker spawned from
  `main` at `d705572`; brief verbatim in `briefs/headless-runtime.md`.
- 2026-09-13 05:40Z — #85 (headless runtime) merged; full 3.12 suite on the
  merged tree 1,992 tests, 3 skips; CI green. Sprint complete. Workers
  archived, Routines deleted. Owner turn-on sequence in §7; next sprint
  briefs in §2b, not spawned.
- 2026-09-13 05:45Z — Research-engine sprint spawned on the owner's go: funds
  benchmark (opus), streaming bulk backfill (sonnet), delisting-audit symbols
  (sonnet), all from `main` at `6bdd379`. Merge order when they land: funds
  first (it adds `0014`; the other two add no migration), then the rest. The
  owner's terminal agent is running the notifications turn-on sequence (§7)
  concurrently.
- 2026-09-13 07:40Z — Research-engine sprint merged: #89 funds/SFP (adds
  `0014`), #88 delisting-audit symbology, #90 streaming bulk backfill. #88 was
  integrated on top of #89 (union of the adapter helpers and the Spec N §12
  rulings); #90 on top of both (adapter tail = funds `_resolve` + bulk stream
  class; backfill CLI = both argument sets, checkpoint/abort before the fund uid
  printout; OWNER_SETUP §4 = bulk fix + funds FIXED, stale blocker dropped).
  Full 3.12 suites on each integrated tree green. Workers archived, Routines
  deleted.
