# Simplification plan — from trading agent to research tutor

Status: **proposal, not ratified.** Written 2026-09-17 on `claude/gh-minutes-simplify-kamdrh`.
Nothing below is scheduled until the owner picks a direction (§5).

## 1. What the evidence says

- The repo is ~150k lines of Python, 49k of them tests (143 files, ~1,469 tests).
- CI on this repo ran 273 times in the week of 2026-09-10 to 09-16, at 9–15 min
  wall-clock each, across 7 jobs per PR and 12 per push to `main` (4 shards x
  1–2 engines + count checks), plus `attestation-check` and `model-lint` on
  every event. That is the whole $35.71.
- The "failure" emails are almost all **cancelled** runs on `main`: the
  `concurrency` block cancels the in-flight run whenever the orchestrator merges
  the next PR minutes later (15 of 43 push runs in the sample). Real failures
  in the sample: 6 PR runs, all fixed by the next push.
- So the cost is *velocity x suite size*, and the velocity is the
  orchestrated-build pattern itself: 198 commits in three weeks. Trimming CI
  helps; not building 130k lines you do not want helps more.

## 2. What the owner actually wants (restated)

1. Claude/Codex as a **research tutor**: dig into a company, and teach the
   financial-analysis and business-model reasoning while doing it.
2. A **private page on the personal site** showing finances overall: account
   balances (updated by Muse, Meta's agent — integration surface unverified),
   plus Robinhood positions/balances, clearly.
3. No autonomous trading agent, scheduler, monitors, or proposal/approval
   machinery.

What worked and is worth keeping in spirit: the sourced-facts discipline,
dossiers and the decision journal, the adversarial critic. What did not deliver:
`compare_setups` (the cohort engine), which is also the single most expensive
subsystem to keep alive (comparables 7k + backtest + Sharadar backfills + the
statistical test load).

## 3. Two ways to get there

### Option A — fresh minimal repo, archive this one (recommended)

Extract the ~3–5k lines that matter into a new repo (`research`, or a folder
of the personal-site repo); tag this repo `trading-agent-final` and archive it.

Kept, by extraction not deletion:

| Keep | From | Notes |
|---|---|---|
| Dossiers, theses, journal as Markdown | `research/` (already the git mirror) | run `scripts/sync_research_mirror.py` once more first so Postgres → Markdown is current |
| Agent instructions, rewritten as a tutor brief | `AGENTS.md` §1–2, §5 | drop the tool table, keep: sourced facts only, critic never balanced, journal every decision incl. passes |
| Three subagent briefs | `.claude/agents/{company-researcher,thesis-critic,filings-analyst}.md` | retarget to `WebSearch`/`WebFetch` + EDGAR; no MCP server |
| Robinhood read-only client | `execution/brokers/robinhood.py` (1.6k, strip to positions/balances/lots), `database/token_store.py`, `scripts/robinhood_auth.py` | `mcp<2` pin travels with it |
| One balances script + cron | new, ~150 lines | writes `finances.json`; GH Actions `schedule` or Railway cron, 1 job/day |

Dropped: everything else, including Postgres, Alembic, the FastAPI/MCP
workspace, both Railway services, Telegram, the strategy lab, execution,
orchestrator, comparables, backtest, evals/attestation.

Cost: 3–4 worker PRs, sonnet-class. No test churn because nothing is deleted
in place.

### Option B — gut this repo in place, keep the workspace MCP

Keep `workspace/`, `research_workspace/`, `portfolio/` (ledger only),
`database/`, `migrations/`, `filings/`, `data/`, `utils/`, `config/`; delete
`orchestrator/`, `execution/` (except brokers), `bot/`, `strategy_lab/`,
`backtest/`, `comparables/`, `scanning/`, `screening/`, `evals/`, `tools/`.

Blockers found: `database/models.py` imports `strategy_lab.domain`, so the
domain model has to be extracted before `strategy_lab/` can go; `execution/`,
`strategy_lab/`, `portfolio/` and `database/` are the most-imported packages in
`tests/`, so every deletion PR drags a test-repair tail. Expect 8–12 PRs and
~40k lines still standing, plus Postgres and Railway still running.

Only worth it if the hosted MCP endpoint is something you use from clients
other than Claude Code / Codex on your own machine. If it is not, Option B keeps
infrastructure whose only consumer is you, locally.

## 4. Regardless of option — stop the bleeding now

1. **Owner action:** stop the trading service on Railway (`python main.py`).
   With no owner in the loop it still runs the scheduler, both monitors and the
   approval poller.
2. **CI trim PR** (one worker, sonnet, mechanical): drop `attestation-check`
   and `model-lint`; shards 4 → 2; SQLite only on PRs *and* pushes with a
   `workflow_dispatch` for Postgres; delete `test-count-check`; on `main` set
   `cancel-in-progress: false` so back-to-back merges stop emailing you. Cuts
   per-run cost roughly 3x and ends the cancelled-run mail. Skip this if Option
   A is chosen and the repo is going to be archived within the week.

## 5. Decisions only the owner can make

1. **A or B.** Recommendation: A.
2. **Where the private finance page lives.** The site's stack and auth are not
   in this repo. Requirement either way: real auth in front of it (Cloudflare
   Access, Vercel auth, or the site's existing login), never obscurity, and no
   account numbers in the rendered data. Alternative worth considering: render
   it locally and never host it.
3. **Muse.** I could not verify what surface Meta's Muse offers for writing
   balances out. Plan around a data contract Muse *can* fill however it
   fills it: a private `finances.json` with
   `{accounts:[{name, balance, as_of, source}], positions:[...]}`. Robinhood
   and Muse both write to the same schema; the page reads only that file.
4. **Is `compare_setups` worth a 30-minute autopsy before it goes?** My guess
   is the failure is data (Sharadar backfill / universe) rather than the method,
   and that a one-off notebook would answer the same question more cheaply if
   it ever matters again. Unverified.

## 6. Proposed PR graph for Option A (not spawned)

1. `export-research` — final Postgres → Markdown sync; commit `research/`
   snapshot; tag `trading-agent-final`. (sonnet)
2. `research-repo-skeleton` — new repo: tutor `AGENTS.md`, three briefs, the
   dossier/journal format, a `learning/` log for concepts covered. (sonnet;
   the tutor brief itself is the one piece worth a stronger model's edit pass)
3. `robinhood-readonly` — stripped client + token store + auth script + the
   daily balances cron writing `finances.json`. (sonnet; touches credentials
   so the owner runs the auth step)
4. `finance-page` — in the personal-site repo, behind auth, reads
   `finances.json`. (depends on decision 2)

Each is independent of the others except 4 → 3.
