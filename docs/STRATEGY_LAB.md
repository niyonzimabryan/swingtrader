# Strategy Lab (Spec Q) — the domain model, the SDK, and the experiment run

One champion, many challengers, over the same market opportunities, with the
rules frozen before the results arrive. This document covers **PR 1** — the
domain contracts in `strategy_lab/`, the eight tables in
`migrations/versions/0007_strategy_lab.py`, and the invariants the database
holds rather than trusts; **PR 2**, the point-in-time snapshot builder, the
deterministic strategy SDK, the execution-policy contract and the initial
strategy roster (§9–§14); and **PR 3**, the experiment runner, the historical
replay adapter, the shadow executor, the measurement layer and the scorecard
CLI (§15 onwards).

Nothing here runs by itself. No scheduled job registers a strategy, no pipeline
calls one, no flag turns anything on, and no broker is reachable from any of it:
the runner and the CLI are invoked by a test or by hand. PR 4 adds the
default-off pipeline hook, PR 5 the live order lifecycle, PR 6 the paper
tournament and promotion workflow.

Spec: [`specs/investment-workspace/strategy-lab/strategy-lab-architecture.md`](../specs/investment-workspace/strategy-lab/strategy-lab-architecture.md).

---

## 1. The four objects

| Object | What it is | Why it is immutable |
| --- | --- | --- |
| `StrategyVersion` | A slug, a semantic version, the rules, the config, the execution policy it references, and the transitive implementation manifest. | The rules are the version. A parameter change is a **new version**, never an edit, so an experiment cannot be rewritten after seeing its results (Spec Q §3). |
| `ExperimentSpec` | The pre-registration: hypothesis, universe, primary metric, benchmarks, guardrails, end criteria, and the planned variant count. | Frozen at registration. A primary metric chosen after the fact is not a metric. |
| `MarketSnapshot` | One point-in-time input bundle under one cutoff — a single ticker, or a whole universe with its constituents, provenance, source-observation ids and quality warnings. | It is the input a decision is reproducible from. Two arms ranking the same rebalance share one snapshot id rather than assembling ranks from snapshots taken at different moments (§7). |
| `StrategyDecision` | `long` / `flat` / `abstain` for one ticker, with reason codes and a versioned risk plan. | Its hash is the audit trail: same version + same snapshot ⇒ same hash. |

All four are frozen dataclasses in `strategy_lab/domain.py` and hash their own
canonical JSON, in the shape `comparables/setup_spec.py` established.

### What the hashes deliberately exclude

`StrategyVersion.content_hash` excludes **`status`**. A version's rules are
frozen forever; its status moves along `draft → shadow → paper → live_eligible
→ retired`. Hashing the status would make every promotion look like a rule
change, and the registry could not tell the two apart.

`StrategyDecision.decision_hash` excludes **the arm and all portfolio state**.
The same version over the same snapshot decides the same thing in shadow, in
paper and in live. Cash, exposure, open positions and broker state are
evaluated *immediately before execution* and recorded on `strategy_trades`
instead. That split is what lets a portfolio change block an execution without
mutating or duplicating the decision that preceded it (§6).

### `flat` is not `abstain`

`flat` means the strategy evaluated this constituent and did not select it.
`abstain` means it could not evaluate it — stale data, a missing dependency, or
an input that is not point-in-time safe, and nothing else. Collapsing the two
would let a data outage read as a considered decision not to trade, so the
domain refuses an `abstain` with no blocked reason and refuses a `flat` that
carries one.

The `blocked_reasons` vocabulary on a decision is exactly
`{stale_data, missing_dependency, invalid_point_in_time_input}`. A portfolio or
broker block is not a decision block; it belongs on the trade row.

---

## 2. The state machines

Four lifecycles, all in `strategy_lab/domain.py`, all with a
`require_transition` check at the service boundary. Re-asserting the state a row
is already in is allowed everywhere — that is what a retry does.

```text
EXPERIMENT (Spec Q §8)                     STRATEGY VERSION (§6)

  draft                                      draft
    |                                          |
    v                                          v
  registered ------> paused                  shadow <----+
    |     ^            |                       |         |
    v     +------------+                       v         |
  running -----------> evaluating ---> completed        paper -----+
    |                     |                              |    ^    |
    |                     |                              v    |    |
    +------> cancelled <--+                       live_eligible----+
       (from draft, registered,                          |
        running, paused, evaluating)      every state --> retired


EXPERIMENT ARM (§8)                        PROMOTION LADDER (§8)

  inactive --> active --> paused             shadow --> paper --> live
     |          |  ^        |                   (one rung at a time;
     |          |  +--------+                    a demotion may drop
     |          v                                any distance)
     +------> retired <-----+
        (terminal: an arm's evidence
         belongs to the experiment
         that is over)
```

And the execution lifecycle, transcribed from §12. PR 5 implements the service;
PR 1 carries the vocabulary because the database constraint below has to name
the terminal states in DDL.

```text
                              proposed
                                 |
   owner_rejected / cancelled / expired / risk_rejected  [terminal]
                                 |
                                 v
                          owner_approved
                                 |
        review_rejected / cancelled / expired / failed_no_order  [terminal]
                                 |
                                 v
                           risk_reserved
                                 |
                                 v
                             submitted --------> placement_unknown
                                 |                      |
                    order_rejected [terminal]           |  (keeps its
                                 |                      |   reservation)
                                 v                      |
                             accepted                   |
                              |     |                   |
                              v     v                   |
                   partially_filled |                   |
                              |     |                   |
                              +-->  filled              |
                                     |                  |
                                     v                  |
                            protection_pending          |
                              |               |         |
                              v               v         |
                          protected   protection_failed  |
                              |               |          |
                              |     PAUSE ALL NEW LIVE ENTRIES
                              v               |          |
                           closing <----------+          |
                              |                          |
                              v                          |
                            closed  [terminal]           |
                                                         |
   Any non-terminal state --> reconciliation_required <---+
   and reconciliation resolves back into the lifecycle, or terminally.
```

`placement_unknown` is **not** terminal and does not release its reservation.
An unknown outcome is not a failure you can bank; it is resolved by
reconciliation proving no order exists or finding the order.

---

## 3. The tables

Eight, added by `0007_strategy_lab`, branching from
`0006_merge_research_workspace`. `source_observations` — the ninth table Spec Q
§8 names — already exists from Phase 3a and is reused, not duplicated.

| Table | Holds | Key constraint |
| --- | --- | --- |
| `strategy_versions` | Identity, rules, config, manifest + hash, replayability, status. | Unique `(slug, version)`. |
| `experiments` | The frozen pre-registration and the owner's decision fields. | Unique `name`; `planned_variants >= 1`. |
| `experiment_arms` | One strategy version in one **immutable** mode, with its risk budget. | Two partial unique indexes — see below. |
| `market_snapshots` | The point-in-time input bundle, scope, provenance, quality. | Unique `content_hash`; a CHECK that a `ticker` row has a ticker and a `universe` row does not. |
| `strategy_decisions` | One row per constituent — `long`, `flat` or `abstain`. | Unique `(arm_id, snapshot_id, ticker)`. |
| `strategy_trades` | The canonical execution intent in every mode, with `execution_id`. | Unique `execution_id`; one non-terminal execution per decision. |
| `experiment_metric_snapshots` | An arm's evaluation at one cutoff, with sample counts and warnings. | Unique `(arm_id, evaluation_cutoff_utc)`. |
| `promotion_events` | Append-only owner tier changes. | `source_arm_id <> target_arm_id`. |

Every foreign key points inside this group. Snapshot rows reference
`source_observations` ids as plain integers in a JSON column, and
`strategy_trades.execution_id` is the plain value `broker_orders.execution_id`
already carries. Phases 3c, 4 and P are landing in parallel, and a cross-phase
foreign key is what turns their integration merge revision from a no-op join
into a real migration (`migrations/README.md`).

### The two partial unique indexes

Both engines accept a `WHERE` clause on a unique index — SQLite since 3.8.0,
Postgres always — so these are the same DDL on both, given per dialect only
because SQLAlchemy namespaces the keyword.

```sql
CREATE UNIQUE INDEX uq_experiment_arms_single_live_champion
    ON experiment_arms (mode)
    WHERE mode = 'live' AND status = 'active';
```

Every row this indexes holds the same value in the indexed column, so at most
one can exist **in the whole table**: the one global live champion of Spec Q §3.
A unique index over a literal constant would say the same thing, but SQLite will
not index a constant expression, and a real column costs nothing.

```sql
CREATE UNIQUE INDEX uq_strategy_trades_open_execution
    ON strategy_trades (decision_id)
    WHERE status NOT IN ('cancelled','closed','expired','failed_no_order',
                         'order_rejected','owner_rejected','review_rejected',
                         'risk_rejected');
```

At most one non-terminal execution per decision (§12 invariant 6). It is written
as `NOT IN` the terminal states rather than `IN` the non-terminal ones so that a
state added to the enum without a migration lands on the *constrained* side and
fails closed; the inclusive spelling would have failed open.

`uq_experiment_arms_active` — unique `(experiment_id, strategy_version_id, mode)`
where `status = 'active'` — is what makes arm activation idempotent.

These are enforced by the database, not by the application, because Spec Q §12
requires it: an in-process check is invisible to a second worker and to a
process that restarted mid-flight. `strategy_lab/registry.py` checks them first
anyway, so the common case produces a sentence instead of a constraint name.
`tests/test_strategy_lab_registry.py::SchemaConstraintTests` proves them on
SQLite **and** Postgres in the same run.

---

## 4. Immutability, at the service boundary

`strategy_lab/registry.py` is the only module in the package that may hold a
database session. It refuses, with the diff of what changed:

* re-registering a `(slug, version)` whose rules, config, manifest or
  dependencies differ — "a rule change is a new version";
* editing a **registered** experiment's analysis plan (a `draft` one may still
  be revised, which is what draft means);
* recording a *different* decision for an `(arm, snapshot, ticker)` that already
  has one. Same version, same snapshot, different answer means the
  implementation changed underneath the version identity, which §6 requires to
  fail closed rather than overwrite the record.

Registering the *identical* thing again is a no-op that returns the stored row,
everywhere: versions, experiments, arms, snapshots, decisions, metric snapshots.

---

## 5. Promotion binds three things

A promotion is not a version reaching a tier. It binds:

1. the **source arm** whose evidence was read,
2. that arm's own **evidence snapshot** (`experiment_metric_snapshots`), whose
   warnings have been acknowledged by a named owner,
3. a **distinct, inactive target arm** carrying the **same immutable strategy
   version** and the requested higher mode.

Spec Q §8 is explicit that no strategy-version-only authorization is valid:
evidence collected by one arm may not authorize another arm's capital, and live
execution must match the exact promoted target arm id, version, mode and budget.
`domain.authorize_promotion` is pure and refuses each of those bindings
separately; `registry.record_promotion` supplies the rows, appends the event, and
activates the target — a live activation through `replace_live_champion`, which
deactivates the incumbent and activates the target in the caller's transaction,
so a failure anywhere leaves the prior champion exactly as it was.

Promotion is still **not** entry approval. Every proposed live execution gets its
own signed, expiring owner callback (§13); PR 5 and PR 6 build that. And nothing
in this package decides to promote anything — the system may recommend, the owner
decides (§3).

---

## 6. What cannot be imported

> No strategy module may import a broker, database session, Telegram client, or
> LLM client. It receives an immutable snapshot and returns a decision. — §5

That is a structural claim, so it is tested structurally, in
`tests/test_strategy_lab_import_graph.py`:

* the package may reach `strategy_lab`, `utils`, and — from `registry.py` and
  `snapshot_builder.py` alone — `database`. Nothing else, first-party or
  third-party;
* every strategy module reaches `strategy_lab` and nothing else first-party,
  and the pure SDK modules (`execution_policy`, `indicators`, `snapshots`,
  `universe`, `validation`) are named one by one so that deleting one from the
  package is a visible failure rather than a silently narrower guarantee;
* `domain.py` may reach **nothing** first-party at all, and a fresh interpreter
  importing it pulls in no SQLAlchemy and no model client;
* `config` is on the forbidden list. §12 invariant 11 requires execution mode to
  be an immutable input carried by the arm, and a module that can read a global
  setting can infer a mode from one.

The dependency runs one way: `database/models.py` imports the vocabulary from
`strategy_lab/domain.py`, never the reverse, so the CHECK constraints in the
schema and the enums a strategy validates against cannot drift apart.

---

## 7. Flags and configuration

**Two gates, both off.** PR 4 introduces them, together with the pipeline hook
that is the first thing they gate — see §21 for what each one turns on and
`docs/ENV_SETUP.md` §10 for the runbook. PR 1, PR 2 and PR 3 introduced no
environment variable at all: a flag that nothing reads is a flag nobody can
trust.

There is deliberately **no paper or live flag**. Spec Q §14 lists five, and the
three above shadow gate services that PR 5 and PR 6 own; adding them now would
mean shipping a switch whose "on" position does nothing, which is worse than an
absent one. `/live_kill` remains Phase 6's persistent database row (Spec L §6.5)
and is untouched.

Adding these tables changes no existing behaviour. `0007_strategy_lab` creates
eight empty tables and touches no existing one.

---

## 8. Verifying it

```bash
# Both engines, from empty and from the previous head, with the partial
# indexes proven on each.
python -m unittest tests.test_strategy_lab_migration

# The domain contracts (no database), the registry rules, and the boundary.
python -m unittest tests.test_strategy_lab_domain \
                   tests.test_strategy_lab_registry \
                   tests.test_strategy_lab_import_graph

# PR 2: the SDK. Policies against the one simulator, the snapshot rules, the
# roster's golden vectors, the four guards, and the database-backed builder.
python -m unittest tests.test_strategy_lab_execution_policy \
                   tests.test_strategy_lab_snapshots \
                   tests.test_strategy_lab_strategies \
                   tests.test_strategy_lab_validation \
                   tests.test_strategy_lab_snapshot_builder

# PR 3: replay against the one simulator, experiment registration and the
# idempotent multi-arm run, the decision/execution split, the measurement
# refusals, and the acceptance fixture with its byte-identical scorecards.
python -m unittest tests.test_strategy_lab_replay \
                   tests.test_strategy_lab_runner \
                   tests.test_strategy_lab_shadow \
                   tests.test_strategy_lab_metrics \
                   tests.test_strategy_lab_scoreboard

# How a given database will be classified before it is migrated. Read-only.
python -m scripts.schema_status sqlite:///copy-of-prod.db
```

With `TEST_POSTGRES_URL` set, the migration and constraint tests run against
SQLite *and* Postgres in a single run rather than waiting for the CI matrix to
cover the second engine. An invariant that holds on only one of the two engines
`DATABASE_URL` can select is not an invariant.

---

## 9. The snapshot: one cutoff, one object

A strategy sees exactly one thing: a `MarketSnapshot`. Building one is split
across two modules on purpose.

| Module | Holds a session | Job |
|---|---|---|
| `strategy_lab/snapshots.py` | no | States every point-in-time rule, and reads a snapshot back |
| `strategy_lab/snapshot_builder.py` | yes | Queries `price_bars`, `universe_membership`, `source_observations`, `scored_candidates` and `memos`, and hands the rows to the rules |

The split is what makes the rules testable with hand-written numbers instead of
a database, and the import-graph test holds it: `snapshots.py` cannot reach
`database` or `sqlalchemy` at all.

Three rules do the work.

**A future fact fails closed.** An input whose `known_at_utc` is after the
cutoff raises `NonPointInTimeInput`. It is not filtered out quietly: a builder
that hands over a future fact has a bug, and dropping it silently would hide the
bug while the number it would have changed goes unexplained. The same applies to
a bar whose session had not closed, to a membership row that could not have been
computed yet, and to a frozen composite result scored after the cutoff.

**Cross-sectional means one snapshot.** A universe-scoped snapshot carries the
whole constituent set, their prices, their exclusions and their provenance under
one cutoff. `momentum_v1` ranks against that one object, so ranks assembled from
ticker snapshots taken at different moments are not merely discouraged — there
is no code path that could produce them.

**Reconstructed is not point-in-time.** No price source in this repository
carries availability or revision provenance, so
`snapshot_builder.REPLAY_ELIGIBLE_PRICE_SOURCES` is **empty** and every snapshot
built from stored bars is `archival_reconstructed`, `replay_eligible=False`, and
exploratory. That is the honest state of the price plane (§6 of the spec says so
directly), not a defect to route around: the way to change it is to land a
licensed archival source with availability/revision provenance, or to
forward-collect the observations, and add that source's name to the set.

The bitemporal ledger is **not** rebuilt here. `source_observations` already
exists, written through `filings/observations.py`; snapshots reference those
rows' ids, and nothing in `strategy_lab/` writes an observation.

---

## 10. One execution-policy contract, one simulator

`backtest/simulator.py` is the only simulator: T+1-open entry, adverse slippage
on every fill, gap-through fills at the worse open, pessimistic same-bar
stop-before-target resolution, half out at target 1 with the stop unchanged on
the remainder, and a calendar-day time exit at the close of the first bar on or
after `entry_date + max_holding_days`.

Spec N's cohort engine already drives it through
`comparables/outcomes.py::PolicySpec` — `(slug, stop_frac, target1_frac,
target2_frac, max_holding_days, direction)`, fractions of the entry reference.
`strategy_lab/execution_policy.py` produces **exactly those constructor
keywords** from `ResolvedExecutionPlan.policy_spec_fields()`, and
`tests/test_strategy_lab_execution_policy.py` builds a real `PolicySpec` from
them and asserts the simulator returns the identical trade. Spec N and Spec Q
share one execution-policy contract; the import stays in the test because
`strategy_lab/` does not reach `comparables` or `backtest`.

| Policy | Entry | Stop | Targets | Max hold | Slippage |
|---|---|---|---|---|---|
| `event_swing_14cal_v1` | first open after the signal is tradable | entry − 2.0 × ATR(14) | +2R (half), +3R | 14 calendar days | 10 bps |
| `reversal_5cal_v1` | next open after the signal close | entry − 1.5 × ATR(14) | +1R (half); remainder keeps the stop and times out | 5 calendar days | 10 bps, stressed at 25 and 50 |
| `momentum_quarterly_89cal_v1` | next open after a quarter-end session | entry − 10% | none | 89 calendar days | 10 bps |
| `swingtrader_memo_trade_params_v1` | the memo's limit entry | whatever the pipeline computed | whatever the pipeline computed | the memo's `max_hold_days` | 10 bps |

Two of them anchor the stop to ATR multiples of the **filled** entry, which does
not exist at decision time. So a decision carries the policy *version*, the
entry style and the maximum hold, and the arithmetic resolves against the entry
bar's open at execution or replay time — the same moment
`comparables/outcomes.py::_replay` resolves it. The compatibility policy is the
exception: its prices were computed by the pipeline and are frozen, so they
travel on the decision as absolute numbers.

---

## 11. The roster

| Slug | Scope | Policy | Replayable | Tier |
|---|---|---|---|---|
| `swingtrader_composite_v1` | ticker | `swingtrader_memo_trade_params_v1` | **no** | champion |
| `earnings_drift_v1` | ticker | `event_swing_14cal_v1` | yes | challenger |
| `momentum_v1` | universe | `momentum_quarterly_89cal_v1` | yes | challenger |
| `short_term_reversal_v1` | universe | `reversal_5cal_v1` | yes | **shadow-only** |

Every threshold comes from the normative V1 reference configuration in Spec Q §7
and is declared in the version's immutable `config`, alongside the citation it
came from. Where a rule the spec does not fix had to be decided — how a surprise
maps into a [0, 1] signal strength, when a newly-landed earnings record fires,
how the top decile is counted — the choice is listed under `assumptions` in the
same config and labelled a project assumption rather than presented as a finding.

`short_term_reversal_v1` is universe-scoped because its cap — "the 10 largest
absolute three-session declines per signal date" — is a cross-sectional rule; it
cannot be evaluated one ticker at a time without mixing cutoffs.

`swingtrader_composite_v1` maps the pipeline's already-produced output and calls
no model. It maps `cohort == "memo"` with a long direction to `long`, because
that is the band in which the current system produces a memo and asks Bryan to
approve a trade (`tracking/shadow_ledger.py::classify_cohort`); it re-derives no
threshold of its own.

---

## 12. The four guards

`strategy_lab/validation.py`, all pure, all callable without a database.

**Decision-set validity.** A ticker strategy returns one draft for the
snapshot's ticker; a universe strategy returns one for *every* constituent —
`long` for the selected, `flat` for the eligible-but-unselected, `abstain` for
the unevaluable — ordered by ticker ascending, no repeats, each pinned to this
snapshot's content hash and to this version's execution policy. PR 3's runner
calls it before persistence; every strategy here already calls it before
returning.

**Implementation-manifest drift.** The manifest covers the strategy's own
source, every output-affecting helper in the package, the execution policy's
source *and* its resolved configuration, each indicator formula's source, and
the pinned runtime. `registry.verify_registered_manifest` recomputes it and
compares against `strategy_versions.implementation_manifest_hash`;
`registry.activate_strategy_version` runs the same check before moving a status.
A mismatch raises `ManifestDrift` and requires a new version. Editing
`indicators.py` therefore changes the manifest of every strategy that uses it —
that is the intended blast radius, because the formula is part of what the
version *is*.

The Python version is recorded to minor precision: a patch bump under a
deployment should not invalidate every registered version, and a minor-version
move is a real runtime change that should.

**Historical replayability.** `guard_historical_replay` refuses a version that
declares `historically_replayable=False` — the composite, always — and refuses
any snapshot carrying `archival_reconstructed` or `not_point_in_time`. Those
results are exploratory and never enter clean metrics or satisfy a promotion
gate (§10).

**Structural shadow-only.** `require_mode_allowed` refuses a paper or live arm
for a version whose immutable config declares `shadow_only`. Lifting it is a new
version with its own evidence, not an edit.

---

## 13. Abstain, flat, and never impute

Spec Q's rule is that a strategy "must never guess missing values", and the
three-way outcome is how that is kept visible:

* **`abstain`** — the strategy could not evaluate this name. No bars, a stale
  series, a missing dependency, no ATR to anchor a stop to, a formation window
  that does not reach back far enough. It carries one of the three Spec Q §6
  blocked reasons.
* **`flat`** — the strategy evaluated this name and did not select it. The
  liquidity screen failed, the surprise was 4%, the name was outside the top
  decile, today is not a rebalance session. A considered decision not to trade.
* **`long`** — with a signal strength, a risk plan and the reason codes that got
  it there.

Collapsing the first two would let a data outage look like a considered decision
not to trade, and that is the single most expensive thing an evidence system can
get wrong about itself.

Every indicator in `strategy_lab/indicators.py` returns `None` rather than
padding a short window, which is what makes the abstention automatic rather than
remembered.

---

## 14. What PR 2 did not do

* Nothing is wired into `orchestrator/pipeline.py`. No scheduled job builds a
  snapshot, no arm runs, no decision is persisted by anything but a test.
* No flag is added, because nothing reads one yet (§7 still holds).
* No broker is reachable, no order is proposed, and no notional limit exists to
  change.
* No table is added. PR 1's eight are enough; snapshots record through
  `registry.record_snapshot`, which already existed.
* No promotion-eligible replay is possible yet, and the code says so out loud
  rather than producing a number that looks clean (§9).

---

## 15. The run: registration, then N arms over one snapshot

`strategy_lab/runner.py`. Two jobs, and the order between them is the point.

**Registration freezes the question.** `register_experiment` stores the
pre-registration — hypothesis, primary metric, guardrails, universe, benchmarks,
data cutoff, end criteria, planned variant count — and creates one arm per
strategy version and mode *before* a single decision exists. Four refusals fire
before any arm row is written: an unknown strategy, a version that is not the
one on disk, a structurally shadow-only version asked for a paper or live tier,
and **more arms than the experiment pre-registered variants for**. That last one
matters because `planned_variants` is the multiple-testing denominator: raising
it after seeing results is how a tournament launders luck into evidence.

**A run is idempotent, and a failure is one arm's.** `run_snapshot` records the
snapshot once and evaluates every arm against it, so two arms of one rebalance
reference one `market_snapshots` row. Each arm is guarded independently —
manifest drift, a forbidden tier, a refused historical replay, a scope
mismatch — and its refusal is recorded on that arm's result rather than aborting
the run. An experiment in which one broken arm quietly removes itself is worse
than one that says which arm broke.

Re-running writes nothing: `registry.record_decision` returns the stored row for
an identical decision and refuses a *different* one for the same
`(arm, snapshot, ticker)`. `long`, `flat` and `abstain` are all persisted, one
row per constituent for a universe arm.

The runner opens no session. It takes one and hands every write to
`registry.py`, which stays the only module in the package importing `database` —
`tests/test_strategy_lab_import_graph.py` now asserts that about `runner.py` and
`shadow.py` by name.

### Every variant tried

`runner.variant_ledger` counts every `(strategy version, mode)` ever created
under the experiment — retired and paused arms included — against the
pre-registered count, and the scorecard uses the larger of the two as the
multiple-testing denominator. Running more variants than were declared is a
warning code on the card (`variants_run_beyond_preregistration`), not a silent
adjustment.

---

## 16. Replay: one simulator, three refusals

`strategy_lab/replay.py` is the **only** module in the package that imports
`backtest`, and the import-graph test enforces it. It owns no exit logic: it
resolves the execution policy against the T+1 entry reference, expands
`ResolvedExecutionPlan.policy_spec_fields()` into absolute prices exactly the way
`comparables/outcomes.py::_replay` does, and calls `simulate_trade`.
`tests/test_strategy_lab_replay.py` builds a real `comparables.outcomes.PolicySpec`
from the same dict and asserts the identical trade, entry, exit, rule and P&L —
so "the Strategy Lab and the cohort engine fill the same way" is checked rather
than claimed.

Three refusals fire before a bar is touched:

1. `historically_replayable=False` — the compatibility arm freezes an LLM's
   conclusion, which is not reconstructible at a historical T (§7A).
2. A snapshot carrying `not_point_in_time` or `archival_reconstructed` cannot
   produce clean evidence. It may still be replayed *as an exploratory report*,
   which `classify_evidence` labels and `require_clean_replay` refuses.
3. An observation whose `known_at_utc` is after the decision cutoff is
   **rejected**, not filtered: `reject_after_cutoff` raises naming it, because a
   replay that quietly drops a look-ahead fact reports a smaller sample and
   calls it clean. `resolve_as_of` is the deliberate filter — one fact per
   `(ticker, fact_type, valid_at)`, the greatest `known_at_utc <= cutoff` — so a
   2026 restatement does not leak into a 2024 replay.

**There is no promotion-eligible replay in this deployment.**
`snapshot_builder.REPLAY_ELIGIBLE_PRICE_SOURCES` is empty, so every snapshot
built from stored bars is `archival_reconstructed`. The scorecard says so in its
own section header. Nothing routes around it.

### Maturity

`ReplayOutcome.matured` is false when the bars ran out before
`entry_date + max_holding_days` and no stop or target fired: the position is
**open**, not flat at zero. Spec Q §9 forbids ranking a long-horizon strategy
against a five-day one on incomplete positions, and this is the field that keeps
that promise. `metrics.py` counts open observations, names their tickers, and
excludes them from every statistic.

Costs are explicit or absent. `CostAssumptions(slippage_bps, half_spread_bps,
commission_bps)` has no default constructor: a replay without one produces a
gross number that the measurement layer refuses to score.

---

## 17. Shadow: the decision/execution split

`strategy_lab/shadow.py` refuses any arm whose mode is not `shadow` — a paper
arm belongs to the Alpaca adapter (PR 6), a live arm to the promoted broker path
(PR 5) — and the package imports no broker at all, so "sends no order" is
structural rather than conditional.

The split it exists to make:

| | Reproducible from | Recomputed |
|---|---|---|
| `strategy_decisions` | the snapshot alone | never |
| `strategy_trades` | a fresh `PortfolioContext`, hashed onto the row | every attempt |

So the same decision is executed on Monday and refused on Tuesday because the
portfolio changed, and neither outcome mutates or duplicates the decision.
`tests/test_strategy_lab_shadow.py` asserts exactly that: two attempts, two
context hashes, one unchanged `strategy_decisions` row, two `strategy_trades`
rows.

An execution is identified by `(decision, portfolio_context_hash)`, so a whole
shadow run re-runs idempotently; on top of that a decision has at most one
non-terminal execution, held by the partial unique index, so a retry against a
*different* context reuses the open row rather than creating a second placement.

Sizing is arithmetic, never a choice: `risk_dollars = equity × arm_risk_budget ×
position_risk_pct`, notional is that over the stop distance, and every cap that
bound the result is named on the row (`max_position_fraction`,
`max_daily_notional`). Percentage returns and R are what the scorecard ranks on,
precisely because they do not depend on the virtual budget an arm happened to
get.

A shadow fill walks the same §12 state machine live will —
`proposed → owner_approved → risk_reserved → submitted → accepted → filled →
protection_pending → protected → closing → closed` — with the lab standing in
for owner, risk desk and broker, and every hop checked against
`domain.EXECUTION_TRANSITIONS`. That is deliberate: PR 5's machine should be
exercised by months of shadow evidence rather than met for the first time with
money on it. `mode` says `shadow` on every one of those rows.

---

## 18. Measurement, and what it refuses to say

`strategy_lab/metrics.py` is pure stdlib. It computes net after explicit costs,
gross beside it, mean and median R, win rate, profit factor, maximum drawdown
and time under water, exposure, turnover, benchmark-relative return, and the
overlap and correlation between every pair of arms — all with `n` and warning
codes attached.

**The bootstrap is not reimplemented here.** Spec N's `comparables/inference.py`
is this repository's one implementation of the stationary block bootstrap, the
effective sample size and Romano–Wolf step-M, and `strategy_lab/` may not import
`comparables`. So `metrics.py` declares two protocols and
`scripts/strategy_lab_scoreboard.py` implements them over `comparables.inference`.
A run without them prints `uncertainty_unavailable` and refuses to name a winner
rather than falling back to a normal approximation.

The refusals, in the order they fire:

| Condition | Result |
|---|---|
| Clean and reconstructed evidence in one set | raises `MixedEvidence` |
| A matured trade with no cost model | `blocked` — **no return metric at all** |
| Bars ran out before the horizon | counted, named, excluded |
| Below a floor | computed and shown *with n*, status `insufficient_evidence` |

Floors default to Spec Q §10's own operational minimums (100 matured, 20
distinct dates, 30 closed, 60 shadow days), are configurable, and are printed
beside every result.

**Chronological only.** `chronological_folds` produces contiguous walk-forward
splits and purges from training every observation whose label window reaches
into the validation block or its embargo. There is no random-shuffle option,
because that is the mistake the function exists to prevent. `reserve_holdout`
cuts the most recent slice off by entry date, and `evaluate_arm` **raises** if a
development evaluation touches it — unsealing is an explicit argument someone
types once, at the end, on purpose.

### When the word "winner" may be used

`rank_arms` always produces the ordering — hiding it would be its own kind of
dishonesty — but `winner` is `None` and the label is `insufficient_evidence`
unless *all* of these hold:

- the evidence is clean rather than reconstructed;
- no arm is blocked and every arm clears its floors;
- the leader has an uncertainty interval and a multiplicity-adjusted one;
- the adjusted lower bound is above zero;
- the leader beats its benchmark;
- the adjusted lower bound clears the runner-up's point estimate;
- Romano–Wolf step-M rejects for the leader across the family.

The gate's conservatism is the project's, not a citation, and it is printed on
every card. It gates a *recommendation*: promotion is owner-only (§3, §8), and
nothing in this package performs one.

The multiplicity adjustment is Šidák written for a confidence level rather than
a p-value — each interval taken at `level ** (1 / m)` for `m` trials — and
`tests/test_strategy_lab_metrics.py` asserts the exact round trip against
`inference.sidak_adjusted` rather than trusting the algebra.

---

## 19. The scorecard CLI

```bash
python -m scripts.strategy_lab_scoreboard \
    --database-url sqlite:///swing_trader.db \
    --experiment q1_2026_roster \
    --cutoff 2026-06-30T21:00:00 \
    --json artifacts/scoreboard.json \
    --markdown artifacts/scoreboard.md \
    --slippage-bps 10 --half-spread-bps 5 \
    --reps 2000 --seed 20260908
```

Read-only: it places no order, writes no experiment row and performs no
promotion. The same database, cutoff and options produce **byte-identical**
files — every collection is sorted, every float goes through one formatter, the
seed is an explicit option printed on the card, and the only clock in the
artifact is the cutoff the caller passed. A card that changed because it was
generated twice would be worthless as evidence.

The card carries, in this order: the inputs (floors, gate, cost assumptions),
every variant tried against the pre-registered count, a **clean** section and an
**exploratory `archival_reconstructed`** section that are never combined, each
with its arm table, uncertainty intervals with their seed and replication count,
the pairwise overlap and correlation matrix, and a verdict with the reasons it
is what it is. Then the arms that produced nothing and why, and the warning
codes.

---

## 20. What PR 3 does not do

* Nothing is wired into `orchestrator/pipeline.py`, no scheduler job exists, and
  no flag is added — PR 4 owns the hook and the flags that gate it.
* No broker, paper or live, is reachable. `shadow.py` refuses a non-shadow arm
  outright; PR 5 and PR 6 own those paths.
* No promotion is performed or recommended as an authorisation. A cleared gate
  is a reason to look at the evidence, and `promotion.py` remains PR 6's.
* No table is added. PR 1's eight are still enough: executions are
  `strategy_trades` rows and evaluations are `experiment_metric_snapshots`.
* **No promotion-eligible replay is possible yet** (§16). Every scorecard this
  deployment can produce today is exploratory, and it says so.
* Probability-of-backtest-overfitting (CSCV) is **deferred**. The
  multiple-testing control that ships is Šidák-adjusted intervals plus
  Romano–Wolf step-M, both from `comparables/inference.py`; a CSCV
  implementation is its own PR against Spec N's inference layer rather than a
  second, differently-shaped copy inside `strategy_lab/`.

---

## 21. PR 4: the shadow hook, the flags, and the operator surface

### The hook is one call, at the end

`orchestrator/pipeline.py` gains a single call at the end of
`_run_full_scan_inner`, *after* every memo has been generated and delivered and
every notification sent. The whole integration lives in
`orchestrator/strategy_lab_shadow.py`, and the hook is nine lines: a flag check,
a deferred import, one call, and an `except Exception` that logs.

Two guards, not one. The module catches its own exceptions and returns a summary
carrying them; the hook catches whatever that missed, including the `ImportError`
a half-deployed container produces. Spec Q §14 says Strategy Lab writes are
best-effort and must not break memo generation, and the ordering is what makes
that cheap to believe: by the time the hook runs there is nothing left to break.

```
scan → tier 1 → tier 2 → regime → discovery → per-ticker analysis
     → memos delivered → paper auto-approve → notifications
     → [flag] Strategy Lab shadow pass
```

### What one pass does

One **ticker** snapshot per scored name, and — behind its own flag — one
**universe** snapshot for the whole cutoff, shared by every cross-sectional arm.
That second part is Spec Q §6's rule and PR 4's requirement 8: ranks assembled
from snapshots built at different times are not a cross-section, so the universe
snapshot is built once per cutoff and never per ticker.

The arms come from `runner.run_snapshot`, unchanged. Re-running the same cutoff
writes nothing: `registry.record_snapshot` returns the stored row for identical
content and `record_decision` returns the stored decision, so a duplicate scan,
a retry and a restart all converge.

### Registration is idempotent, and the plan is a constant

The pre-registration — hypothesis, primary metric, benchmarks, guardrails, end
criteria, planned variant count — is a literal in
`orchestrator/strategy_lab_shadow.py`, not a setting. An analysis plan a
deployment variable can move is not a pre-registration. `ensure_experiment` runs
at the start of every pass and is a no-op once the rows exist; it re-asserts the
status the experiment is already in rather than the status a first registration
would take, because `running -> registered` is not a legal transition and asking
for it would make the second scan of the day fail on the experiment the first
scan started.

`PLANNED_VARIANTS` is the literal `4` rather than `len(ROSTER)`. It is the
multiple-testing denominator (§18), and a denominator that grows silently when
someone adds a strategy is how a tournament launders luck into evidence. Adding
an arm is a visible edit that changes the experiment's content hash and forces a
new `STRATEGY_LAB_EXPERIMENT` name — which is the freeze working, not a problem
to route around.

### Maturation: decisions today, executions later

A shadow decision cannot be executed on the day it is made. A forward simulation
needs the sessions that came *after* the snapshot, and at scan time there are
none. So the scan records decisions and a nightly job (04:15 ET, registered only
when both flags are on) opens and settles the executions once the bars exist,
through `shadow.execute_arm` — the same §12 state machine PR 5's live path walks.

A decision whose bars never arrive stays a decision with no execution. It is
neither a win, a loss, nor a zero, and the scorecard counts it as pending.

### Two integration defects this PR found and fixed

Both were in `strategy_lab/snapshot_builder.py`'s compatibility adapter, both
were invisible to PR 2's unit tests because those tests wrote fixture rows rather
than rows the pipeline produces, and each on its own made the champion arm
abstain or go flat on **every** real scan:

* **Vocabulary.** The pipeline records a *view* — `bullish`, `bearish`,
  `neutral` — and the Strategy Lab records a *side*. `swingtrader_composite_v1`
  tests `direction == "long"`, a word the pipeline never writes. The builder now
  translates, which is exactly the adapter's job.
* **Ordering.** The pipeline generates the memo and *then* writes the ledger row,
  so the builder's "memo at or after `scored_at`" filter never matched the memo
  of the same scan, the frozen trade parameters were always absent, and the arm
  abstained with `missing_dependency`. The two rows are now paired within a named
  two-hour window — wider than any scan, far narrower than the five hours between
  the three daily scans — with the resolved memo id on `model_provenance` so a
  reader can check the pairing. When `memos` gains a run id, that join replaces
  the window rather than widening it.

### The operator surface

`bot/handlers/strategy_lab.py`, owner-only through the same chat-id allowlist
every other command uses. `/experiments`, `/strategies`, `/strategy <slug>`,
`/pause_experiment [name]`, `/resume_experiment [name]`, plus a weekly-report
section.

Pausing an experiment stops the *work*, not only the reporting: a paused
experiment is not in `registry.RUNNABLE_EXPERIMENT_STATUSES`, so every arm under
it refuses at the runner and the shadow pass writes nothing.

**No number on the card is produced here.** Counts are `len()` over stored rows.
Every performance figure — samples, maturity, costs, drawdown, benchmark,
uncertainty with its seed and `n_eff`, the family adjustment, correlation and
overlap, and every warning code — is read out of the payload
`scripts/strategy_lab_scoreboard.py` built over `metrics.py` and
`comparables/inference.py`. `tests/test_strategy_lab_bot.py` holds that
literally: it extracts every float in the rendered message and asserts each one
appears in the payload, so a renderer that derived a ratio or a sum would fail.

The clean and exploratory sections stay separate in the message because they are
separate in the payload, a winner is named only when the payload names one, and
the card says in as many words that a recommendation is not an authorisation.

### What PR 4 does not do

* **No promote and no live-tier command.** Spec Q §13 lists `/promote_arm` and
  the per-trade approval callback; their safety services are PR 5 and PR 6. An
  owner-only button that calls a promotion path which does not exist yet is worse
  than no button. `tests/test_strategy_lab_bot.py` asserts the registered surface
  is exactly the five commands above plus Phase 6's untouched `/live_kill`.
* **No broker, under any path.** The tests run every failure path with a broker
  double that raises on *any* attribute access — not just on an order method —
  and the scan and the shadow pass both complete without touching it.
* **No new table and no new migration.** PR 1's eight tables are still enough;
  the head stays where it was.
* **No existing scheduled job moves.** `tests/test_strategy_lab_scheduler.py`
  pins every current job id and cron time, on both sides of the flag.
* **No paper arm and no ensemble.** Every arm this creates is `shadow`, its mode
  fixed at creation.

---

## 22. The execution machine (PR 5, Spec Q §12)

PR 5 makes a Strategy Lab arm able to execute — on Phase 6's service, not beside
it. Phase 6 already owns signed single-use approval, fresh risk re-evaluation,
the kill switch, entry placement, fill polling, the `gtc` `stop_market` with its
read-back, `unprotected` paging, and the daily stop replacement. None of that is
reimplemented. What PR 5 adds is the persistent §12 machine those steps land on,
and the gates that decide whether a step may be taken at all.

**Nothing here is enabled.** No flag is added, `PHASE6_EXECUTION_ENABLED` stays
off, and no order has been placed against a real broker by any of it.

### The split, and why it is where it is

| Concern | Module | May reach |
|---|---|---|
| the §12 rules: mode→venue, the reserving/blocking state sets, redaction | `strategy_lab/execution.py` | `strategy_lab`, `utils` — **pure** |
| every `strategy_trades` write, PR 3's and PR 5's alike | `strategy_lab/registry.py` | the above plus `database` |
| gates, the observer, resume, reconcile | `execution/strategy_lifecycle.py` | the above plus `execution/`, `portfolio/` |
| placement, protection, the read-back | `execution/lifecycle.py` (Phase 6) | the broker |
| does the broker agree with the ledger | `tracking/position_reconciliation.py` | `database`, `strategy_lab.domain` |

The first row is the load-bearing one. Spec Q §12 invariant 11 says execution
mode is an explicit immutable input, never inferred from global mutable
settings — so the module that decides *which venue a mode may reach* is the one
module that cannot read a setting. `import config` fails there, by test
(`tests/test_strategy_lab_import_graph.py`), and so do `database`, `execution`
and `portfolio`: it is in `PURE_SDK_MODULES` alongside the strategy SDK. The
rules cannot be talked out of themselves.

The second row matters for a different reason. PR 3 made `registry.py` the one
writer of `strategy_trades` — `record_execution`, `set_execution_state`,
`open_execution_for` — and PR 5 adds `open_execution` (reuse-on-race),
`advance_execution` (the timestamps and the release log), `reserved_notional`,
`blocking_executions` and `resumable_executions` **beside** them rather than
opening a second writer. Two writers for one table is how two workers end up
with two placements.

### One execution per decision, and one id before anything

`open_execution` writes an `execution_id` and commits it **before** any
reservation and long before any broker call. Uniqueness is held by the partial
index `uq_strategy_trades_open_execution` — a database constraint, so a second
worker and a restart both see it. A caller that loses the race is handed the
winner's row inside a SAVEPOINT rather than an `IntegrityError`, which is what
makes a retried approval reuse one placement instead of creating a second.

The index is partial on `status NOT IN (terminal)`, so a cancelled or rejected
execution frees its decision at the *database* level while a live one never does.
A second rule sits on top of it, from PR 3 and inherited deliberately: an attempt
is identified by `(decision, portfolio_context_hash)`, so re-proposing a decision
the owner just cancelled — against a book that has not moved — resolves to the
cancelled row and places nothing, rather than minting a second card. A genuinely
different portfolio context is a new attempt, which is how "the same decision was
blocked on Tuesday" stays answerable (§17 makes the same split for shadow).

### The reservation is a predicate, not a counter

Spec Q §12 requires every terminal path to release its reservation *exactly
once*. Rather than a released-at column and the discipline to write it once,
a reservation is membership in `RESERVING_EXECUTION_STATES`: a row holds its
notional from `risk_reserved` until it reaches a terminal state, and terminal
states have no outgoing edge. Release is therefore the terminal transition
itself — there is no second write to forget, repeat, or disagree with the
status.

`placement_unknown` and `reconciliation_required` are inside the reserving set
on purpose (invariant 12): an unknown outcome might be a real order, and an
order that might exist has not released anything.

### What blocks a new entry

`portfolio.killswitch.entry_block` gained a third **reason**, not a second
switch:

1. `kill_switch_engaged` — the owner pulled it; a database row, survives restart.
2. `unprotected_position_blocks_entries` — a Phase 6 proposal is `unprotected`
   or `reconciliation_required`.
3. `unresolved_execution_blocks_entries` — a `strategy_trades` row is
   `protection_failed`, `placement_unknown`, or `reconciliation_required`.

`protection_pending` is deliberately not blocking: it is the few seconds between
a fill and its stop being read back, and the window's own deadline resolves it
either way. A row still in `protection_pending` after a restart is moved to
`protection_failed` by the resume pass, which is what makes that distinction
safe rather than a loophole.

### Mode → venue → adapter

```text
shadow  ->  (no venue at all; bind_adapter raises)
paper   ->  alpaca_paper    ->  AlpacaBroker.venue == "alpaca_paper"
live    ->  robinhood_live  ->  RobinhoodMCPBroker.venue == "robinhood_live"
```

Three checks, in order: the arm's recorded mode must equal the requested one;
the venue must be the single one that mode may select; and the adapter's **own**
`venue` declaration must not contradict it. The third is what catches the wiring
error the first two cannot — the live adapter registered under the paper key.
An adapter that declares nothing is accepted; one that declares something else
never is.

### What a live arm needs beyond the flags

`PHASE6_EXECUTION_ENABLED`, `ALLOW_LIVE_TRADING=true` and `EXECUTION_MODE=live`
are necessary and nowhere near sufficient. On top of them (§12 invariant 2), at
propose time and before a card is minted:

* the arm is `active`;
* the arm **is** `registry.active_live_arm` — the single global champion;
* the arm carries at least one owner `promotion_event`, itself already bound to
  a source arm, its evidence snapshot, the shared immutable strategy version,
  the mode, and the approved budget when it was recorded;
* the adapter's declared capabilities pass `gate_intent` with
  `requires_protective_exit=True`.

Any one missing is a terminal `risk_rejected` with the reason on the row, and
the safety suite asserts zero order calls for each.

### The cases where a live entry is impossible, and stays impossible

Spec Q §12's closing paragraph: if Robinhood cannot provide a verifiable
protective exit for a case, that case cannot be opened live. No manual-exit
fallback, and no in-process watcher — a watcher disappears with the process,
which is the failure it would be pretending to prevent.

| Case | What refuses it | Where |
|---|---|---|
| the adapter stops declaring `can_place_standalone_gtc_stop` | `gate_intent`, before any order is formed | `portfolio/capabilities.py` |
| the adapter declares no capabilities at all | treated as "cannot protect" | `_capability_refusal` |
| a fractional quantity, or a `dollar_amount` entry | `stops_whole_shares_only` | `gate_intent` |
| an extended-hours entry | `stops_regular_hours_only` | `gate_intent` |
| a short entry | there is no protective-stop shape for one | `ck_proposals_side_long_only` |
| a stop at or above the entry | per-share risk ≤ 0 | `portfolio/proposals.py` |
| a fill whose stop cannot be read back | `protection_failed`, pages, blocks every entry | `_protect` |
| a stop that later vanishes | re-placed daily; unverifiable ⇒ `unprotected` | `replace_missing_stops` |

None of these is a flag. Turning a live entry back on for one of them means
changing a declaration that a test asserts, which is the intended cost.

### Restart, and the two rules the resume pass never breaks

`StrategyExecutionService.resume()` re-derives every non-terminal *paper or live*
execution from the broker's current answer, because after a restart there is no
other source of truth.

Shadow rows are skipped, and not as an optimisation. §17's shadow executor walks
this same machine, so a simulated position sits at `protected` — non-terminal —
until it matures; it is resumed by re-running the shadow executor, never from a
broker, and asking for an adapter for it is `ShadowReachedExecution` by design.
For the same reason `killswitch.blocking_executions` excludes `shadow`: a
simulated position must never block real capital, and §12 invariant 3 is about
existing *live* trades.

* **No entry order is ever placed by resume.** A protective stop may be
  re-placed — that is the whole point of surviving a restart with an unprotected
  fill — but an entry never is. An execution whose entry cannot be found
  resolves to `failed_no_order` or to `reconciliation_required`, never to a
  re-submission.
* **A read that fails is not an answer.** Any exception leaves the row where it
  was or moves it to `reconciliation_required`. "I could not check" and "it is
  fine" are never the same answer.

`placement_unknown` leaves only through `reconciliation_required`, exactly as
§12 draws it, holding its reservation the whole way; from there the broker's
answer to "does an order carrying this ref_id exist" resolves it into the
lifecycle or terminally.

### Partial fills, and resizing protection

A partial fill is protected at what actually filled. When the remainder arrives
later, the stop covers less than the position — and nothing detects that without
asking the broker again, which is what `resume` does. The resize:

1. cancels the undersized stop **first** (two live sell orders against one
   position is not protection, it is a short);
2. asks for a new `ref_id`, keyed to the quantity being protected, so
   re-protecting an unchanged fill reuses one stop and a grown position gets its
   own;
3. routes `protected → reconciliation_required → filled → protection_pending →
   protected`, because §12 draws no edge from `protected` back to
   `protection_pending` and a position whose stop no longer covers it is
   precisely "broker and local state disagree".

### Reconciliation

`tracking.position_reconciliation.reconcile_executions` runs the comparison the
legacy pass never did — *this execution believes it holds a position, does the
broker agree?* — and returns `matched`, `missing_at_broker`, `quantity_mismatch`,
`unexpected_at_broker`, or `unsupported`. It transitions nothing; the bridge
applies `reconciliation_required` and pages with a recovery instruction. An
execution it cannot evaluate is `unsupported` and fails closed.

### Redaction

`strategy_lab.execution.redact` is an allowlist, not a denylist: ids,
idempotency keys, states, reasons, and the numbers reconciliation needs survive;
every other key is kept with its **value** replaced by `[redacted]`, so a
reviewer can see that a token was present without seeing it. A denylist's
failure mode is a field nobody thought of, arriving from a payload shape we do
not control.

---

## 23. PR 6: the paper tournament

PR 5 built the execution machine and gave it an explicit-mode entry point. PR 6
is what calls it for a paper arm, and the call site is one module:
`orchestrator/strategy_lab_paper.py`.

```text
scan (orchestrator/pipeline.py)
  |
  +-- memos delivered, notifications sent        <- production work, first
  |
  +-- _run_strategy_lab_shadow      STRATEGY_LAB_SHADOW_ENABLED
  |     snapshot -> every ACTIVE arm of every ENABLED tier -> strategy_decisions
  |
  \-- _run_strategy_lab_paper       STRATEGY_LAB_PAPER_ENABLED
        for each active paper arm:
          decisions with no strategy_trades row, action=long, plan resolvable
            |
            +-- snapshot older than PAPER_MAX_SNAPSHOT_AGE_MINUTES --> abstain
            +-- ticker reserved by another paper arm                --> skip
            +-- shadow.assess against the arm's VIRTUAL paper book  --> skip/size
            |
            \-- StrategyExecutionService.propose(mode=paper, tag=lab:.../arm:N)
                  |
                  +-- bind_adapter: paper -> alpaca_paper, one adapter, or refuse
                  +-- kill switch, capabilities, tier flag
                  \-- Phase 6 create_proposal -> a `proposed` row + an approval card

        NOTHING IS PLACED HERE. The card carries a signed, expiring, single-use
        owner reference; `on_approval` is where an order happens.
```

### The decision pass runs every enabled tier, the maturation pass does not

A `StrategyDecision` is a function of the snapshot and the immutable strategy
version alone (§9) — it carries no portfolio state and no mode — so a paper arm
needs decisions for exactly the same reason a shadow arm does, from the same
snapshot. `orchestrator.strategy_lab_shadow.active_decision_arms` is therefore
the arms of every *enabled* tier, and a tier whose flag is off contributes none:
turning `paper` off stops the decisions as well as the dispatch, so the arm stops
producing evidence rather than producing evidence nobody can act on.

`active_shadow_arms` stays shadow-only, and that is not an oversight.
`mature_shadow_decisions` simulates a fill from stored bars. That is the right
thing to do to a hypothetical position and the wrong thing to do to one that
exists at a broker; a paper arm's executions are settled by the §12 state machine
against the broker's own answer.

### Independent virtual budgets

Spec Q §11: "shadow and paper arms receive independent virtual budgets." The
paper book is its own settings family (`STRATEGY_LAB_PAPER_EQUITY`,
`_RISK_BUDGET`, `_MAX_OPEN_POSITIONS`, `_MAX_POSITION_FRACTION`,
`_DAILY_NOTIONAL`) and the sizing runs through PR 3's pure
`strategy_lab.shadow.assess` / `size_position`, which is where the
max-open-positions, position-fraction, daily-notional and ticker-already-held
rules already live. Reusing them means the paper tier's caps are the ones months
of shadow evidence were produced under.

Phase 6 then applies its **own** caps — the risk-fraction hard cap, the budget's
per-trade cap, concentration, sector, daily notional, settled cash — over the
real ledger, and those can only make an order *smaller*. So the honest
description of a paper arm's size is: the virtual book decides it, and the
production book is a ceiling.

**What that leaves open, stated plainly.** `create_proposal` reads the ledger's
`agent_placeable` account for equity, concentration and settled cash, which on a
configured deployment is the Robinhood Agentic account — not the Alpaca paper
account the order actually reaches. A paper arm is therefore sized against the
virtual book and *bounded* by a book it does not trade. That is conservative in
the direction that matters (the bound can only shrink the order) but it is not
the same thing as a self-contained paper ledger, and closing it means teaching
Phase 6's `read_context` to take a venue. PR 6 did not do that: it is a Phase 6
change with its own risk surface, and it is recorded here rather than hidden.

### No duplicate orders, at three levels

| Level | Mechanism |
|---|---|
| one execution per decision | the partial unique index on `strategy_trades.decision_id`, held by the database rather than by a process |
| one position per ticker per mode | `reserved_tickers(session, mode=...)` — every non-terminal execution in that mode, **across arms**, because the failure it prevents is two different arms opening the same position |
| within one pass | the dispatcher advances its own view of the book between proposals, so the eleventh name of a ten-position book is blocked inside a single run |

Modes do not reserve against each other. A paper position at Alpaca and a live
position at Robinhood are different books, and treating them as one would make
the tournament's exposure depend on the champion's.

### Experiment tags

Every proposal a paper arm makes carries
`requester_token_label = lab:<experiment>/arm:<id>/<slug>@<version>`, and Phase
6's `client_context` gains `experiment` and `execution_id` for a lab execution
(and is byte-identical for every other proposal). So an order at Alpaca is
attributable to the arm that produced it without a join.

### A stale snapshot abstains

The entry reference a paper dispatch prices against is the decision's own
snapshot — the last bar's split-adjusted close, or, for the compatibility arm
whose plan was frozen by the pipeline that produced it, that composite result's
own `entry_price`. The snapshot's age is therefore the quote's age, and past
`STRATEGY_LAB_PAPER_MAX_SNAPSHOT_AGE_MINUTES` (90 by default) the decision is
skipped with `stale_snapshot` rather than priced off a number nobody should trade
on (Spec Q §12 invariant 7).

## 24. The three jobs PR 5 deferred

PR 5 wrote `resume()`, `expire_stale()` and `reconcile()` and scheduled none of
them, because the flag that would gate the schedule did not exist yet. PR 6 adds
all three, gated on `STRATEGY_LAB_ENABLED` **and** `STRATEGY_LAB_PAPER_ENABLED`,
and re-checked inside each job so a flag turned off without a restart stops the
work rather than only the next schedule.

| Job | Cron (ET) | What it does |
|---|---|---|
| `strategy_lab_resume` | `mon-fri 9-16 */30m` | Re-derives every non-terminal execution from the broker's own answer. **Never places an entry**; may re-place a protective stop, which is the whole point of surviving a restart with an unprotected fill. A read that fails is not an answer. |
| `strategy_lab_expire` | hourly at :05 | Terminates `proposed` executions whose approval reference has lapsed, freeing the decision's one open-execution slot. Releases nothing, because a `proposed` row reserved nothing. |
| `strategy_lab_reconcile` | `mon-fri 16:45` | Compares the execution ledger against the broker, for **every enabled tier whose adapter is registered** — not only paper. Every mismatch moves to `reconciliation_required`, which blocks new entries through the **existing** kill switch rather than a second one, and pages with a recovery instruction. |

All three are gated on `STRATEGY_LAB_PAPER_ENABLED`, and that is also why a *live*
arm requires the paper flag as well as its own (Spec Q §14: each tier requires
every tier below it). The reason is operational rather than ceremonial: `live on,
paper off` would be live positions that nothing resumes after a restart and
nothing reconciles against the broker.

The adapter map the jobs use registers the live venue **only** when the primary
broker declares itself to be that venue. Registering the `BrokerRouter` would
reintroduce exactly the global-mode inference invariant 11 exists to remove.

## 25. Promotion: rendered, then confirmed

Two modules, and the split is the import graph doing its job.

`strategy_lab/promotion.py` holds what a tier change is a property of — the
*data*. It cannot see a feature flag, a kill switch or a broker, because
`strategy_lab/` may not import `config`, `portfolio` or `execution`: the module
that says "a tier change binds this evidence to this arm" must not be able to
read a setting it could infer one from instead.

`orchestrator/strategy_lab_promotion.py` can see all three, and contributes them
as `external_refusals`. A refusal from either side blocks the confirmation
identically, and the card shows both lists.

### What an authorization binds

```text
/promote_arm <source_arm_id> <tier> [reason]
        |
        +-- prepare_target: create-or-return the INACTIVE arm at <tier>,
        |   same experiment, same immutable strategy version, budget from settings
        |   (writes a row; activates nothing; idempotent)
        |
        +-- latest_evidence_id(source) or an explicit snapshot id
        |
        \-- plan(): every refusal, accumulated, never short-circuited
              |
              |  from strategy_lab/promotion.py:
              |   - the domain bindings (authorize_promotion): distinct arms,
              |     evidence belongs to the SOURCE, shared strategy version,
              |     target INACTIVE, warnings acknowledged, a legal rung
              |   - requested_mode == target.mode       <- the card's own claim
              |   - requested_risk_budget == target.risk_budget
              |   - evidence not already used for a DIFFERENT target
              |   - evidence not already used for THIS target
              |   - evidence complete: costs, benchmark, uncertainty, metrics,
              |     n_decisions, content_hash
              |   - the tier's operational floor (Spec Q §10)
              |
              \  from orchestrator/strategy_lab_promotion.py:
                  - STRATEGY_LAB_ENABLED, PHASE6_EXECUTION_ENABLED
                  - the destination tier's own flag
                  - the kill switch
                  - Phase 6's live gates (ALLOW_LIVE_TRADING, EXECUTION_MODE)
                  - the live adapter's declared exit capability
        |
        \-- a card + a signed, expiring, single-use, owner-bound confirmation
              |
              \-- confirm(): RECOMPUTES the plan, then appends a promotion_event
                    and activates the target. Live goes through
                    replace_live_champion, so the global champion swaps
                    atomically or the prior champion is untouched.
```

`requested_mode` and `requested_risk_budget` exist for one reason: without them
the card an owner read ("this promotes `momentum_v1` to *paper* at 0.5%") and the
row that gets activated are two independent facts that happen to agree. A target
arm edited between the render and the tap would activate something nobody saw.

`confirm()` recomputes rather than trusting a stored plan, because the gap
between rendering a card and tapping it is exactly where a kill switch gets
engaged.

### Evidence is not reusable

One `experiment_metric_snapshots` row authorizes one target arm. A second target
is refused outright, and so is re-promoting the same arm with the snapshot that
put it there. "The evidence that justified paper now justifies live" is precisely
the reasoning Spec Q §8 forbids: each tier needs evidence collected *in* that
tier.

### The strongest thing the system will say

`recommendation` returns one of three labels, computed arithmetically from the
stored counts and the configured floors:

- `ready_for_owner_review` — complete evidence, floor met, nothing refusing;
- `insufficient_evidence` — complete or not, the floor is not met;
- `blocked` — something refuses.

None of them is "promote". Spec Q §3 keeps promotion authority with the owner,
and §10 forbids selecting a winner from a return figure. There is no scheduled
job, pipeline hook, or dispatcher path that reaches `confirm()`;
`tests/test_strategy_lab_promotion.py` greps for one.

### A live arm is prepared with a zero budget

`STRATEGY_LAB_LIVE_RISK_BUDGET` defaults to `0.0`, and that is a safety property
rather than an unfinished default: sizing multiplies by the arm's budget, so a
promoted live champion with no budget is the single global champion and can still
place nothing. Setting a number is its own deliberate owner step (Spec Q §3: the
dollar budget is deployment configuration, not part of the strategy).

### Demotion

`/demote_arm <source_arm_id> <tier>` is the same machinery downward, with one
asymmetry: a demotion also stands the **source** arm down (`active -> paused`).
Promoting says nothing about the arm you promoted from; demoting says everything,
and leaving the demoted arm active would mean the tier change changed nothing.

## 26. Promotion is not entry approval

Spec Q §13 states it and PR 6 implements it as two separate signed callbacks with
two separate prefixes:

| Callback | Prefix | What it authorizes | Signed with |
|---|---|---|---|
| tier change | `slpr:` / `slpx:` | one `promotion_event`, one arm activation | `portfolio.approvals.sign`, nonce held in the bot process |
| one entry | `p6ok:` / `p6no:` | one `proposed` execution, atomically | `portfolio.approvals.mint`/`verify`, nonce on the `proposals` row |

Both are owner-bound, expiring and single-use. Neither is the other's approval: a
promoted live arm that proposes ten entries needs ten approvals.

There is exactly **one** HMAC in the codebase (`portfolio.approvals.sign`) and
one kill switch (`portfolio.killswitch`, `/live_kill`). PR 6 added neither a
second signing scheme nor a second switch.

**The gate is re-read at the tap, not only at the render.** An approval card can
sit in a chat for half an hour. Phase 6 already re-checks what it owns —
`PHASE6_EXECUTION_ENABLED`, the kill switch, its own live flags, and every risk
guard from fresh state. `StrategyExecutionService.on_approval` adds the two it
cannot see: the Strategy Lab's tier flag, and (for a live arm) the §12 invariant
2 authorization — still `active`, still the single global champion, still
carrying a `promotion_event`. A promotion is not a standing licence; an arm that
has since been paused, demoted or replaced cannot place, even with a valid card
in hand.

A refusal there deliberately **does not consume the approval or move the row**.
Nothing was placed and nothing was reserved, so clearing the condition and
tapping the same card again works; a card nobody clears is terminated by the
hourly expiry job instead.

**Routing.** A Phase 6 approval callback for a proposal carrying an
`execution_id` is routed to PR 5's explicit-mode service, not the globally-bound
one — approving a lab execution through the router would be exactly the inference
invariant 11 forbids. The routing key is read from the **row**, not from the
callback, so a crafted callback cannot make a plain proposal look like a lab
execution or the reverse. A rejection cancels the `strategy_trades` row as well
as the proposal, so the decision's open-execution slot is released rather than
held forever.

## 27. The operator surface after PR 6

| Command | What it does |
|---|---|
| `/experiments` | experiments, arms, tier distribution |
| `/strategies` | the roster: champion, challengers, versions, statuses |
| `/strategy <slug>` | one strategy's decisions, executions, scorecard, warnings |
| `/pause_experiment` `/resume_experiment` | stop and restart an experiment's arms |
| `/promote_arm <arm> <tier> [reason]` | render a tier change, then confirm it |
| `/demote_arm <arm> <tier> [reason]` | the same, downward; stands the source arm down |
| `/promotions` | the append-only audit trail |
| `/live_kill on\|off` | Phase 6's kill switch. Unchanged, and still the only one |

Everything is owner-only through the existing chat-id allowlist, and every
performance figure on every card is copied out of
`scripts/strategy_lab_scoreboard.py`'s payload. Nothing in `bot/` computes,
rounds, selects or characterises a number.

## 28. What PR 6 does not do

- **It does not enable anything.** `STRATEGY_LAB_PAPER_ENABLED` and
  `STRATEGY_LAB_LIVE_ENABLED` are false, as are the two PR 4 flags and every
  Phase 6 gate. With all of them false a scan behaves exactly as it does today
  and writes no Strategy Lab row (`tests/test_strategy_lab_e2e.py`).
- **It places no live order, and no live order is reachable.** Live needs
  `STRATEGY_LAB_LIVE_ENABLED`, `ALLOW_LIVE_TRADING`, `EXECUTION_MODE=live`,
  `PHASE6_EXECUTION_ENABLED`, kill-switch clearance, a Robinhood adapter
  declaring `can_place_standalone_gtc_stop`, an owner `promotion_event` binding
  the one global champion, a non-zero live risk budget, and a separate signed
  approval per entry. The real `gtc stop_market` probe
  (`docs/EXECUTION_LIFECYCLE.md` §6) has still not been run against a live
  account, so the capability declaration is unverified in production and live
  automation remains disabled with that limitation recorded.
- **It does not give a paper arm its own ledger.** See §23's "what that leaves
  open".
- **It does not evaluate.** `experiment_metric_snapshots` rows are written by PR
  3's evaluator through `scripts/strategy_lab_scoreboard.py` and the maturation
  job; PR 6 reads them and refuses a promotion when one is incomplete. There is
  no scheduled job that records evidence *for the purpose of* a promotion.
- **It adds no migration.** The single Alembic head stays
  `0011_comparable_subject_ticker`; `promotion_events`,
  `experiment_metric_snapshots` and `strategy_trades` have existed since PR 1
  precisely so that PR 6 would not have to reopen the graph.
- **It does not touch production capital or settings.** No Railway variable was
  changed; the rollout checklist in `docs/STRATEGY_LAB_RUNBOOK.md` is a draft
  for the owner to execute.
