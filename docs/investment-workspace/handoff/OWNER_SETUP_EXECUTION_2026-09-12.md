# OWNER_SETUP execution report — 2026-09-12

A local Claude Code session on Bryan's laptop worked `docs/OWNER_SETUP.md` §2–§6
against Railway project `swingtrader` / environment `production`. This is the
record of what ran, what it printed, what was worked around, and — the part that
matters most for whoever picks this up — **what is not verified**.

Branch: `claude/owner-setup-executed`. Nothing here was merged.

No secret appears in this file, in the repo, or in any commit. The workspace
token was written only to `~/.swingtrader-workspace-token` (mode 600) on the
laptop.

---

## Status at a glance

| Section | State |
| --- | --- |
| §2 Postgres cutover | **Done.** Both services on Postgres at `0011_comparable_subject_ticker`. |
| §3 Workspace + token | **Done**, except `portfolio_overview` over MCP — see the bug below. |
| §4 Prices and cohorts | **Partly done.** Real prices in; benchmark and cohort smoke blocked. |
| §5 Paper-trade flags | **Flags set. No trade submitted** (owner's to make, and needs the MCP fix live). |
| §6 Evidence planes / Lab shadow | **Flags set. Fixtures deliberately not recorded.** |

Production is healthy: bot logs `schema_ready action=upgraded backend=postgresql`,
workspace `/health` is 200 with `"backend": "postgresql"`.

---

## §2 — Postgres cutover (done)

- `schema_status` on the live file: `state: versioned`, `0011_comparable_subject_ticker`.
- Consistent snapshot taken **in the container** with `sqlite3.Connection.backup`
  (a raw copy of a live WAL database is not consistent): integrity `ok`, 62
  tables, 1765 rows, sha256 `23455bf6…bf51`.
- **Rehearsal** and **cutover** both ran inside the bot container over the
  private network: `PASS`, 61 tables, 1764 source rows = 1764 target rows, every
  row `ok`, 60 identity sequences resynced. Reports committed as
  `docs/audits/2026-09-12T153015Z-sqlite-to-postgres.md` and
  `docs/audits/2026-09-12T154146Z-sqlite-to-postgres.md`.
- Rollback archive pulled to the laptop via `gzip -9 -c | base64 -w0`, decoded
  with `base64 -D`, sha256 **identical** to the container's copy;
  `pragma integrity_check` → `ok`. Stored at
  `~/swingtrader-archive/swing_trader.prod.rehearsal.2026-09-12.db`. The SQLite
  file also stays on the `/data` volume as the archive.
- Variables set with `--set-from-stdin` so the URL never entered argv or shell
  history: bot `DATABASE_URL` (private psycopg) + `DATA_DIR=/data`; workspace the
  same `DATABASE_URL`.

**The bot was not paused, by the owner's explicit decision.** `railway scale` and
`railway service scale` both panic on CLI 4.29.0
(`Cannot query field "railwayMetal" on type "Region"`), so there is no CLI path
to replicas 0. Instead the window was *proved* clean: the live file was hashed
before the migration and re-hashed after it and was byte-identical both times
(`23455bf6…bf51`), so nothing was written during the run and the copy migrated
was the final state. Two snapshots 75 s apart were already byte-identical
beforehand, with `SCHEDULER_ENABLED=false`, `holdings` empty and `broker_orders`
empty. Setting `numReplicas = 0` in `railway.toml` was explicitly rejected: that
file is committed and applies to every service built from the repo, so it would
also have stopped `workspace`.

**Not verified:** the Telegram smoke (`/status`, one `/eval`) — a session cannot
drive Telegram. Bryan should still send both.

---

## §3 — Workspace on Postgres, token (done, with one bug found)

- Flags set: `WORKSPACE_API_ENABLED`, `RESEARCH_WORKSPACE_ENABLED` on the
  workspace; `PORTFOLIO_SYNC_ENABLED`, `RESEARCH_WORKSPACE_ENABLED` on the bot.
- Token `claude-code`, scopes `read,research:write,propose`, issued inside the
  container against Postgres.
- `GET /v1/whoami` → `{"token_label":"claude-code","scopes":["read","research:write","propose"],"execute_scope_exists":false}`.
  No token → `401`.
- `/health` → `"backend":"postgresql"`, `"migration_revision":"0011_comparable_subject_ticker"`,
  `"workspace_api_enabled":true`, `"research_workspace_enabled":true`.

Note the REST `/v1` namespace serves **only** `whoami` (`workspace/app.py`).
`/v1/portfolio_overview` is a `404` by design; every other tool is MCP.

### Bug found and fixed: the MCP surface was 100% unreachable

Every request to `/mcp` — GET or POST, with a valid token — returned
`421 Invalid Host header`.

Not Railway. `workspace/app.py` built `FastMCP(...)` with no `host`, so it
defaulted to `127.0.0.1`, and mcp 1.28.1
(`mcp/server/fastmcp/server.py`, the `transport_security is None and host in
("127.0.0.1", "localhost", "::1")` branch) **auto-enables DNS-rebinding
protection** with `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`. The
process never binds loopback — uvicorn serves it behind Railway's edge — but the
guard fired anyway. `/health` and `/v1/...` kept working because they are
ordinary FastAPI routes, so nothing pointed at the cause.

The suite could not see it either: `tests/workspacefixture.py` serves on
`127.0.0.1`, so every existing MCP test sends a loopback `Host`.

Fix: `workspace/app.py` now passes `transport_security` explicitly via
`_transport_security()`. With `WORKSPACE_BASE_URL` set, protection stays **on**
and the allowlist is that host plus loopback; with no base URL there is no host
to name, so it is switched off explicitly rather than inherited by accident
(refusing everything would be the same outage in a new costume).

`tests/test_workspace_mcp_host.py` (4 tests) sets `Host` explicitly. Verified it
**fails on the unpatched app** (`AssertionError: 421 == 421 : the public host is
refused; the MCP surface is unreachable`) and passes on the patched one. No
existing test was weakened.

**Not verified, and this is the biggest open item:** `portfolio_overview` and
every other MCP tool, in production. The workspace service builds from `main`,
and the fix is on this branch. Bryan chose to wait for the merge rather than
`railway up` an un-merged build. **When this PR merges, Railway redeploys the
workspace and the MCP surface should come up — verify it then**, with
`whoami` and `portfolio_overview` over `/mcp`.

---

## §4 — Prices and cohorts (partly done)

Set: `PRICE_PLANE_ENABLED=true`, `PRICE_PLANE_SOURCE=sharadar`.

**Real vendor prices are in production**: `20411 bars, 133 actions, 10 securities
from sharadar into snapshot 'dev'`, exit 0, with the Spec N §4.3 reconstruction
identity check passing on every name.

```
AAPL 2514  2016-09-12..2026-09-11      OSCR 1389  2021-03-03..2026-09-11
BBIO 1812  2019-06-27..2026-09-11      PANW 2514  2016-09-12..2026-09-11
CPRT 2514  2016-09-12..2026-09-11      VRTX 2514  2016-09-12..2026-09-11
DAL  2514  2016-09-12..2026-09-11      GIS  2514  2016-09-12..2026-09-11
HIMS 1798  2019-07-18..2026-09-11      HNGE  328  2025-05-22..2026-09-11
```

Those ten are exactly the tickers in `historical_events` / `event_outcomes`.

### Three blockers, none of them operator error

**1. `--bulk years=10` cannot run in this container.** `load_bulk_bars` →
`_read_bulk_csv` materialises every row of the whole-market zip and then builds a
`DailyBar` per row — two full in-memory copies. Instrumented with a wrapper
sampling child RSS and cgroup counters, the child was at **3.4 GB RSS 15 seconds
in** (`cg_current` 4.27 GB) and the container was SIGKILLed (exit 137). It
happened three times and bounced the bot each time; it recovered cleanly every
time via `restartPolicyType = ON_FAILURE`.

It is **not** the cgroup's own 8 GB `memory.max`: `memory.events` never records
an `oom_kill`, so the platform kills earlier. Beware that `memory.peak` read
*after* the fact is worthless — it resets when the container restarts, which is
why the first two investigations wrongly concluded "only 266 MB, not memory".
`--tickers` does not help: `backfill_bulk` filters *after* the parse. This needs
a streaming parse or a much larger instance. The memory-safe per-ticker slice
path was used instead.

**2. No benchmark, so `cohort_smoke` cannot run.** Sharadar splits equities
(`stocks`/SEP) from funds (`funds`/SFP). SPY's only row in `tickers` is
`table=funds` (permaticker 118691, "SPDR S&P 500 ETF TRUST");
`stocks?ticker=SPY` is empty while `funds?ticker=SPY` returns real bars.
`data/prices/sharadar.py` hardcodes `table=stocks` in `security_master` and reads
`TABLE_STOCKS` in `daily_bars`, so the adapter is equities-only by construction
and `--tickers SPY` fails with `tickers has no row for 'SPY'; refusing to invent
a security id`.

So `COMPARABLE_BENCHMARK_SECURITY_UID` is **not set**, and `cohort_smoke` stops
at `CohortContext.benchmark_security_uid is required` (Spec N §4.2, §5.2).

Adding `funds`/SFP support is a feature with its own price-adjustment semantics
and tests, so it was not attempted here. Substituting some equity as a stand-in
benchmark was also refused: a cohort answer computed against a benchmark that is
not the benchmark is a wrong number.

**3. `audit_delisting_returns` aborts on its first case, `RSH`.** Two
independent causes, both verified against the vendor API:

- *Symbol.* `data/prices/delisting_audit_list.py` names companies by their
  pre-bankruptcy symbol; Sharadar keys them by the post-bankruptcy `Q` symbol.
  `RSH` → `RSHCQ` ("RADIOSHACK CORP", permaticker 199304) exists, as do `SHLDQ`,
  `BBBYQ`, `SIVBQ`, `FTRCQ`, `RADCQ`, `BIGGQ`. `JCP` → `JCPNQ` does **not**
  resolve, so this is not a mechanical suffix rule and needs per-case judgement —
  `tickers.relatedtickers` is the likely path. Only 4 of the 20 cases
  (`ACI, SUNE, WLL, CBL`) resolve as written.
- *Window.* Even under the right symbol, `stocks?ticker=RSHCQ` returns no rows:
  the 10-year tier starts 2016-09-12, so the 2015 cases (`RSH`, `WLT`, `ZQK`)
  need the `full` tier. The 2023–2024 cases do have prices — `SIVBQ` and `BBBYQ`
  both return real bars.

That list is the survivorship-bias check, so a symbol that silently resolves to
nothing is exactly the failure mode it exists to catch. **Worth treating as a
data-correctness bug, not a script annoyance.**

---

## §5 — Paper trade (flags set, no trade submitted)

`PHASE6_EXECUTION_ENABLED=true` on **both** services with a shared fresh
64-character hex `EXECUTION_APPROVAL_SECRET`, and `EXECUTION_MODE=paper` on the
bot.

`EXECUTION_MODE` is **not** `live`. `ALLOW_LIVE_TRADING` was left exactly as
found. `STRATEGY_LAB_LIVE_ENABLED` and `STRATEGY_LAB_LIVE_RISK_BUDGET` were not
touched. No Robinhood stop probe was run.

The proposal is the owner's to make — an agent must not both propose and approve
— and it needs the MCP fix deployed first. Once the workspace is redeployed from
`main`, from an attached client:

```
propose_order(ticker="AAPL", entry=<price>, stop=<price>, risk_fraction=0.005)
```

No quantity: the execution service sizes it. A card arrives in Telegram; approve
it there. `docs/EXECUTION_LIFECYCLE.md` lists every refusal.

---

## §6 — Evidence planes and Strategy Lab shadow (flags set, fixtures not recorded)

Set on the bot: `SEC_USER_AGENT`, `PLANE_SEC_MINIMAL_ENABLED`,
`PLANE_FILINGS_ENABLED`, `PLANE_MACRO_VINTAGE_ENABLED`, `PLANE_NEWS_ENABLED`,
`STRATEGY_LAB_ENABLED`, `STRATEGY_LAB_SHADOW_ENABLED`.
`STRATEGY_LAB_UNIVERSE_ENABLED` deliberately left unset.

**The macro fixtures were deliberately not recorded or replaced.** Three
separate problems:

1. `scripts.record_macro_fixtures` **requires `--series`**; the bare invocation
   in OWNER_SETUP exits on a missing argument.
2. Most of the series the fixture README names **cannot be recorded at all**.
   `SP500` is not in ALFRED (`400 the series does not exist in ALFRED`), and
   `VIXCLS`, `DGS10`, `DGS3MO` and `DGS2` each exceed ALFRED's 2000-vintage-date
   ceiling (3931–5108 vintages) because `get_series_all_releases` requests the
   full real-time period. Only `CPIAUCSL` and `USREC` record successfully.
3. Committing those two **would break
   `tests/test_macro_plane.py::test_usrec_only_via_vintage`**, which asserts
   `set(series_as_of(USREC, 2024-06-30).values()) == {0.0}`. Real USREC carries
   **579 rows valued `1.0`** at that vintage — every NBER recession back to 1854.
   The synthetic fixture's invented 2024 recession is the thing under test.
   Verified by recording to a scratch directory and checking the data, not by
   reasoning about it.

Re-writing those assertions against real vintages is a change to the tests and
belongs in its own pull request. One datum worth keeping: the synthetic set
claims January 2024 CPI first printed on 2024-02-13, and ALFRED agrees.

The filings fixtures were not recorded either, since the same PR should decide
the macro question first and the filings README explicitly says real recordings
should *extend* rather than replace the synthetic edge cases.

---

## Traps worth knowing (all cost real time here)

1. **`#` is not a comment in an interactive zsh.** Pasted comment lines execute;
   ones containing parentheses die with `zsh: parse error near ')'`.
   `docs/OWNER_SETUP.md` §2 now keeps prose out of the fenced blocks.
2. **`railway ssh -- <cmd>` re-parses your command through `sh -c`** in the
   container, so locally-typed quotes are consumed locally.
   `python -c "…(…)…"` fails with `sh: 1: Syntax error: "(" unexpected`. Prefer
   `python -m` with metacharacter-free args, or base64 a script across.
3. **The ssh channel is a PTY**, so binary output is corrupted — `railway ssh --
   cat db > file` produces a broken file. Use `gzip | base64 -w0`, strip CR/LF,
   decode with `base64 -D` on macOS.
4. **Backgrounding inside `railway ssh` silently loses the job.** Both
   `nohup … &` and `setsid … &` were killed when the session closed — proved with
   two `sleep 90` markers, both dead afterwards. A silent 240 s foreground
   session, by contrast, survived (`EXIT=0`), so it is not an idle timeout. Hold
   the session open in the foreground for long work.
5. **Any variable change redeploys the service** and kills whatever is running in
   its container. Set every variable you need before starting a long job.
6. **`config/settings.py` calls `load_dotenv(override=True)`**, so the repo's
   gitignored `.env` **overrides exported environment variables** for every
   laptop-side script. It has `FRED_API_KEY=` empty and a SQLite `DATABASE_URL`,
   which is why a correctly-exported key still fails with `FRED_API_KEY is unset`.
7. **This Postgres service publishes no `DATABASE_PUBLIC_URL`** — only the
   private `DATABASE_URL`. A public URL needs a TCP proxy enabled in the
   dashboard. Running in-container avoids needing one.

---

## Verification run before pushing

`python -m compileall -q .` → clean.
`python -m unittest discover -s tests -p "test_*.py"` on Python 3.12 →
see the pull request body for the final numbers; the run that gated the push
included `tests/test_workspace_mcp_host.py` and the patched `workspace/app.py`.

---

## What remains, and who can do it

**Needs the merge first**
- Merge this PR so the workspace redeploys from `main`, then verify `whoami` and
  `portfolio_overview` over `/mcp`. Until then the MCP tool surface is down.

**Needs a decision or a follow-up PR**
- `funds`/SFP support in the Sharadar adapter, or another benchmark, so
  `COMPARABLE_BENCHMARK_SECURITY_UID` can be set and `cohort_smoke` can run.
- The delisting audit symbol remap (and `years=full` for the pre-2016 cases).
- A streaming bulk parse, or a larger instance, for `--bulk years=10`.
- Whether to re-write the macro vintage assertions against real ALFRED data.

**Owner-only, unchanged**
- Fund the Robinhood Agentic account and re-run `scripts/robinhood_auth.py`.
- The live `gtc stop_market` probe (`docs/EXECUTION_LIFECYCLE.md` §6).
- Confirm the Rule 10b5-1 element and name the tracked investors (Spec O).
- Decide `ALLOW_LIVE_TRADING` deliberately.
- **Rotate `SHARADAR_API_KEY`** — the value went through a chat.
- Send `/status` and one `/eval` in Telegram as the §2 smoke test.
