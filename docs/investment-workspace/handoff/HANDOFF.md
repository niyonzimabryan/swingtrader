# Investment Workspace build — handoff

**Purpose.** Everything a person (or a fresh agent session in Cursor, Codex, or
Claude Code) needs to finish the build if the orchestrating session stops. It is
updated in every integration commit; the "Last updated" line says how fresh it
is. If it is more than a few hours old, trust `git log origin/main` and the
open-PR list over this file.

**Last updated:** 2026-09-13 02:20 UTC, by the orchestrating session
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
| Alembic head | `0011_comparable_subject_ticker` (single) |
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
ruling and must be recorded in L §10 and a new K rulings log); owner mutations
(kill switch, promotions) also go over MCP; cards are designed HTML emails
with a signed full-page view served by the workspace.

| PR / branch | What | Session | Model | State |
|---|---|---|---|---|
| `claude/notify-email-cards` | `notify/` package, Resend channel behind `NOTIFY_EMAIL_ENABLED`, HTML card renderer + PNG chart, signed `/cards/<uid>` page, `notifications_sent` table | `session_01NSbbAvFecNAkxaZSQ6v8P9` | opus | building |
| `claude/owner-tools-mcp` | `OWNER_ID`; `admin`-scoped `approve_order`/`reject_order`/`approve_memo`/`kill_switch`/`promote_arm`/…; runtime approval poller calling `on_approval`; spec rulings | `session_01A4B5sYhhr8ZDnRFHkVq4xY` | opus | building |
| `claude/system-overview-doc` | `docs/SYSTEM_OVERVIEW.md`, standalone, sourced; published to Google Drive by the orchestrator after merge | `session_01D8jUpshohWT44o1L6gLPHd` | sonnet | building |

Briefs are verbatim in `briefs/notify-email-cards.md`, `briefs/owner-tools-mcp.md`,
`briefs/system-overview-doc.md`. Not yet spawned, **after the first two merge**
(they both rewire `main.py`): the headless runtime — `TELEGRAM_ENABLED=false`
runs scheduler, monitors and the approval poller with no Telegram token, and
`NotificationManager` routes through `notify/`.

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

## 7. Owner actions to reach a testing state (unchanged from ENV_SETUP)

`docs/ENV_SETUP.md` "Order of operations" is the runbook. Open owner items:
commit `docs/robinhood/tool_schemas.json`; record real SEC / Robinhood / macro
fixtures from the laptop; confirm the Rule 10b5-1 element; name tracked
investors; buy Sharadar Prices, run the delisting audit and backfill; Postgres
cutover per `docs/POSTGRES_CUTOVER_RUNBOOK.md`; workspace service + token;
`COMPARABLE_BENCHMARK_SECURITY_UID`; `scripts/cohort_smoke.py` against prod;
the live `gtc stop_market` probe (`docs/EXECUTION_LIFECYCLE.md` §6);
`schema_status` against the prod DB.

## 8. Change log of this file

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
