# Spec K — Workspace core and tool surface

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05
**Flag:** `WORKSPACE_API_ENABLED=false`

---

## 1. Problem

SwingTrader's state lives in `swing_trader.db`, a SQLite file on a Railway volume at
`/data/swing_trader.db`. That is unreachable from a Claude cloud session, from Codex on
another machine, and from a phone. Research done in one session evaporates when the
session ends. There is no way to ask the system a question except through Telegram
commands written for one specific workflow.

The goal is one authenticated endpoint that every client can attach to, returning the
same answer everywhere, with no client-specific server.

## 2. Outcome

- A single Railway web service, `swingtrader-workspace`, exposing the same capabilities
  three ways: **REST** (for scripts and the bot), **MCP over streamable HTTP** (for
  Claude Code, cloud sessions, and Codex), and **a `swing` CLI** (a thin client over
  REST, for terminals and cron).
- Postgres as the structured store; the repo as the narrative store.
- A session that opens the repo anywhere gets a working workspace with no local setup
  beyond one token.

## 3. Foundation work (blocking, do first)

### 3.1 Alembic

Already specified in Spec Q §14 Phase 0 and not restated here. It is a hard
prerequisite: **do not add a workspace table before the Alembic baseline exists.**

### 3.2 SQLite → Postgres

1. Provision Postgres in Railway project `e556a6d9-2023-4c81-a031-e32e160a33be`.
2. `DATABASE_URL` already exists in `.env.example`; the code path must accept a
   `postgresql+psycopg://` URL with no other change. Audit for SQLite-isms first:
   `database/db.py` WAL/`busy_timeout` pragmas (shipped in Audit D), any
   `sqlite3`-specific `ON CONFLICT` or `INSERT OR IGNORE`, `datetime` storage
   assumptions, and `Boolean`/`JSON` column behaviour.
3. One-shot migration script `scripts/migrate_sqlite_to_postgres.py`:
   - dumps every table in dependency order,
   - verifies row counts and a per-table content hash on both sides,
   - is idempotent and refuses to run against a non-empty target without `--force`,
   - writes a report to `docs/audits/`.
4. Keep the SQLite file as a read-only archive. Do not delete it in this PR.
5. **Verification gate:** the full existing test suite passes against Postgres in CI,
   with the SQLite run retained as a second matrix entry until the migration is
   confirmed in production for one week.

Rationale for Postgres specifically: concurrent readers (many agent sessions plus the
bot), network reachability, and real `JSONB` querying for the snapshot/evidence tables
Specs N and Q add. This is not preference — SQLite on a single mounted volume
structurally cannot serve a cloud session.

### 3.3 Narrative mirror in git

Structured rows are not readable by a session that cannot reach the API. Every dossier
and thesis (Spec M) is **also** written as Markdown under `research/` in the repo, with
YAML front-matter carrying the identifiers that link it back to Postgres. A commit hook
or a scheduled job keeps them consistent (`scripts/sync_research_mirror.py`, direction:
Postgres → repo, never the reverse without an explicit `--import`).

**Licensing rule for the mirror.** The repo is public. Vendor price and fundamentals
data (Tiingo, Polygon, Sharadar, FMP) may not be redistributed, and several licences
extend to derived works; Yahoo data via `yfinance` sits outside Yahoo's terms entirely.
So `research/` holds narrative, sourced claims, and *summary* figures that cite a
cohort id — never a raw series, never a vendor table. `docs/DATA_LICENSES.md` maps each
source to what may be committed versus what lives only in Postgres, and a CI check
(`test_no_vendor_series_in_mirror`) fails on a numeric column longer than a handful of
rows under `research/`.

Consequence: **git clone is the offline fallback.** If the workspace API is down, an
agent session still has every thesis, every dossier, and every past decision — just not
live prices. That degradation is designed, documented, and tested (§8).

## 4. The service

```text
swingtrader-workspace  (FastAPI, one Railway service)
├── /health                         liveness + dependency status
├── /mcp                            streamable-HTTP MCP endpoint  (agents)
├── /v1/...                         REST                          (CLI, bot, scripts)
└── /admin/...                      owner-only, token-gated
```

The existing bot process stays a separate service. It calls the workspace over REST
rather than importing it, so a workspace deploy never restarts the trading monitor.

**Transport decision (research slot 8):** the official `mcp` Python SDK, mounted into
the FastAPI app, streamable HTTP (SSE is formally deprecated as of the 2026-07 spec
revision). The surface here is fifteen tools with no sampling, elicitation, or proxying,
which is exactly the case where one dependency beats a framework. Both the official SDK
(1.x → 2.x in 2026-08, `FastMCP` renamed `MCPServer`) and `fastmcp` (2 → 3 → 4 within
seven months) are churning; **pin `mcp>=1.29,<2` now** and migrate to v2 in a dedicated
PR. `fastmcp` becomes the answer only if claude.ai connector OAuth is required (§4.1).
Railway's reference MCP deployment adds Redis for stream resumability so a long
`compare_setups` call survives the proxy closing an idle connection — adopt that if and
when a call exceeds the proxy timeout, not before.

### 4.1 Authentication

- One long-lived **owner token** per client, issued by `swing auth issue --label
  "codex-laptop"`, stored hashed, revocable, and scoped.
- Scopes: `read`, `research:write`, `propose`, `admin`. **There is no `execute`
  scope** — order execution is not reachable by token at all (§6 of Spec L).
- Every request is logged with token label, tool name, and argument hash. `/admin`
  requires a separate token that is never placed in an agent's environment.
- Rate limits per token, concrete: 60 read calls and 10 write calls per minute per
  token by default. A runaway agent loop must cost time, not money.
- **Static bearer tokens reach Claude Code and Codex today** (`--header "Authorization:
  Bearer …"` and `bearer_token_env_var` respectively). claude.ai custom connectors are
  OAuth-first, with static-header support only in beta as of this writing. The plan is
  bearer for CLI clients now; if a claude.ai connector is wanted later, add an OAuth
  provider (the one argument for `fastmcp`) without changing the tool surface.

### 4.2 MCP tool surface

Named to be unambiguous in a transcript. Read tools are free of side effects and safe
to call speculatively; write tools are explicitly marked.

| Tool | Kind | Returns |
|---|---|---|
| `portfolio_overview` | read | positions, cash, exposure, sync freshness (Spec L) |
| `position_detail` | read | one position: lots, basis, unrealized, linked thesis |
| `orders_open` | read | pending and recently filled orders across brokers |
| `research_get` | read | dossier + current thesis + invalidators for a ticker |
| `research_search` | read | full-text over dossiers, theses, and decisions |
| `research_write` | **write** | upserts a dossier section or thesis, with provenance |
| `thesis_review` | **write** | records a review verdict: hold / weakened / invalidated |
| `compare_setups` | read | **the Spec N cohort answer** |
| `cohort_detail` | read | the constituent events behind a cohort answer |
| `filings_recent` | read | 13F/13D/G/Form 4 for a ticker or tracked investor (Spec O) |
| `macro_state` | read | current regime + the vintage-correct series (Spec O) |
| `news_timeline` | read | timestamped, deduped news for a ticker |
| `experiments_status` | read | Strategy Lab arms and scoreboard (Spec Q) |
| `propose_order` | **write** | creates a `proposed` order row. Places nothing. |
| `journal_append` | **write** | appends a dated note to the decision journal |

Every read tool returns a `provenance` block: `as_of_utc`, per-field source, staleness
flags, and a `data_quality` tier. A tool that cannot meet its freshness contract returns
the stale value **with the flag set**, never a silent guess and never an exception that
the agent will paper over.

### 4.3 Client attachment

The repo ships the configuration for all three clients so attaching is copy-paste:

- `.mcp.json` — Claude Code project-scoped MCP server entry pointing at
  `https://<service>.up.railway.app/mcp` with `${WORKSPACE_TOKEN}`.
- `AGENTS.md` — the shared entry point, and now a Linux Foundation standard read
  natively by Codex, Cursor, Copilot and others. It holds the workflow rules and the
  non-negotiables. **`CLAUDE.md` line 1 is `@AGENTS.md`**, the documented Claude Code
  import; Claude-specific material sits below the import. Parity is structural, not
  tested — the earlier plan to hand-maintain two files and test their agreement was a
  maintenance tax nobody else pays. A much smaller test remains (§8).
- `docs/WORKSPACE_ACCESS.md` — how to attach from a cloud session and from the phone.

`CLAUDE.md` at the repo root gains a short "how to work in this repo" section: read the
dossier before answering, write findings back through `research_write`, never claim a
number the tools did not return.

## 5. Cost discipline

The workspace's job is to make the expensive thing rare.

- **Every scheduled job is pure Python.** Sync, pricing, forward-return maturation,
  cohort statistics, reconciliation, evaluation, and report rendering contain zero
  model calls. If a job needs an LLM, it is not a job — it is a proposal for a human
  turn.
- Model calls that do happen are ledgered through the existing `llm_call` structlog
  ledger and Langfuse stage tags shipped in Spec G, with the workspace tool name as a
  tag, so "what did this question cost" is answerable.
- `compare_setups`, the most-called expensive-looking tool, is **entirely
  deterministic** — it is SQL and NumPy. It costs nothing per call and is cached by
  `(setup_hash, as_of_date)`.

## 6. Deployment

- New Railway service in the existing project; `railway.toml` gains a second service
  definition. Auto-deploy on push to `main` as today.
- Health check `/health` reports: Postgres reachable, last portfolio sync age, last
  cohort-maturation job age, migration revision.
- Secrets via `railway variables --set`. The workspace token is generated once and
  never committed; `.env.example` gains `WORKSPACE_TOKEN=` and `WORKSPACE_BASE_URL=`
  with comments only.

## 7. Non-goals

- No web UI. The clients are the UI.
- No multi-user. One owner, several tokens.
- No websocket streaming quotes. Polling is sufficient for a swing-trading horizon and
  the failure modes are far simpler.
- No agent orchestration framework. Spec P uses the client's own subagent primitives.

## 8. Test plan

| Test | Asserts |
|---|---|
| `test_postgres_parity` | The suite passes identically against SQLite and Postgres |
| `test_sqlite_migration_roundtrip` | Row counts and content hashes match; re-running is a no-op |
| `test_no_execute_scope` | No route and no MCP tool reaches a broker placement call; enforced by import-graph assertion, not by convention |
| `test_provenance_required` | Every read tool's response model requires a non-empty `provenance` block |
| `test_stale_is_flagged_not_hidden` | With a frozen clock past the freshness budget, tools return data **and** `stale=true` |
| `test_offline_git_fallback` | With the API unreachable, `research/` Markdown alone answers "what is my thesis on X" |
| `test_claude_md_imports_agents_md` | `CLAUDE.md` line 1 is `@AGENTS.md`, and `AGENTS.md` contains the four non-negotiable strings (no order placement, no model statistic, provenance required, deterministic jobs) |
| `test_no_vendor_series_in_mirror` | No file under `research/` contains a numeric series longer than the configured cap |
| `test_rate_limit_per_token` | The 61st read call in a minute on one token is refused with a retry-after |
| `test_token_scopes` | A `read` token is refused on `research_write` and `propose_order` |

## 9. Definition of done

- The same three questions — holdings, thesis, cohort — answer identically from Claude
  Code, a cloud session, and Codex, verified by hand once and recorded in
  `docs/WORKSPACE_ACCESS.md`.
- Production runs on Postgres for a week with the SQLite archive untouched.
- `/health` is green and paged on failure through the existing Telegram alert path.
- Zero model calls in any scheduled job, asserted by test.
