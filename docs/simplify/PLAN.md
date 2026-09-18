# Simplification plan — from trading agent to research tutor

Status: **v3, direction ratified 2026-09-17.** Briefs in `briefs/`; local-agent
prompt in `LOCAL_AGENT_PROMPT.md`. Research repo: `niyonzimabryan/research-bench`.

Progress (2026-09-18):
- Done: Railway bot service stopped (owner, via CLI). swingtrader PR #107
  (freeze) merged. research-bench PR #1 (skeleton) merged; PR #2 (Robinhood
  read-only snapshot, built in Cursor) merged.
- In flight: brief 2 (edgar CLI), worker on sonnet, branch `claude/edgar-cli`.
- Blocked: brief 5 (research export). The local agent's first attempt saw a
  Postgres with no matching schema; most likely it never reached production
  (private `railway.internal` host), unverified. Diagnosis prompt issued;
  no stamp or migrate is to be run. If production holds no dossiers, brief 5
  closes as a no-op.
- Waiting on owner: brief 4 (AlphaSense skill file); one local `rh_auth`
  re-run (the copied token's refresh returned 404); Railway workspace and
  Postgres teardown after the export question settles; GitHub archive of
  swingtrader.
- Not started: a first real research session in research-bench (the
  acceptance test for the whole build).

## 1. Evidence

- ~150k lines of Python, 49k of them tests (143 files, ~1,469 tests). Git
  history starts 2026-09-09 (199 commits); the pre-workspace history is not in
  this clone, so "legacy vs workspace-era" below comes from
  `docs/SYSTEM_OVERVIEW.md` §1, not from commit dates.
- CI ran 273 times in the week of 09-10 to 09-16, 9–15 min each, 7 jobs per PR
  and 12 per push to `main`, plus `attestation-check` and `model-lint` on every
  event. That is the $35.71.
- The "failure" emails are mostly **cancelled** runs on `main` (15 of 43 push
  runs sampled): the `concurrency` block cancels the in-flight run when the
  next PR merges minutes later. Genuine failures in the sample: 6, all on PRs.
- Cost = velocity x suite size, and the velocity was the orchestrated-build
  cadence itself. Trimming CI helps; not carrying 130k lines helps more.

## 2. The two systems in this repo

Per `docs/SYSTEM_OVERVIEW.md`:

- **Legacy bot** (predates the specs; the one that worked in Telegram):
  `orchestrator/pipeline.py`, `agents/`, `scoring/`, `screening/`, `scanning/`,
  `memo/`, `bot/` (Telegram), `backtest/`, `tracking/`, Alpaca paper via
  `execution/brokers/`.
- **Investment workspace** (Specs K–Q, ~30 PRs from 09-05, plus everything
  since): `workspace/` (FastAPI + MCP), `research_workspace/`, `portfolio/`
  ledger and proposals, `comparables/`, `strategy_lab/`, the evidence planes
  (`filings/`, `macro/`, `news/`), `notify/`, the Postgres cutover and
  `migrations/`, the approval poller, `evals/` + `tools/` and their two
  workflows, `specs/`, `.claude/agents/`.

The owner's read: the legacy bot was decent; the workspace was not well
thought through. Two pieces of the workspace era are still worth salvaging
because they serve the research goal directly: the throttled SEC client
(`filings/client.py`, 351 lines, plus `sec_minimal.py` and `form4.py`) and the
Robinhood read paths (`execution/brokers/robinhood.py`, positions/balances/lots
only, with `database/token_store.py` and `scripts/robinhood_auth.py`).

## 3. Target shape

Two repos, no servers.

### 3a. `swingtrader` — public portfolio piece for the legacy bot

Pick one:

- **Freeze (recommended).** Tag `workspace-final` at today's `main`, rewrite
  the README to say honestly what worked (the scan → agents → score → Telegram
  digest pipeline, paper on Alpaca) and what was an over-reach (Specs K–Q,
  unmaintained), stop the Railway services, archive the repo or simply stop
  pushing. Zero build PRs; CI cost goes to zero because nothing moves. The
  code stays readable and forkable.
- **Restore-and-run.** Only if the owner wants the Telegram digests running
  again. Remove the workspace-era packages in place, keep Postgres/Alembic (it
  works), strip `main.py` back to scheduler + digest + Telegram. Known
  blockers: `database/models.py` imports `strategy_lab.domain`; `execution/`,
  `strategy_lab/`, `portfolio/`, `database/` are the most-imported packages in
  `tests/`. Estimate 6–10 PRs with a test-repair tail, and the monitors and
  approval poller must be deleted, not flagged, or the "unattended" property
  comes back. Not recommended unless the digests are actually missed.

Either way, **first**: stop the trading service on Railway (owner action). It
still runs the scheduler, both monitors and the approval poller with no one
watching.

### 3b. `research` — new repo: Claude/Codex as tutor and research partner

Markdown and a few CLIs. No database, no service, no MCP server, no Railway.
Git is the system of record; a session that ends loses nothing that was
written.

**Why no MCP server.** The chat that "kept using web" almost certainly never
had the workspace connected: this session shows the same
`swingtrader-workspace (INVALID_CONFIG)` because `WORKSPACE_BASE_URL` was not
exported, and the server also needs the Railway service up. And even connected,
`filings_recent` only served 13D/G and Form 4, not financial statements, so a
10-K question would still have gone to web search. A CLI checked into the
repo has neither failure mode. (Likely, not verified against that chat's
transcript.)

Layout:

```
AGENTS.md                 tutor + research-partner brief (CLAUDE.md imports it)
.claude/agents/           company-researcher, thesis-critic (opus), filings-analyst
.claude/skills/edgar/     how to call the edgar CLI, with examples
.claude/skills/alphasense/ the owner's prompt-generator skill, copied in
tools/edgar.py            the SEC client + CLI (extracted, ~600 lines)
tools/rh_snapshot.py      Robinhood read-only snapshot (extracted, ~400 lines)
research/<TICKER>/        dossier.md, thesis.md, invalidators.md, sources/
research/INDEX.md         one line per name: status, last touched, thesis in a sentence
journal.md                dated decisions, including passes
learning/concepts.md      every concept taught: what, why it mattered here, where it shows in the filing
portfolio/snapshot.json   written by rh_snapshot; read by the "situate" step and, later, the website
```

**The tutor brief** (the one piece worth a stronger model's edit pass):

- Keep from the old AGENTS.md: sourced facts only, unsourced recall is not
  evidence; the critic is never asked to be balanced; journal every decision
  including passes; write invalidators before conviction; no personalized
  advice, and say so when a question crosses the line.
- Drop: the tool table, the scopes, the four non-negotiables as
  test-asserted properties (there is nothing to place an order with), the
  evidenced/discretionary budgets, SetupSpecs and cohorts.
- Add, and this is the point: **teach the why**. Every session that reads a
  filing explains the metric or structure it just used (what it is, why it
  matters for *this* business model, where it shows up in the document), and
  appends to `learning/concepts.md` rather than repeating a concept already
  logged. The session reads the concepts log at start, the same way it reads
  the dossier, so teaching compounds instead of restarting.
- The loop becomes: recall (dossier + INDEX + concepts) → frame the question
  → gather (edgar CLI, AlphaSense report if one exists, web last) → attack
  (critic) → situate (snapshot.json) → record (dossier delta, journal,
  concepts).

**`edgar` CLI** — the token-efficiency answer. Free SEC endpoints, no key,
only `SEC_USER_AGENT`. Compact Markdown tables, cached on disk under
`.cache/edgar/` so the second session on a name costs nothing:

```
edgar facts BE --concepts revenue,gross_profit,fcf,shares --years 5
edgar filings BE --forms 10-K,10-Q,8-K --since 2024-01-01
edgar section <accession> --item 7 --max-chars 12000   # MD&A, truncated on purpose
edgar insiders BE --days 180                             # Form 4 summary
edgar peers BE                                           # SIC-code peers from the submissions feed
```

If the owner's "EDGAR key" is a third-party one (sec-api.io, EDGAR Online),
it slots in as an optional full-text-search backend; the SEC's own `efts`
full-text endpoint is the default. Which key it is decides whether this is a
20-line addition or nothing.

**AlphaSense** — a workflow, not an integration, until API access is
confirmed. The session runs the owner's prompt-generator skill to produce the
query; the owner runs it in AlphaSense; the report lands in
`research/<TICKER>/sources/alphasense-<date>.md` with front-matter (query,
date, doc types covered); the dossier cites it like any other source, and
untrusted-document rules still apply to its text. If AlphaSense API access
exists, `tools/alphasense.py` is a later PR.

**Robinhood** — `rh_snapshot.py` writes `portfolio/snapshot.json`
(positions, cost basis, lots, cash, `as_of`). Two consumers: the research
loop's situate step now, and the personal-site page later. The page and Muse
are out of this build; the contract they will read is this file's schema, so
nothing here has to change when that work starts. Whether the site pulls the
file from this repo or runs its own copy of the script is decided there.

## 4. PR graph (not spawned)

New repo, all independent unless marked:

1. `skeleton` — AGENTS.md tutor brief, formats, INDEX, three briefs, README,
   CI = `compileall` + a handful of unit tests, one job. Sonnet, then one
   opus pass on AGENTS.md only.
2. `edgar-cli` — extract `filings/client.py`, `sec_minimal.py`, `form4.py`
   into `tools/edgar.py`; the five commands above; disk cache; tests on the
   recorded fixtures that already exist (`scripts/record_sec_fixtures.py`).
   Sonnet.
3. `rh-snapshot` — extract the read paths from `robinhood.py` + token store +
   auth script; `mcp<2` pin travels with it. Sonnet. Owner runs the auth step.
4. `alphasense-skill` — copy the owner's skill in, the sources/ convention,
   the front-matter. Sonnet. Needs the skill's path from the owner.
5. `research-export` — last `sync_research_mirror.py` run against Railway
   Postgres, copy `research/` into the new repo, backfill INDEX. Sonnet. Needs
   `DATABASE_URL` from the owner.

`swingtrader`:

6. `freeze` — README status rewrite, tag, delete the two extra workflows,
   trim `ci.yml` to one SQLite shard so anything that does get pushed is
   cheap. Sonnet. Owner stops Railway and (optionally) archives.

## 5. Out of this build

- The private finance page and the Muse integration. The data contract is
  `portfolio/snapshot.json` + whatever Muse writes into the same
  `{accounts:[{name, balance, as_of, source}]}` shape; that work lives in the
  personal-site repo.
- A `compare_setups` autopsy. If it ever matters, a notebook over the
  Sharadar export answers it more cheaply than the engine did.

## 6. Teaching design (v3)

Built on the synced `teach` skill (MISSION, RESOURCES, learning-records,
lessons and reference as HTML, NOTES), because its heuristics are exactly the
"don't overteach" rules:

- **Mission first.** `learning/MISSION.md` says why: to read a 10-K and a
  business model well enough to form and defend a thesis. Every lesson ties
  to it or is not written.
- **Zone of proximal development.** The session reads `learning-records/`
  before teaching anything; it teaches the next thing, not the whole thing.
- **One lesson per research session at most, and only when the research
  hits it.** Research is the trigger; a concept that came up (say, deferred
  revenue at a company that just changed its billing) becomes the lesson. No
  scheduled curriculum, no lesson because the calendar said so.
- **Knowledge is cheap, skill is effortful.** The explanation stays short and
  cited; the retrieval practice (a quiz, "find this line in the filing
  yourself") is where difficulty is allowed.
- **Reference over lessons.** Lessons are rarely reread; `reference/*.html`
  (glossary, ratio cheat-sheet, "how to read segment notes") is what gets
  revisited and is kept current.
- **Sources, never parametric knowledge.** `RESOURCES.md` grows first; a
  lesson without a primary source is not shipped.

Layout in the research repo: `learning/{MISSION.md,RESOURCES.md,NOTES.md,
learning-records/,lessons/,reference/,assets/}`. The old `concepts.md` idea is
replaced by learning-records, which are the same thing with a format.

**Artifacts.** This account has the Slides, Docs, Design and Design System
artifact types today (verified from this session). Lessons stay HTML files in
the repo (the skill's unit, and git is the record), and the session may also
publish a lesson as an Artifact page, a reference doc as a Docs artifact, or a
walk-through as Slides when that reads better than a file. Publishing is
additive; the repo file is canonical.

## 7. Credentials and owner actions

- **Railway:** no token or CLI in this environment. `LOCAL_AGENT_PROMPT.md`
  is a paste-ready prompt for a local agent with the Railway CLI: stop the
  bot service, read the SEC variable, run the final research export.
- **EDGAR:** no SEC/EDGAR variable is visible from this session. The code
  needs only `SEC_USER_AGENT` (a contact string; the SEC issues no key). If
  the thing set up is a sec-api.io or similar key, it is an optional
  full-text backend in the edgar CLI.
- **AlphaSense skill:** owner adds the file later; brief 4 stays open.
- **New repo:** owner creates it (out of this session's repository scope).
  Proposed name below.

## 8. Repo name

Needs "research" in it, must not corner a later generalization past stocks.
Recommendation: **`research-bench`** — a bench is where you do the work and
learn the craft, and nothing in it says finance. Alternates: `research-desk`,
`loupe-researcher`. First `README` line: "A research partner that teaches while
it works. Currently scoped to public companies."

## 9. On the frozen codebase

Would a reader think it is bad? Not bad: over-built. It is documented,
tested, and honest about what is unverified (`SYSTEM_OVERVIEW.md` §9 is a
strength most repos lack). What reads poorly is scope judgment: 150k lines
and a K–Q spec series for one person's bot, root-level clutter
(`todoscratchpad.md` full of nightly reconcile logs,
`ARCHITECTURE_EVOLUTION_TRIGGERS.md`, `swing-trader-prd.md`). The freeze PR
owns that in the README ("what worked, what I over-built, what I would do
differently") and tidies the root. A repo that says that about itself reads as
judgment, not as a mess.
