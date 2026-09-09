# Strategy Lab (Spec Q) — the domain model and its constraints

One champion, many challengers, over the same market opportunities, with the
rules frozen before the results arrive. This document covers **PR 1**: the
domain contracts in `strategy_lab/`, the eight tables in
`migrations/versions/0007_strategy_lab.py`, and the invariants the database
holds rather than trusts.

Nothing here runs. No strategy is registered, no pipeline calls it, no flag
turns it on, and no broker is reachable from any of it. PR 2 adds the snapshot
builder and the strategies, PR 3 the runner and the metrics, PR 4 the
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

* the package may reach `strategy_lab`, `utils`, and — from `registry.py` alone
  — `database`. Nothing else, first-party or third-party;
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

# How a given database will be classified before it is migrated. Read-only.
python -m scripts.schema_status sqlite:///copy-of-prod.db
```

With `TEST_POSTGRES_URL` set, the migration and constraint tests run against
SQLite *and* Postgres in a single run rather than waiting for the CI matrix to
cover the second engine. An invariant that holds on only one of the two engines
`DATABASE_URL` can select is not an invariant.
