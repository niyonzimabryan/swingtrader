# Running on SQLite and Postgres

`DATABASE_URL` selects the engine. Nothing else in the code path differs.

```
sqlite:///swing_trader.db                                  # local dev
sqlite:////data/swing_trader.db                            # current Railway deploy
postgresql+psycopg://user:password@host:5432/swingtrader   # Postgres
```

The SQLite path is unchanged and stays supported. This document covers what had
to change for the two to behave identically, and how to check that they do.

## Schema

The schema is Alembic-owned from `0001_baseline`; see
[`migrations/README.md`](../migrations/README.md). `init_db()` runs migrations
before any ORM session exists, and `Base.metadata.create_all()` is no longer a
production path.

`python -m scripts.schema_status [DATABASE_URL]` reports, without writing
anything, how `ensure_schema` would classify a given database.

## SQLite-isms that were removed

| # | Where | What it was | Why it broke on Postgres | Fix |
| --- | --- | --- | --- | --- |
| 1 | `database/db.py` `_run_migrations()` | `ALTER TABLE trades ADD COLUMN t1_hit BOOLEAN DEFAULT 0` (and 14 more) | SQLite accepts `0` as a boolean literal; Postgres rejects `DEFAULT 0` on `boolean` | Columns are declared in `0001_baseline` |
| 2 | `database/db.py` `_run_migrations()` | `CREATE TABLE web_research_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, … created_at DATETIME DEFAULT CURRENT_TIMESTAMP)` | `AUTOINCREMENT` is SQLite-only syntax; `DATETIME` is not a Postgres type | `0001_baseline` creates the table from the model (`SERIAL`/`INTEGER` per dialect) |
| 3 | `database/db.py` `_run_migrations()` | `ALTER TABLE memos ADD COLUMN memo_data_json TEXT DEFAULT '{}'` | Inline DDL at startup, unversioned on either engine | `0001_baseline` |
| 4 | `database/db.py` `_run_migrations()` | `CREATE INDEX IF NOT EXISTS …`, `DROP TABLE IF EXISTS reddit_sentiment` | Valid on both, but unversioned schema mutation at startup | Indexes come from the models via the baseline; the `reddit_sentiment` drop is carried in `0001_baseline` |
| 5 | `database/db.py` `init_db()` | `"sqlite" in database_url` chose `check_same_thread` and the WAL pragmas | A Postgres URL whose host, database, or password contains `sqlite` would take the SQLite branch and fail | `database.schema.backend_name()` parses the URL and compares the backend name |
| 6 | `database/db.py` | WAL / `busy_timeout` pragma listener | No Postgres equivalent; `PRAGMA` is a syntax error there | Unchanged in behaviour, but now attached only when the parsed backend is `sqlite`, and documented as a SQLite storage-engine setting |
| 7 | `database/models.py` | 43 `Column(DateTime)` columns holding naive UTC | Neither engine enforces it. An aware datetime is silently stripped of its offset by SQLite's bind processor and truncated by Postgres — both store the wrong instant | `database/types.py::UtcDateTime` converts aware → UTC → naive on bind. Same DDL, so the baseline is unaffected |
| 8 | `database/models.py` `WebResearchCache` | two inline `lambda: datetime.now(timezone.utc).replace(tzinfo=None)` defaults | Not an engine bug, but a second definition of "now" that could drift from the first | Uses `utils.timeutils.utcnow_naive` like every other column |
| 9 | `bot/handlers/performance.py`, `bot/handlers/ask.py` | `ORDER BY exit_date DESC` on a nullable column | SQLite sorts NULLs last under `DESC`; Postgres sorts them **first**, so a closed trade with no `exit_date` would head the list and push real trades out of the `LIMIT` | `.desc().nullslast()`, which pins the existing SQLite behaviour on both |
| 10 | `Base.metadata.create_all()` at startup | schema created from the models on every boot | Not engine-specific, but it makes the schema unversioned and untrackable | `ensure_schema()` |

Three things were checked and deliberately **not** changed:

- **JSON columns.** Every JSON payload is stored in a `Text` column and parsed
  in Python (`Memo.memo_data_dict`, `FundamentalData.raw_data_dict`, …). No SQL
  touches JSON structure, so this is already engine-neutral. Moving to
  Postgres `JSONB` is a Phase 0b/Spec-N decision, not a portability fix.
- **Naive `TIMESTAMP WITHOUT TIME ZONE` storage.** Switching to
  `DateTime(timezone=True)` would make Postgres return aware datetimes and
  SQLite return naive ones — *less* portable, and it would break every
  `utcnow_naive() - row.created_at` in the codebase. Naive UTC everywhere,
  enforced on bind, is the engine-neutral choice.
- **`ORDER BY` on nullable columns that carry a Python-side default**
  (`created_at`, `scored_at`, `added_at`, `started_at`, `confidence`, `rank`).
  Those are never NULL in practice, so no null-placement clause was added.

## Running the suite on Postgres

`tests/dbfixture.py` gives each test a disposable database. With
`TEST_DATABASE_URL` unset it is a temporary SQLite file; when set to a Postgres
URL it is a freshly created, uniquely named schema with `search_path` pinned to
it.

```bash
# SQLite (default)
.venv/bin/python -m unittest discover -s tests -p "test_*.py"

# Postgres
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/swingtrader_test \
  .venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Tests that are genuinely *about* SQLite (the WAL/`busy_timeout` pragmas, adopting
a legacy `.db` file) build their own `sqlite:///` URL so that coverage does not
disappear from the Postgres run.

### A local Postgres

Any of these works. Pick one:

```bash
# Debian/Ubuntu container or host
apt-get install -y postgresql
service postgresql start
su postgres -c "psql -c \"ALTER USER postgres PASSWORD 'postgres';\" -c 'CREATE DATABASE swingtrader_test;'"

# or Docker
docker run --rm -d -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=swingtrader_test postgres:16
```

CI uses a `postgres:16` service container.

## CI

`.github/workflows/ci.yml` runs the tests as a matrix over `engine: [sqlite,
postgres]`. Each entry compiles the sources, runs `alembic upgrade head` against
an empty database of that engine (which exercises the CLI path, `alembic.ini` +
`DATABASE_URL`), and then runs the full suite. The gitleaks history scan moved
into its own `Secret scan` job so it runs once rather than per matrix entry.

The SQLite entry stays until the Postgres migration is confirmed in production
for one week (spec K §3.2).

## What still points at SQLite

These are out of Phase 0a's scope and are listed so they are not forgotten:

- `evals/pnl_monitor.py` and `evals/test_swingtrader_evals.py` open the
  production `.db` file with `sqlite3` directly. They are an offline eval
  harness, not part of the application or of `unittest discover -s tests`. They
  will need a SQLAlchemy path at cutover (Phase 0b).
- `config/settings.py::data_dir()` derives the sidecar-file directory from a
  `sqlite:///` URL and falls back to the working directory otherwise. On
  Postgres the pattern-backfill queue therefore lands in the process's working
  directory; Phase 0b should give it an explicit setting.
- `Dockerfile` and `.env.example` still default to SQLite, which is correct
  until the cutover.
