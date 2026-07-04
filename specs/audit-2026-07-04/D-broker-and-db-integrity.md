# Spec D — Broker order integrity + SQLite contention (P1-4, P1-5)

Read first: `docs/audits/2026-07-04-system-audit.md` (§P1-4, P1-5).

## Problem

Production evidence from the 2026-07-02 scan (Alpaca paper):

1. **Target orders conflict with held shares.** BBIO 12:58:50 —
   `limit_sell_failed` / `target_1_order_failed` / `target_2_order_failed`
   with `"insufficient qty available (requested: 19, available: 0)"`: all 39
   shares were `held_for_orders` by existing order `8d721b3b` (a stop or
   earlier bracket). The order manager places target/stop orders without
   checking or releasing qty held by existing open orders.
2. **Time-exit vs. broker drift.** HNGE 09:38:19 — `time_exit_triggered
   days_held=20` → `close_position_failed '{"code":40410000,"message":
   "position not found: HNGE"}'` plus `trade_reconciled_missing_position
   trade_id=2`: the DB believed a position was open that the broker had
   already closed; the close attempt errored instead of reconciling.
3. **SQLite lock contention.** 10× `web_research_cache_write_failed ...
   (sqlite3.OperationalError) database is locked` during the parallel scan
   (workers=3) — cache writes silently lost. The engine uses default
   journal mode with no busy timeout.

## Changes

### D1. Order placement respects held quantity (`execution/order_manager.py`)
- Before placing exit orders (stop / target_1 / target_2 / limit sell) for a
  position: query open orders for the symbol; compute available qty
  (position qty − qty held by open orders). If insufficient:
  - If the conflicting order is one this bot manages for the same trade (e.g.
    an existing stop when we now want an OCO-style stop+targets), cancel-and-
    replace atomically (cancel, confirm, then place the new set); log each step.
  - If the conflict is unrecognized, do NOT cancel — log
    `event="exit_order_conflict"` with the blocking order id and alert via the
    existing notifier.
- Prefer Alpaca's native bracket/OCO order class where the current code
  places separate stop + target orders for a new position — investigate
  whether the entry path already uses brackets; if separate legs are
  deliberate (partial targets at T1/T2), keep legs but implement
  cancel-and-replace above. State which in the PR.

### D2. Close-position handles position-not-found as reconciliation
- In the time-exit / close path (grep `close_position_failed`,
  `time_exit_triggered` in `execution/`): on Alpaca error code 40410000 /
  404 position-not-found, mark the DB trade closed via the same logic
  `trade_reconciled_missing_position` uses (it already fires — make the close
  path do the reconciliation itself rather than erroring first), fetch fills
  if available to record exit price, else mark exit price/PNL as
  reconciled-unknown. One clean log event, no error-level noise for a
  self-healing case.

### D3. SQLite WAL + busy timeout
- Where the SQLAlchemy engine is created (`database/`): for SQLite URLs, set
  `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on connect (event
  listener on "connect"). Keep it opt-out via setting
  `sqlite_wal_enabled: bool = True` only if trivial; otherwise unconditional
  is fine for SQLite.
- Retry once on `OperationalError: database is locked` for the web-research
  cache write path specifically (it's a cache — second failure logs and
  drops, as today).
- Note: WAL creates `-wal`/`-shm` files next to the DB — harmless on the
  Railway volume; mention in PR body. Do NOT attempt Postgres/Alembic here
  (BRY-107 is a separate decision).

## Out of scope
- Robinhood broker path (paper Alpaca only for these bugs), position-monitor
  timeouts/watchdog (Spec B), schema migrations.

## Acceptance criteria
1. Suite green. New tests (mock Alpaca client + in-memory/tmp SQLite):
   - exit-order placement with all qty held by a bot-managed stop →
     cancel-and-replace sequence issued, orders placed;
   - qty held by unrecognized order → no cancel, conflict event + alert;
   - close on 404 position-not-found → trade reconciled closed, no exception;
   - concurrent writes from 3 threads to a tmp SQLite DB with WAL+busy_timeout
     succeed (regression for the lock error);
   - cache write retries once on lock then succeeds.
2. PR body: file:line list of every exit-order call site audited, and
   before/after behavior table for the BBIO and HNGE scenarios.
