# Spec B — Production reliability (P0-1 code fix, P1-3, P1-6)

Read first: `docs/audits/2026-07-04-system-audit.md` (§P0-1, P1-3, P1-6).

## Problem

1. **The bot hung in production** on 2026-07-03 09:30 ET: the position monitor
   logged `broker_positions_reconciled` once and then nothing — for a full day+
   — with no restart (Railway policy `ON_FAILURE` doesn't catch hangs). Likely
   a broker/price call inside `execution/position_monitor.py`
   `_check_portfolio_thresholds` / `_check_positions` blocking forever (no
   timeouts anywhere in the loop).
2. `execution/position_monitor.py:76` `_is_market_hours()` is a naive
   weekday/9:30–16:00 ET check with no US-market holiday calendar (it ran all
   day on July 3, a market holiday).
3. `orchestrator/scheduler.py`: three daily scan jobs (`add_job` at `:32`,
   `:40`, `:48`) all call `_run_scan()` (`:104`) with **no mutual exclusion**.
   The last full scan took 49 minutes; once the scheduler is re-enabled,
   overlap → duplicate PipelineRuns, double API spend, possible duplicate
   orders.
4. `_run_scan`'s `except Exception` (`:121`) only logs — a dead scheduled scan
   is invisible to the operator (no Telegram message).
5. `agents/catalyst_agent.py` and `agents/discovery_agent.py` call the LLM
   without retry (unlike `anthropic_client.py`'s `@retry`) — one transient
   429/503 fails the agent and cascades into parallel-stage timeouts.

## Changes

### B1. Timeouts + watchdog in the monitors
- In `execution/position_monitor.py` and `execution/order_monitor.py`: wrap
  every external call (broker positions/orders, price fetches) in an explicit
  timeout. For sync SDK calls executed on the loop, move them behind
  `asyncio.wait_for(loop.run_in_executor(...), timeout=...)`; for HTTP paths,
  set client timeouts. New settings with defaults:
  `monitor_broker_call_timeout_s: int = 30`.
- Heartbeat + watchdog: each monitor loop records
  `self._last_tick = <utc now>` per iteration. Add a lightweight watchdog task
  (started where the monitors are started, likely `main.py`) that every 5 min
  checks each monitor's last tick; if stale beyond
  `monitor_watchdog_stale_s: int = 900` **during expected-active hours**, log
  CRITICAL, attempt a Telegram system alert, and `sys.exit(1)` so Railway's
  ON_FAILURE policy restarts the container. Timeout exceptions inside an
  iteration are caught, logged, and the loop continues (a slow broker call
  must not kill the loop — only a genuinely stuck one trips the watchdog).
- Match existing structlog event-style logging (`event="..."` keywords).

### B2. Holiday-aware market hours
- Add a US equity-market holiday check to `_is_market_hours()`. Prefer a small
  static table (module-level list of NYSE full-closure dates for 2026–2027 +
  early-close awareness optional/out of scope) over a new dependency — this
  repo avoids heavyweight deps. Apply the same check anywhere else market
  hours are computed (grep for other `_is_market_hours`/market-hours logic,
  e.g. order_monitor, scheduler digests) — unify into one shared helper in
  `utils/` used by all callers rather than copies.

### B3. Scan mutual exclusion + failure alerting
- `orchestrator/scheduler.py`: add an `asyncio.Lock` (or running-flag) around
  `_run_scan`. If a scan is already running when a trigger fires: skip, log
  `event="scan_skipped_overlap"`, and send a Telegram system message noting
  the skip. Do NOT queue (a skipped midday scan is fine; a queued backlog is
  not).
- In the `except Exception` handler at `:121`: in addition to `log.error`,
  send a Telegram system message via the existing notification manager
  (grep `bot/` / `notification` for the system-message API the monitors use).
  The notification attempt itself must be wrapped so a Telegram failure can't
  mask the original error.

### B4. LLM retries in agents
- Route `agents/catalyst_agent.py` and `agents/discovery_agent.py` LLM calls
  through the same retry mechanism `utils/anthropic_client.py` uses (reuse the
  client/decorator — do not hand-roll a new retry). Audit the other agents
  (`fundamental_agent.py`, `macro_agent.py`, `web_research_agent.py`) for the
  same gap and fix uniformly. Retries: transient classes only (429, 5xx,
  connection/timeout), bounded (e.g. 3 attempts, exponential backoff), and the
  final failure must still propagate as it does today.

## Out of scope
- Restarting the prod service, flipping `SCHEDULER_ENABLED` (ops), broker
  order-management bugs (Spec D), utcnow() cleanup (Spec E — don't touch
  deprecation sites you aren't already editing).

## Acceptance criteria
1. Suite green (pytest + unittest CI command). New tests:
   - monitor iteration survives a broker call raising `asyncio.TimeoutError`;
   - watchdog exits when last-tick is stale during active hours, and does NOT
     exit when market is closed;
   - `_is_market_hours()` returns False on 2026-07-03 and 2026-12-25, True on
     a normal Tuesday 10:00 ET;
   - second concurrent `_run_scan` is skipped and notifier called;
   - scan exception triggers the Telegram system message (mocked);
   - catalyst agent retries on a mocked 429 then succeeds; gives up after N.
2. PR body shows the test output and lists every external call site now under
   a timeout (file:line table).
