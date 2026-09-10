# Strategy Lab (Spec Q) — the domain model and its constraints

One champion, many challengers, over the same market opportunities, with the
rules frozen before the results arrive. This document covers **PR 1** — the
domain contracts in `strategy_lab/`, the eight tables in
`migrations/versions/0007_strategy_lab.py`, and the invariants the database
holds rather than trusts — and **PR 2**, the point-in-time snapshot builder,
the deterministic strategy SDK, the execution-policy contract and the initial
strategy roster (§9 onwards).

Nothing here runs. No strategy is registered by any scheduled job, no pipeline
calls one, no flag turns anything on, and no broker is reachable from any of
it. PR 3 adds the runner and the metrics, PR 4 the default-off pipeline hook,
PR 5 the live order lifecycle, PR 6 the paper tournament and promotion
workflow.

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

**None yet.** PR 1 introduces no environment variable and no setting.
`STRATEGY_LAB_ENABLED` and its four siblings (§14) are introduced by PR 4, which
owns `config/settings.py` and `.env.example`, together with the pipeline hook
that is the first thing they gate. A flag that nothing reads is a flag nobody
can trust; there is nothing to enable until then.

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

## 14. What PR 2 does not do

* Nothing is wired into `orchestrator/pipeline.py`. No scheduled job builds a
  snapshot, no arm runs, no decision is persisted by anything but a test.
* No flag is added, because nothing reads one yet (§7 still holds).
* No broker is reachable, no order is proposed, and no notional limit exists to
  change.
* No table is added. PR 1's eight are enough; snapshots record through
  `registry.record_snapshot`, which already existed.
* No promotion-eligible replay is possible yet, and the code says so out loud
  rather than producing a number that looks clean (§9).
