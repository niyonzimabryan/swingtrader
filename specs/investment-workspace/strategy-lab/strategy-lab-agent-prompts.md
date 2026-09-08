# SwingTrader Strategy Lab: Agent Goal Prompts

These prompts implement `specs/strategy-lab-architecture.md`. Use one thread per prompt. Each worker must use a separate git worktree and branch.

## Shared rules for every agent

1. Read `specs/strategy-lab-architecture.md`, `docs/SYSTEM_CAPABILITIES.md`, `swing-trader-prd.md`, `README.md`, `CONTRIBUTING.md`, and relevant existing code before editing. Read `.claude/napkin.md` if present; do not fail or create a competing runbook when it is absent because this repo uses `todoscratchpad.md`.
2. Inspect `git status` first. Preserve unrelated changes. Work from the exact base
   ref supplied by the owner and verify both spec files exist there. After the spec
   branch merges, that base is current `origin/main`; never start from a ref that
   does not contain these specifications.
3. Do not change production Railway variables, broker credentials, live notional limits, or scheduled jobs.
4. Do not enable any Strategy Lab feature flag in production.
5. Never submit a live broker order. Broker tests use fakes/mocks. Any external canary requires separate explicit owner authorization.
6. Reuse existing scan, simulator, order manager, broker adapters, logs, and DB helpers. Do not build parallel copies.
7. Strategy modules must be deterministic and have no broker, DB-session, Telegram, network, or LLM imports.
8. Any rule or parameter change creates a new immutable strategy version.
9. Add tests for every branch, boundary, error path, idempotency path, and live-safety refusal.
10. Run focused tests, the full unittest suite, compileall, model lint, secret scan where available, and an independent code review.
11. Update docs and `todoscratchpad.md` only for changes owned by the PR. Do not rewrite existing user-authored ledger content.
12. Open a PR with evidence. Do not merge unless the user explicitly authorizes merge in that thread.
13. Execution mode is carried by the immutable experiment arm. Never infer a Strategy Lab arm's broker or mode from mutable global settings.

## Master orchestration prompt

```text
GOAL: Implement the SwingTrader Strategy Lab end to end from
specs/strategy-lab-architecture.md using the phased PR plan in Section 15.

Start by auditing the supplied base ref containing these specs and converting the
spec into a dependency graph. Execute PR 1 first. After PR 1 merges, PR 2 and PR 5 may run in separate
worktrees because their file ownership does not overlap. Run PR 3 after PR 2,
then PR 4 after PRs 1-3. Run PR 6 last, after PRs 1-5.

For every PR:
- preserve current behavior behind default-off flags;
- implement all acceptance criteria for that phase;
- add complete tests, including error and safety paths;
- run the full repo verification suite;
- perform an independent review and resolve all P0/P1 findings;
- open a focused PR with commands, results, schema/flow notes, rollback, and
  explicit production steps that were NOT performed.

Hard boundary: no production variable changes and no live orders. Stop for owner
input if Robinhood cannot verifiably enforce the requested protective exit
lifecycle, if a migration cannot be rehearsed against a production-shaped SQLite
copy, or if a proposed change would alter existing memo/order behavior while the
Strategy Lab flags are off.

Report progress as a checklist against Sections 15, 16, and 19 of the spec.
The final handoff must identify what is merged, what is deployed but disabled,
what evidence was collected, and exactly what still requires owner authorization.
```

## Agent 1: Domain and migration foundation

```text
GOAL: Implement PR 1 from specs/strategy-lab-architecture.md: Alembic foundation,
Strategy Lab domain contracts, and experiment persistence. No pipeline or broker
behavior changes.

Scope ownership:
- alembic.ini and alembic/
- database/models.py and database/db.py only as required for migration coexistence
- new strategy_lab/domain.py and strategy_lab/registry.py
- migration/domain tests

Requirements:
1. Baseline the current SQLAlchemy/SQLite schema with Alembic. Existing databases
   must upgrade without data loss; empty databases must initialize correctly.
2. Add the nine specified tables and constraints: source_observations,
   strategy_versions, experiments, experiment_arms, market_snapshots, strategy_decisions,
   strategy_trades, experiment_metric_snapshots, and promotion_events. Decisions
   are unique per arm + snapshot + non-null ticker so a universe snapshot can emit
   one decision per constituent.
3. Implement enums/dataclasses/validation for lifecycle states, action/mode,
   strategy metadata, decisions, and promotion events.
   Promotion events must bind a source-evidence arm, its evidence snapshot, and a
   distinct inactive target-tier arm sharing the immutable strategy version; no
   strategy-version-only authorization is valid. Add a database-level partial unique
   constraint allowing at most one globally active live arm.
4. Enforce immutable registered experiments and immutable strategy versions at
   service/domain boundaries. Database rows may have status changes but frozen
   rule/config fields cannot mutate after registration.
5. Enforce idempotency constraints on strategy decisions and active arms.
   `strategy_trades` must have a persisted execution ID and a database-enforced
   maximum of one nonterminal execution per decision.
6. Replace production startup ordering: classify schema, back up a legacy database,
   stamp only an exact known legacy signature, and run Alembic before opening ORM
   sessions. Empty DBs run upgrade-to-head. Unknown/partial schemas fail closed.
   `Base.metadata.create_all()` is allowed only in an explicit ephemeral-test helper,
   never as a production fallback after Alembic owns the schema.
7. Document how Alembic becomes authoritative and forbid new inline ALTER migrations.
8. Rehearse upgrade using a test fixture shaped like current production schema,
   run upgrade twice, and test backup/restore instructions.

Acceptance:
- Existing app starts against empty and upgraded SQLite DBs.
- Existing test suite passes unchanged.
- Migration downgrade/restore path is documented; no destructive production
  operation is performed.
- New domain objects reject invalid state transitions and malformed data.
- PR includes an ASCII schema/state diagram and exact verification output.
```

## Agent 2: Snapshot and strategy SDK

```text
GOAL: Implement PR 2 from specs/strategy-lab-architecture.md: a point-in-time
snapshot builder, deterministic strategy SDK, execution-policy contracts, and
the initial strategy roster. No broker calls and no scheduler integration.

Prerequisite: PR 1 merged.

Scope ownership:
- strategy_lab/snapshots.py
- strategy_lab/execution_policy.py
- strategy_lab/validation.py
- strategy_lab/strategies/*
- strategy unit/golden tests

Requirements:
1. Build immutable ticker- and universe-scoped MarketSnapshot objects from existing
   price/event/research data with one cutoff, provenance, staleness/PIT flags,
   universe version, and a deterministic content hash. Cross-sectional ranks must
   use one shared universe snapshot, never per-ticker snapshots from different times.
   Capture normalized inputs in a bitemporal source-observation ledger with
   `valid_at` and `known_at_utc`; snapshots reference those immutable observations.
   Existing backfilled price/earnings rows without availability provenance are
   `archival_reconstructed` and replay_eligible=false.
2. Implement the Strategy protocol. Same strategy version + market snapshot must
   yield the same ordered per-constituent decision set and hashes. Ticker strategies
   emit one draft; universe strategies emit one draft for every constituent and a
   pure SDK validator validates cardinality, membership, uniqueness, and ticker
   sort order. PR 3's runner must invoke this validator before persistence.
   Portfolio/risk context is excluded from strategy evaluation and is assessed
   separately immediately before execution.
   Build a canonical transitive implementation manifest covering strategy source,
   output-affecting project helpers, execution policy, indicator formulas, and pinned
   runtime/library versions. Recompute it at activation and before every run; refuse
   a mismatch with the registered manifest hash until a new version is registered.
3. Implement swingtrader_composite_v1 as a compatibility adapter without changing
   current ScoringEngine output. Freeze the already-produced final score,
   classification, direction, signal breakdown, trade parameters, model/trace
   provenance, output hash, and portfolio-context hash into the snapshot. The
   adapter maps stored values and makes no LLM call. Mark it historically_replayable=false.
4. Implement earnings_drift_v1, momentum_v1, and short_term_reversal_v1 exactly as
   specified in the normative V1 reference configuration, with all thresholds and
   execution-policy versions declared in immutable version config. Do not tune or
   replace those values in this PR.
   Trace each rule to a cited source or label it a project assumption.
5. Abstain on missing, stale, contradictory, or non-PIT-safe dependencies. Never
   impute or silently default a trading input.
6. Make short_term_reversal_v1 structurally shadow-only in metadata.
7. Add hand-computed golden vectors, boundary cases, stale/missing data tests,
   no-network tests, deterministic hash tests, and import-boundary tests proving
   strategy modules cannot reach brokers/DB/Telegram/LLMs.
8. Use the normative calendar-day and intrabar-stop policies exactly as written so
   they match backtest/simulator.py. Do not add a session-count or close-triggered
   exit under the same version.

Acceptance:
- Ticker strategies run on the same synthetic ticker snapshot; momentum runs every
  constituent from one synthetic universe-scoped snapshot.
- Replayability guard rejects the current LLM composite in historical mode.
- Every decision includes structured reason codes and a versioned execution plan.
- A source edit without a new registered version is rejected before evaluation.
- No existing behavior changes because nothing is wired into the pipeline yet.
```

## Agent 3: Experiment runner, replay, and measurement

```text
GOAL: Implement PR 3 from specs/strategy-lab-architecture.md: experiment
registration/runtime, multi-arm shadow execution, historical replay adapters, and
honest evaluation. Do not integrate the production scheduler or any broker.

Prerequisites: PRs 1 and 2 merged.

Scope ownership:
- strategy_lab/runner.py
- strategy_lab/shadow.py
- strategy_lab/metrics.py
- backtest integration and a new CLI/report artifact
- experiment/evaluation tests

Requirements:
1. Register and freeze experiment hypotheses, variants, metrics, universe, data
   cutoff, benchmarks, and end criteria before running arms.
2. Run N arms over one snapshot idempotently; preserve abstentions and blocks.
   Universe arms must persist one decision per constituent under the shared snapshot.
   Invoke PR 2's pure validator before persistence and keep `long`, `flat`, and
   `abstain` rows.
3. Reuse backtest/simulator.py fill/exit behavior. Do not duplicate its semantics.
4. Refuse historical replay for non-PIT or historically_replayable=false inputs.
   Reconstructed archival data may produce a clearly separated exploratory report,
   but it cannot enter clean metrics, rank a winner, or satisfy promotion gates.
5. Support strategies with different maturity horizons without ranking open or
   immature observations as zero.
6. Compute net results after explicit costs, drawdown, exposure, turnover, R,
   profit factor, benchmark-relative performance, overlap/correlation, and
   uncertainty intervals. Always show n and warning codes.
7. Track every tried variant and implement multiple-testing diagnostics when
   sample requirements are met. Otherwise emit insufficient_evidence.
8. Provide a deterministic CLI producing JSON and Markdown scorecards.
9. Add tests for chronological splits, embargo/purge of overlapping labels,
   untouched holdout enforcement, missing costs, small samples, correlated arms,
   idempotent reruns, and pessimistic fills.
10. Separate reproducible market decisions from fresh execution eligibility. Test
    that the same decision can be blocked after portfolio state changes without
    mutating or duplicating the original decision.
11. Test bitemporal cutoffs: observations known after the decision cutoff are
    rejected; superseded/revised records resolve as of cutoff; reconstructed rows
    never satisfy promotion evidence.

Acceptance:
- One fixture registers champion + three challengers, replays eligible arms,
  shadows all four, matures trades, and produces a labeled scoreboard.
- No metric calls a small-sample leader a winner.
- Current event-replay tests still pass and fill semantics remain single-sourced.
```

## Agent 4: Pipeline integration and operator control

```text
GOAL: Implement PR 4 from specs/strategy-lab-architecture.md: default-off shadow
integration with current scans plus owner-only Telegram visibility and controls.

Prerequisites: PRs 1, 2, and 3 merged.

Scope ownership:
- narrow orchestrator/pipeline.py hook
- scheduler configuration/jobs
- bot handlers/keyboards/weekly report
- config/settings.py and .env.example flags
- shadow-only integration/E2E tests

Requirements:
1. With all new flags false, prove current scans, memos, paper auto-approval, and
   broker paths behave identically.
2. With shadow enabled, build one snapshot after existing analysis and run all
   active shadow arms. Strategy Lab failure must not block existing memo delivery.
3. Continue the legacy ScoredCandidate ledger during migration.
4. Add owner-only /experiments, /strategies, /strategy, pause/resume commands and
   weekly scoreboard. Exclude promote/live-kill commands until their underlying
   safety services are merged.
5. Display samples, maturity, costs, drawdown, benchmark, uncertainty, correlation,
   and warnings. Recommendations are informational only.
6. Add funnel/log events with no sensitive payloads.
7. Test duplicate scans, restart, DB failure isolation, Telegram authorization,
   empty data, partial results, and long-duration pending positions.
8. Build cross-sectional snapshots once per rebalance cutoff, not once per ticker.

Acceptance:
- Mocked full scan produces existing memo plus N strategy decisions.
- Disabling flags produces zero new writes and zero output changes.
- No broker method is called by shadow mode under any tested failure path.
```

## Agent 5: Live lifecycle and Robinhood safety closure

```text
GOAL: Implement PR 5 from specs/strategy-lab-architecture.md: a persistent,
broker-independent order lifecycle that closes the current Robinhood monitoring,
protective-exit, concurrency, and reconciliation gaps. Do not enable live trading
or submit a real order.

Prerequisite: PR 1 merged. Coordinate database ownership before editing models or
migrations.

Scope ownership:
- execution broker capability contract
- persistent risk reservation/order lifecycle services
- execution/brokers/robinhood.py as required
- tracking/position_reconciliation.py and monitor integration
- kill switch/incident alerts
- broker contract and safety tests

Requirements:
1. Implement the exact persistent state machine and invariants in Section 12.
   Persist one execution ID before reservation and enforce at most one nonterminal
   execution per decision with a database constraint, not an in-process lock alone.
2. Discover current Robinhood MCP/order capabilities from official documentation
   and a read-only/review-only interface inspection. Do not assume attached OCO,
   fractional stop, partial-fill, or replace behavior.
3. Refuse a live order before placement when its exit policy cannot be enforced.
4. Persist pending notional reservations before the external order call; make all
   transitions idempotent under retries and concurrent approvals.
5. Detect fills/partial fills, create or adjust protection, verify protection at the
   broker, reconcile broker/local state, and recover after process restart.
6. Block all new live entries on protection failure, unknown placement outcome,
   reconciliation mismatch, or kill switch.
7. Redact logs/payload persistence. Preserve request IDs and allowlisted fields only.
8. Add a shared fake-broker contract suite and exhaustive safety regression tests
   proving zero order calls when any gate is missing or state is unsafe.
9. If the current Robinhood interface cannot provide durable protective exits,
   document the limitation and make all Strategy Lab live entries impossible. Do
   not permit a manual-exit fallback and do not substitute an in-process watcher.
10. Add an explicit execution entry point that receives immutable arm mode and
    adapter. Shadow can never reach it, paper can select only Alpaca paper, and live
    can select only the promoted live path. Reject every mismatch before review.

Acceptance:
- Fake-broker tests cover reject, timeout, duplicate retry, partial fill, protection
  failure, owner cancellation, expiry, review/order rejection, verified no-order,
  unknown placement, restart, reconciliation mismatch, closure, exact-once
  reservation release, and concurrent cap reservation.
- Current Alpaca paper behavior remains green.
- Robinhood trades are no longer silently outside reconciliation; unsupported flows
  fail closed with actionable alerts.
- PR contains no production enablement step beyond a future owner-authorized canary.
```

## Agent 6: Paper tournament, promotion, integration, and release proof

```text
GOAL: Implement PR 6 from specs/strategy-lab-architecture.md and deliver the
complete disabled-by-default Strategy Lab: paper tournament, audited promotion,
full documentation, E2E proof, and production-safe rollout plan.

Prerequisites: PRs 1-5 merged.

Scope ownership:
- paper-arm dispatcher through existing OrderManager
- strategy_lab/promotion.py
- final Telegram promote/live-kill controls
- end-to-end tests, docs, PRD, capabilities, runbook
- release verification only; no live order and no production flag changes

Requirements:
1. Dispatch eligible paper arms through the new explicit-mode OrderManager entry
   point while reusing its existing placement internals. Pass immutable mode=paper
   and the Alpaca paper adapter. Never call the current global-mode entry point for
   a Strategy Lab arm. Reject any mismatch before broker review or placement.
   Enforce independent virtual budgets, caps, no duplicate orders, and experiment tags.
2. Implement owner-only, confirmed, append-only promotions and demotions. The system
   can recommend; it cannot auto-promote. Authorizations bind the source-evidence
   arm and snapshot plus a separate inactive target arm with the same immutable
   strategy version, requested mode, and risk budget; reject evidence reuse for a
   different target. Live activation atomically replaces the one global champion or
   leaves the prior champion unchanged on failure.
3. Require complete evidence snapshot and warning acknowledgement for tier changes.
4. Make live promotion require PR 5 capability checks, all feature flags, kill-switch
   clearance, current risk gates, and owner confirmation.
   Promotion is not entry approval: add a separate signed, expiring, owner-only
   callback for each proposed live execution. It atomically approves only the
   referenced proposed execution ID and rejects replay/stale/wrong-owner/state drift.
5. Execute the complete E2E fixture: scan -> snapshot -> four arms -> shadow results ->
   paper orders -> closures -> metrics -> owner report -> promotion dry run.
6. Update README, PRD, SYSTEM_CAPABILITIES, operator runbook, environment docs, and
   architecture diagrams. State exactly what remains disabled.
7. Run full CI plus an independent pre-landing review. Fix every P0/P1.
8. Draft but do not execute the production rollout checklist: migrations, shadow flag,
   observation window, paper flag, Robinhood review-only canary, and separately
   authorized micro-live canary.

Acceptance:
- Definition of done in Section 19 is satisfied or each unmet item is explicitly
  blocked with evidence.
- Existing behavior is unchanged when all Strategy Lab flags are false.
- No live trade occurred and production capital/settings were untouched.
- A safety E2E sets global execution mode to live while dispatching a paper arm and
  proves only the Alpaca paper fake is called and the live broker sees zero calls.
- Final handoff gives exact commands/results, PRs/commits, rollback, and the next owner
  decision.
```
