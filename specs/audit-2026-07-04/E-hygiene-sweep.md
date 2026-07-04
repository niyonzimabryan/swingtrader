# Spec E — Hygiene sweep (P2-1, P2-2, P2-3)

Read first: `docs/audits/2026-07-04-system-audit.md` (§P2-1, P2-2, P2-3).
**Run AFTER specs A–D merge** — this touches files they modify. Rebase on
latest `main` before starting.

## Changes

### E1. `datetime.utcnow()` → timezone-aware (33 sites)
- Replace all `datetime.utcnow()` with `datetime.now(timezone.utc)` (or the
  repo's existing tz helper if one exists — grep `utils/` first).
- Production hotspots: `execution/order_monitor.py` (7 sites, e.g. :283, :334),
  `execution/order_manager.py` (2), `execution/position_monitor.py` (1); the
  rest are tests (`test_safety_regression.py`,
  `test_pipeline_reports_execution.py`).
- CAUTION: naive-vs-aware comparison bugs. Anywhere the replaced value is
  compared to or subtracted from a DB-loaded datetime (SQLite DateTime columns
  come back naive), keep both sides consistent — check each site, don't
  regex-replace blind. If DB columns store naive UTC (likely), a helper
  `utcnow_naive()` that returns `datetime.now(timezone.utc).replace(tzinfo=None)`
  for DB-facing sites is acceptable and honest; document the choice.

### E2. Test rot
- `tests/test_safety_regression.py`: 16 SQLAlchemy `Query.get()` legacy calls
  → `Session.get(Model, pk)`.
- `tests/test_deep_research_client.py:11`: replace the
  `asyncio.get_event_loop().run_until_complete(coro)` helper with
  `asyncio.run(coro)` (kills the "no current event loop" DeprecationWarning).
- Goal: `python -m pytest tests -q 2>&1 | tail -1` warning count drops from
  ~246 to near the transitive-only floor (websockets.legacy from the alpaca
  stack remains — out of our control, note it).

### E3. DB indices
- Add to `database/models.py` (and create via the existing inline-migration
  mechanism so existing prod DBs get them — check how prior columns/tables
  were added; SQLite `CREATE INDEX IF NOT EXISTS` is idempotent):
  - `memos(status, created_at)`
  - `trades(status, created_at)`
  - `trades(broker, status)`
- Verify with `EXPLAIN QUERY PLAN` on the digest/status queries (grep the
  digest and /status handlers for their filters; adjust index columns to the
  real predicates rather than guessing).

### E4. Drop the orphaned `reddit_sentiment` table
- `database/models.py:318-330` declares `reddit_sentiment` (retired at commit
  `465c835`; comment says no reads/writes). Remove the model class and the
  `Ticker` relationship; add an inline migration `DROP TABLE IF EXISTS
  reddit_sentiment`. Grep the whole repo for remaining references first.

### E5. `.env.example` completeness
- Audit `config/settings.py` for every setting an operator might tune
  (PATTERN_*, PARALLEL_*, monitor/watchdog timeouts added by Spec B, backfill
  caps added by Spec A, etc.). Add missing entries to `.env.example`,
  commented out, with a one-line purpose comment and the default. Also fix
  the audit-noted mismatch: `.env.example` mentions
  `ROBINHOOD_ALLOWED_SYMBOLS`/`ROBINHOOD_BLOCKED_SYMBOLS` — verify these are
  real settings in code; if not, remove or wire them (report which).
- Add a short comment block at top documenting the split: repo `.env` local
  subset vs Railway full set vs `~/.env` (Langfuse keys).

### E6. Gemini model config sanity (small)
- `config/settings.py:102-105` defines 4 Gemini model vars. Do NOT
  consolidate (that's a design change gated on PR #18's eval); just add a
  startup log line listing which model each stage resolved to, so skew is
  visible in Railway logs.

## Out of scope
- Alembic adoption (BRY-107 decision), Postgres, any behavior changes beyond
  the above, model swaps.

## Acceptance criteria
1. Suite green (pytest + unittest CI command), warning count reported
   before/after in the PR body.
2. Fresh DB (`create_all`) and an existing DB (copy `swing_trader.db`) both
   end up with the new indices and without `reddit_sentiment` after startup
   migration — show the `sqlite3 .schema` excerpt for both.
3. `python -m scripts.doctor --skip-live` unchanged or improved.
4. Every changed line traces to E1–E6 — no drive-by refactors.
