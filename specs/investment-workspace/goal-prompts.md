# Dispatch prompts — Investment Workspace K–Q

**Series:** [README](README.md) · **Date:** 2026-09-05

One Codex `/goal` contract per delivery phase (README §6). Paste the block body into
the Codex composer **after typing `/goal`** — the blocks below deliberately omit the
slash command. Run them in order; phases 3 and 5 may run in parallel once phase 0a is
merged, and do not wait for 0b.

Canonical validation command for this repo (matches CI):

```bash
python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .
```

## Standing rules for every phase

Repeat these in each goal, or bake them into `AGENTS.md` once and reference them:

- Do not delete, skip, weaken, or narrow tests to make the goal pass.
- Do not refactor unrelated code. Do not add dependencies without saying why in the PR.
- Every new capability ships behind a flag defaulting to **off**.
- Never write code that lets an agent, tool, or MCP surface place a broker order.
- Never write code that lets a model produce a statistic.
- Pause and ask before changing production capital limits, feature flags in Railway, or
  anything under `execution/` that touches live placement.

---

## Phase 0a — Schema discipline (blocks everything; dispatch first)

```
**Objective:** Land the Alembic baseline and make the existing SQLAlchemy models run identically on SQLite and Postgres, per specs/investment-workspace/K-workspace-core-and-tool-surface.md section 3.1-3.2 and specs/investment-workspace/strategy-lab/strategy-lab-architecture.md section 14. Do not perform the production cutover in this phase.
**Read first:** specs/investment-workspace/K-workspace-core-and-tool-surface.md, specs/investment-workspace/strategy-lab/strategy-lab-architecture.md section 14, database/db.py, database/models.py, .github/workflows/ci.yml
**Constraints:** no behavior change to scanning, scoring, memos, or execution; no new tables in this phase; fix SQLite-isms (INSERT OR IGNORE, sqlite ON CONFLICT forms, naive datetimes, Boolean/JSON assumptions, WAL pragmas applied unconditionally) so the models are engine-neutral; add a Postgres service to the CI matrix alongside SQLite; forbid new inline ALTER TABLE or create_all() schema changes after the baseline, enforced by a test; do not delete swing_trader.db
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change, against both SQLite and Postgres
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; log each SQLite-ism found and how it was made engine-neutral
**Stop when:** the full suite passes identically on SQLite and Postgres in CI, an empty-database `alembic upgrade head` produces the exact current schema on both engines, and a test fails if anyone adds a schema change outside Alembic — OR when a schema decision needs human input.
```

## Phase 0b — Cutover (blocks Phases 1, 2, 4; not 3 or 5)

```
**Objective:** Migrate SwingTrader's production persistence from SQLite to Postgres on Railway with proven parity, and stand up the workspace service skeleton, per specs/investment-workspace/K-workspace-core-and-tool-surface.md section 3.2 and section 6.
**Read first:** specs/investment-workspace/K-workspace-core-and-tool-surface.md, database/db.py, the Phase 0a documentation, railway.toml, Dockerfile
**Constraints:** requires Phase 0a merged; scripts/migrate_sqlite_to_postgres.py dumps tables in dependency order, verifies row counts and per-table content hashes on both sides, is idempotent, refuses a non-empty target without --force, and writes its report to docs/audits/; keep the SQLite file as a read-only archive; the workspace service is a separate Railway service and the bot process does not import it; WORKSPACE_API_ENABLED defaults false; pause before touching Railway variables or services — that is an owner action
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; log which tables have verified row-count and content-hash parity
**Stop when:** the migration script is verified by a test that round-trips a populated SQLite fixture into Postgres with matching hashes, the service skeleton answers /health on both engines, and the Railway cutover steps are written as an owner runbook — OR when a Railway or credential action is needed.
```

## Phase 1 — See it

```
**Objective:** Build the portfolio ledger and the read-only workspace tool surface so holdings, cash, exposure, and freshness are answerable from any agent client, per specs/investment-workspace/L-portfolio-ledger-and-brokers.md and K sections 4-6.
**Read first:** specs/investment-workspace/L-portfolio-ledger-and-brokers.md, specs/investment-workspace/K-workspace-core-and-tool-surface.md, execution/brokers/, tracking/position_reconciliation.py, docs/ROBINHOOD_INTEGRATION_PLAN.md
**Constraints:** read paths only — no order placement code in this phase; sync is append-only and never zeroes a position on error; null cost basis stays null; no Schwab adapter (owner deferred it) — ship the BrokerCapabilities contract, a fake broker, and the contract tests instead; PORTFOLIO_SYNC_ENABLED defaults false
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; log which spec-L test-plan rows are passing
**Stop when:** every test in specs/investment-workspace/L-portfolio-ledger-and-brokers.md section 8 passes, including test_no_agent_path_to_broker, and portfolio_overview returns holdings with provenance and staleness — OR when Robinhood's protective-exit capability cannot be determined without an owner decision.
```

## Phase 2 — Remember it

```
**Objective:** Implement the research workspace — dossiers, theses, invalidators, decision journal, and the git Markdown mirror — per specs/investment-workspace/M-research-workspace.md.
**Read first:** specs/investment-workspace/M-research-workspace.md, specs/investment-workspace/K-workspace-core-and-tool-surface.md sections 3.3 and 4.2
**Constraints:** append-only revisions, never overwrite; a thesis cannot reach `active` without at least one machine-checkable invalidator; a triggered invalidator pages and never creates an order or a proposal; mirror direction is Postgres to repo; RESEARCH_WORKSPACE_ENABLED defaults false
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; log the invalidator types implemented and their check jobs
**Stop when:** every test in specs/investment-workspace/M-research-workspace.md section 8 passes, including test_mirror_roundtrip and test_offline_read — OR when an invalidator type needs a product decision about what counts as triggered.
```

## Phase 3 — Measure it (the centerpiece)

```
**Objective:** Build the comparable-setups engine in comparables/ so a setup question returns an estimate with sample size, benchmark-adjusted and policy-simulated outcomes, uncertainty, regime split, balance diagnostics, and null tests — per specs/investment-workspace/N-comparable-setups-engine.md.
**Read first:** specs/investment-workspace/N-comparable-setups-engine.md in full, backtest/simulator.py, backtest/event_replay.py, data/analog_ranker.py, data/event_outcomes.py
**Constraints:** reuse backtest/simulator.py for policy-simulated outcomes — do not reimplement exit semantics; no field in CohortAnswer is optional; `insufficient` is a valid return and must never be softened into a hedge; no model output may become a number; do not tune a setup to make a cohort look better; COMPARABLE_SETUPS_ENABLED defaults false
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; after each one, log which of the twelve failure modes in section 2 now has a passing named test
**Stop when:** every test in section 10 passes (including the universe, delisting-return, decay, Wilson, and shrinkage rows), policy-simulated outcomes reconcile exactly with the existing event replay, and a real run against the warmed event library produces one `clean_pit` and one `insufficient` answer — OR when a statistical choice needs human judgment.
```

## Phase 4 — Widen it

```
**Objective:** Build the filings, vintage-correct macro, and timestamped news evidence planes into source_observations, per specs/investment-workspace/O-evidence-planes.md.
**Read first:** specs/investment-workspace/O-evidence-planes.md, data/macro_data.py, data/news_data.py, data/sec_data.py, specs/investment-workspace/strategy-lab/strategy-lab-architecture.md section 8
**Constraints:** every fact carries valid_at and known_at_utc; a guessed known_at_utc means replay_eligible=false; no LLM in the regime classifier; 13F positions never render without staleness and portfolio share; Form 4 transaction types are never pooled; respect source rate limits in one client module; each plane flag defaults false
**Validate:** `python -m unittest discover -s tests -p "test_*.py" && python -m compileall -q .` after each change
**Document:** Write concise, targeted documentation for all changes — create new .md files or update existing docs as needed.
**Checkpoints:** work in checkpoints; log entity-resolution coverage and unmapped-CUSIP counts
**Stop when:** every test in specs/investment-workspace/O-evidence-planes.md section 6 passes and a past-dated macro query demonstrably differs from the current print on a revised series — OR when a data source requires credentials or a paid plan the owner has not approved.
```

## Phase 5 — Race it

Strategy Lab ships on its own six-PR plan. Use the existing per-agent prompts rather
than a new contract: `specs/investment-workspace/strategy-lab/strategy-lab-agent-prompts.md`.

## Phase 6 — Act on it

Do not dispatch until Phases 0–3 are merged and Robinhood's protective-exit capability
is documented with evidence. The order proposal, approval, and execution lifecycle is
specified in Spec L §6 and Spec Q §12; it touches live capital and is the one phase that
should be written with a human in the loop rather than by an autonomous goal run.
