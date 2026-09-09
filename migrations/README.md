# Migrations

The database schema is owned by Alembic. Nothing else may change it.

```
migrations/
  env.py                      Alembic environment (shared by CLI and startup)
  versions/
    0001_baseline.py          the root revision
    3p01_price_plane.py       phase 3p: the price plane (Spec N §4.2/§4.3)
```

Revision ids carry a phase prefix rather than a global sequence number
(`3p01_price_plane`, not `0002_...`): three phases developed in parallel would
each reach for `0002` and collide in the same directory.

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
| `empty` | no ORM tables exist | `upgrade head` |
| `legacy` | exactly the baseline's tables exist with the expected columns, no `alembic_version` | stamp `0001_baseline`, then `upgrade head` |
| `unknown` | anything else | raise `SchemaMismatch` with recovery instructions |

The `legacy` path is how the existing unversioned Railway SQLite file is
adopted without being rebuilt. It compares against `database.schema.BASELINE_TABLES`
— the tables `0001_baseline` creates — and not against `Base.metadata`, because the
moment a phase adds a table those two sets differ: a production database would be
missing the new tables (and classify `unknown`), while a database that matched the
models would be stamped at the baseline and then have the phase's migration try to
create tables it already has. The signature it checks is table and column
*names*: the old inline `ALTER TABLE` statements attached server defaults that
`create_all()` never emitted, so a byte-exact DDL comparison would reject the
very databases this path exists to adopt.

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
