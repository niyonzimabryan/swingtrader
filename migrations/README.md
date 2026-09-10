# Migrations

The database schema is owned by Alembic. Nothing else may change it.

```
migrations/
  env.py                          Alembic environment (shared by CLI and startup)
  versions/
    0001_baseline.py                 the root revision
    0002_workspace_tokens.py         Phase 0b: workspace owner tokens (spec K §4.1)
    0002_source_observations.py      Phase 3a: the bitemporal source ledger (spec O §2)
    0002_price_plane.py              Phase 3p: securities, bars, actions, membership (spec N §4.2)
    0003_merge_heads.py              the merge of the first two parallel phases
    0004_research_workspace.py       Phase 2: dossiers, theses, invalidators, journal (spec M §3)
    0004_portfolio_ledger.py         Phase 1: the portfolio ledger (spec L §3)
    0004_merge_price_plane.py        joins the price plane to that head
    0005_merge_portfolio_ledger.py   joins the portfolio ledger to it
    0006_merge_research_workspace.py joins the research workspace to it
    0007_strategy_lab.py             Phase 5: the Strategy Lab experiment tables (spec Q §8)
    0008_merge_strategy_lab.py       joins the Strategy Lab branch to the integrated head
    0009_execution_lifecycle.py      Phase 6: proposals + the persistent kill switch (spec L §6)
```

`0009_execution_lifecycle` branches from the single head `0008_merge_strategy_lab`
and adds two tables, `proposals` and `execution_kill_switch`. It carries no
foreign keys of its own: `cohort_answer_id` (Spec N), `execution_id`
(`strategy_trades`, Spec Q) and `account_id` (`brokerage_accounts`, Phase 1) are
recorded as plain values, so the revision joins the integrated head as a clean
add with nothing cross-phase to reconcile.

`0007_strategy_lab` is the first revision to carry a **partial unique index**
(`WHERE` on a unique index). Both engines support it, and it is how two Spec Q
invariants are held by the database rather than by the application: at most one
globally active live arm, and at most one non-terminal execution per decision.
See [`docs/STRATEGY_LAB.md`](../docs/STRATEGY_LAB.md) §3.

## The baseline rule

`0001_baseline` is the root revision. It is a snapshot of the schema as it
existed at the end of the pre-Alembic era — `Base.metadata.create_all()` plus
the inline `ALTER TABLE` migrations that used to run in `database/db.py`.

**Every migration, in every phase, branches from `0001_baseline` or from a
revision that descends from it. Never from another phase's migration, and never
by editing another phase's migration.**

Phases are developed in parallel branches. If two phases each add a revision on
top of the same parent, they produce two heads at integration; resolve that with
an explicit merge revision (`alembic merge -m "merge phase N and phase M" <rev>
<rev>`), never by rewriting either side. Writing your tables so that merge is
trivial — no cross-phase foreign keys without a documented reason — is part of
the job.

`tests/test_schema_discipline.py` fails if the graph gains a second base, gains
a second head, or contains a revision that does not descend from the baseline.

## Adding a table: nothing extra to do

`database/schema.py` adopts an unversioned pre-Alembic database by comparing its
table and column names against **each revision in the graph**, so a database
built before your migration existed is adopted at the revision it does match and
your table is created by the `upgrade head` that follows the stamp. You do not
have to register the table anywhere.

Phase 3a shipped this as an explicit `POST_BASELINE_TABLES` list, on the
reasoning that a hand-maintained line per phase is a one-line merge conflict and
therefore cheap. Phase 0b replaced it at integration with the derived version:
the failure mode of forgetting the line is every un-adopted production database
becoming `SchemaMismatch` at startup, which is a bad thing to leave to whether
someone remembered. The signatures are computed by replaying the migrations into
a throwaway in-memory SQLite database, once per process, and only when a
database has tables but no `alembic_version` — so the cost lands on the one path
that needs it and nowhere else.

## Adding a migration

```bash
export DATABASE_URL=sqlite:///swing_trader.db     # or postgresql+psycopg://...
.venv/bin/python -m alembic revision --autogenerate -m "add strategy_versions"
.venv/bin/python -m alembic upgrade head
```

Then:

1. **Read the generated file.** Autogenerate is a first draft: it does not see
   data migrations, and it renders server defaults and type changes literally.
2. Run the suite on both engines (see `docs/DATABASE_ENGINES.md`). A migration
   that only works on one of them is not finished.
3. Keep the revision engine-neutral. `env.py` turns on `render_as_batch` for
   SQLite so that column alters go through a table rebuild; on Postgres the
   same operation is a plain `ALTER`. Do not hand-write SQLite-only DDL
   (`AUTOINCREMENT`, `BOOLEAN DEFAULT 0`, `PRAGMA`) or Postgres-only DDL
   without a dialect branch.

## What is forbidden

- Inline DDL in application code — `ALTER TABLE`, `CREATE TABLE`, `CREATE
  INDEX`, `DROP TABLE` in any module outside `migrations/`. This is what
  `database/db.py::_run_migrations()` used to do; it is gone and a test keeps
  it gone.
- `Base.metadata.create_all()` as a schema path. The single sanctioned caller is
  `database.schema.create_all_for_tests()`, for ephemeral test databases.

Both are enforced by `tests/test_schema_discipline.py`.

## Startup behaviour

`database.db.init_db()` calls `database.schema.ensure_schema()` before any ORM
session exists. `ensure_schema` classifies the database and acts:

| State | Condition | Action |
| --- | --- | --- |
| `versioned` | `alembic_version` exists | `upgrade head` |
| `empty` | no migration-owned tables exist | `upgrade head` |
| `legacy` | the tables and columns present are exactly some revision's, no `alembic_version` | stamp **that revision**, then `upgrade head` |
| `unknown` | anything else | raise `SchemaMismatch` with recovery instructions |

The `legacy` path is how the existing unversioned Railway SQLite file is
adopted without being rebuilt. The signature it checks is table and column
*names*: the old inline `ALTER TABLE` statements attached server defaults that
`create_all()` never emitted, so a byte-exact DDL comparison would reject the
very databases this path exists to adopt.

It compares against **every revision in the graph**, not against
`Base.metadata`. That distinction only started to matter when `0002` added a
table: the production file has the baseline's tables and not `workspace_tokens`,
and a models-only comparison would have called it `unknown`. The signatures are
computed by replaying the migrations into a throwaway in-memory SQLite database,
once per process, and only when a database has tables but no `alembic_version`.
A phase that adds a table needs to do nothing for this to keep working.

On Postgres, `ensure_schema` takes `pg_advisory_xact_lock` before it classifies
anything, so the bot and the workspace service booting at the same moment cannot
both run `upgrade head`.

Check how a given database will be classified before deploying — read-only,
writes nothing:

```bash
python -m scripts.schema_status sqlite:///copy-of-prod.db
```

Run it against a **copy** of the production file before the first deploy that
runs Alembic. It should report `legacy`.

`unknown` fails closed on purpose. Stamping a schema you have not inspected
makes every later migration a silent no-op, and the damage only surfaces much
later. Recovery is manual: back the database up, reconcile it, then
`alembic stamp 0001_baseline && alembic upgrade head`.

## Downgrade

`0001_baseline.downgrade()` drops every table. It exists so the revision is
reversible in a test, not because downgrading production is a supported
operation.
