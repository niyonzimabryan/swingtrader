# Postgres cutover runbook (owner actions)

Moving SwingTrader's production persistence from the SQLite file on the Railway
volume to Postgres, per [spec K §3.2](../specs/investment-workspace/K-workspace-core-and-tool-surface.md).

**Done 2026-09-12.** Phases A–D were executed against production; the parity
reports are committed in `docs/audits/` (`2026-09-12T153015Z-` for the rehearsal
and `2026-09-12T154146Z-` for the cutover), every table `ok`, 61 tables and 1764
rows each time. The bot and the workspace both run on Postgres at
`0011_comparable_subject_ticker`. `docs/OWNER_SETUP.md` §2 carries the corrected
copy-paste sequence and the three shell traps that cost the most time; this file
remains the reasoning, and the procedure to follow for a rebuild or a repeat.

Read it end to end before starting. Phase C is the only step with downtime.

---

## What is already true

| | |
| --- | --- |
| Schema owner | Alembic, from `0001_baseline`. `head` is `0011_comparable_subject_ticker` as of 2026-09-12. |
| Engine selection | `DATABASE_URL` alone. `postgresql+psycopg://...` needs no other code change. |
| Startup | `init_db()` → `ensure_schema()` classifies the database and migrates it. Two processes starting together serialise on a Postgres advisory lock. |
| The SQLite file | Read-only from the migration script's point of view (`mode=ro`), and kept as an archive. |
| Test coverage | The suite runs on both engines in CI; `tests/test_sqlite_migration_roundtrip.py` migrates a populated SQLite fixture into a real Postgres and compares per-table content hashes. |

---

## Phase A — provision (no production change)

1. In Railway project `e556a6d9-2023-4c81-a031-e32e160a33be`, add a **Postgres**
   database.
2. Copy its connection URL from the Postgres service's variables. Railway
   publishes it as `DATABASE_URL` / `DATABASE_PUBLIC_URL` in the
   `postgresql://` form. **Rewrite the scheme to `postgresql+psycopg://`** —
   the repo's driver is psycopg 3, and a bare `postgresql://` URL asks
   SQLAlchemy for psycopg2, which is not installed.
3. Keep the private URL for services inside the project; the public one is for
   your laptop, and costs egress.

Nothing reads it yet. The bot is still on SQLite.

## Phase B — rehearse (no production change)

Everything here is read-only against production.

1. **Classify the production file.** Read-only; writes nothing, migrates
   nothing:

   ```bash
   railway ssh --service <bot-service>          # or any shell in that container
   python -m scripts.schema_status sqlite:////data/swing_trader.db
   ```

   Expect `state: legacy` (pre-Alembic schema; startup would stamp
   `0001_baseline`, then upgrade) or `state: versioned` if a deploy has already
   adopted it. **If it reports `unknown`, stop** — the recovery instructions it
   prints are the next step, not this runbook.

2. **Take a copy of the file.** It is the rollback plan and the archive.

   Two things the first execution got wrong, recorded so the next one does not:

   - **Snapshot, do not copy.** A raw copy of a live SQLite database with a WAL
     is not a consistent image. Use `sqlite3.Connection.backup` into `/tmp`
     inside the container.
   - **`railway ssh` gives you a PTY, so binary output is corrupted.**
     `railway ssh -- cat /data/swing_trader.db > file.db` produces a broken
     file. Pipe through `gzip -9 -c … | base64 -w0`, strip CR/LF locally with
     `tr -d '\r\n'`, and decode with `base64 -D` (macOS spells it `-D`, not
     `-d`). That round-tripped 4.4 MB byte-identically, sha256-verified on both
     ends. If it ever does mangle, chunk it; the volume copy is an acceptable
     archive of last resort, and say so in the report.
   - The CLI also re-parses your command through `sh -c` in the container, so
     locally-typed quotes are consumed locally and `python -c "…(…)…"` dies with
     ``sh: 1: Syntax error: "(" unexpected``. Prefer `python -m` invocations with
     metacharacter-free arguments, or base64 a script across.

3. **Rehearse the migration into a scratch database.** Create a second Postgres
   database (or just a scratch one locally) and run the real script against the
   real file:

   ```bash
   python -m scripts.migrate_sqlite_to_postgres \
       --source sqlite:////data/swing_trader.db \
       --target "postgresql+psycopg://.../scratch"
   ```

   It prints the parity table and writes it to `docs/audits/`. Every row must
   read `ok`. Exit status is non-zero if any table failed, so it is safe to put
   in a script.

   The rehearsal is what tells you how long Phase C's window is. On 2026-09-12
   it was seconds, not minutes: 61 tables and 1764 rows.

   **Run it inside the bot container over the private network** rather than from
   the laptop. The private URL never leaves the project, no data crosses the
   PTY, and there is no egress cost. Note this project's Postgres service
   publishes only `DATABASE_URL` — there is **no `DATABASE_PUBLIC_URL`** until a
   TCP proxy is enabled in the dashboard, which running in-container avoids
   needing.

   Pass `--report-dir` to a writable path and **capture stdout locally**: the
   report file is written inside the container, where the next redeploy wipes
   it. The printed report is the same text.

## Phase C — cutover (bot downtime: the length of the copy)

The window exists because the bot must not write to SQLite while the copy runs;
a row written after its table is hashed would show up as a mismatch, and one
written after the flip would be stranded in a file nothing reads.

1. **Stop the bot.** Remove the service's replica (Railway: pause the service,
   or set replicas to 0). Confirm from the logs that it is down.

   **There is no CLI path to this.** On CLI 4.29.0 both `railway scale` and
   `railway service scale` panic with `couldn't get regions:
   GraphQLError("Cannot query field \"railwayMetal\" on type \"Region\"")`. It is
   a dashboard action: service → Settings → Deploy → Replicas. And **do not**
   set `numReplicas = 0` in `railway.toml`: that file is committed and Railway
   applies it to every service built from this repo, so it would also stop the
   `workspace` service, and it takes a push and a rebuild to apply and another
   to revert.

   **The alternative actually used on 2026-09-12 was to prove the window rather
   than enforce it.** Hash a consistent snapshot before the migration, and
   re-hash the live file after it; if the two agree, nothing was written and the
   copy you migrated was the final state. Both were
   `23455bf62d0ec26c07772c8f5d76854629c253fe284ebb76ea948b1f8297bf51`. This is a
   defensible substitute only when the bot is demonstrably idle — on that day
   `SCHEDULER_ENABLED=false`, `holdings` and `broker_orders` were empty, and two
   snapshots 75 seconds apart were already byte-identical — and it still requires
   that no Telegram command is sent during the run. A verified window is stronger
   than an assumed one; an unverified window is weaker than both.

2. **Run the migration for real.**

   ```bash
   python -m scripts.migrate_sqlite_to_postgres \
       --source sqlite:////data/swing_trader.db \
       --target "$POSTGRES_URL"
   ```

   The script refuses a target that already holds rows unless you pass
   `--force`, which deletes them first. On a fresh database you will not need
   it. **Do not pass `--force` after the bot has started writing to Postgres**
   — that deletes real rows.

3. **Read the report.** Every table `ok`, source and target row counts equal.
   Commit the report from `docs/audits/` to the repo; it is the record that the
   cutover was verified rather than assumed.

4. **Confirm the target independently.**

   ```bash
   python -m scripts.schema_status "$POSTGRES_URL"     # -> versioned, at head
   ```

5. **Set the bot service's variables:**

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | the `postgresql+psycopg://` URL |
   | `DATA_DIR` | `/data` |

   Set the URL with `railway variables --set-from-stdin DATABASE_URL`, not
   `--set`, so the password never enters shell history or the process list. Pass
   `--skip-deploys` on every variable but the last so the service redeploys once
   rather than once per variable — and note that **any** variable change
   redeploys the service, which kills anything you have running in its container
   (a long `scripts.price_backfill`, for instance). Set the variables you know
   you need before starting long work, not after.

   `DATA_DIR` is not optional. Two files live beside the database and must stay
   on the mounted volume: the encrypted Robinhood OAuth token
   (`robinhood_token.enc`) and the pattern-backfill queue. Their location used
   to be derived from the SQLite path; on a Postgres URL without `DATA_DIR`
   they land on the container filesystem and vanish on the next deploy, which
   shows up later as an unexplained Robinhood re-authentication.

   The `Dockerfile` still sets `ENV DATABASE_URL=sqlite:////data/swing_trader.db`.
   A Railway service variable overrides it. Leave the volume mounted: the
   SQLite file stays there as the archive.

6. **Start the bot.** In the logs, expect:

   ```
   schema_ready action=upgraded backend=postgresql
   ```

   `action=created` means it built an empty schema — i.e. it is pointed at the
   wrong database. Stop and check the URL.

7. **Smoke test** before walking away: `/status` in Telegram, one `/eval`, and
   confirm the position and order monitors tick.

## Phase D — the workspace service

Only after Phase C is confirmed.

1. **Create a second service** in the same project from the same repository.
   Point its config-as-code path at `railway.workspace.toml` (Railway service
   settings → *Config as code*). It builds the same Dockerfile and starts
   `python -m workspace.server` with a `/health` healthcheck.

   *Unverified:* the exact name and location of that setting, and whether
   Railway will instead want the start command set directly on the service.
   Both are one field in the service settings; `railway.workspace.toml` records
   what the values should be either way. It has no volume — the workspace holds
   no local state.

2. **Set its variables:**

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | the same Postgres URL as the bot |
   | `WORKSPACE_API_ENABLED` | `false` for the first deploy |
   | `WORKSPACE_BASE_URL` | the service's public URL, once Railway assigns one |

   Deploy and check `/health`. With the flag false it answers 200 and reports
   `"workspace_api_enabled": false`; every other route returns 503. That is the
   deploy being observable without being open.

3. **Issue a token** (from any shell with the Postgres `DATABASE_URL`):

   ```bash
   python -m scripts.workspace_token --issue --label "claude-code"
   ```

   Shown once. Only its SHA-256 digest is stored.

4. **Flip `WORKSPACE_API_ENABLED=true`** and redeploy.

5. **Attach the clients** — [`docs/WORKSPACE_ACCESS.md`](WORKSPACE_ACCESS.md).

## Rollback

Within the first week, before the SQLite archive has gone stale:

1. Stop the bot.
2. Set `DATABASE_URL` back to `sqlite:////data/swing_trader.db`.
3. Start the bot.

The archive is byte-identical to what it was at Phase C — the migration script
opens it `mode=ro` and a test proves a write through that URL is refused. The
cost of rolling back is every row written to Postgres after the flip. That is
the whole reason to smoke-test in step 7 rather than the next morning.

## Do not

- **Delete `swing_trader.db`.** Spec K §3.2: it stays as a read-only archive.
  Not in this cutover, and not in the PR that follows it.
- **Re-run the migration with `--force` after the bot is live on Postgres.**
  It deletes every target row first.
- **Point the bot and the workspace at different databases.** They share one,
  and `ensure_schema`'s advisory lock is what makes booting them together safe.

## After a week

Spec K §3.2 keeps the SQLite CI matrix entry "until the migration is confirmed
in production for one week". When that week is up, that is a separate, small PR:
drop the SQLite matrix entry (or keep it — it is cheap and it is the only thing
that keeps local development on SQLite honest), and change the `Dockerfile`
default. Neither is urgent, and neither should ride along with anything else.
