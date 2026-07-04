# SwingTrader System Audit — 2026-07-04

Full-system audit: architecture review, Railway production logs, Langfuse traces,
local test suite + doctor, pattern-engine deep dive, and intent reconciliation
across GitHub PRs #16–#20, Linear (BRY), Hermes, and `todoscratchpad.md`.
Findings are sorted by priority. Each item is scoped to be delegable as a
standalone task; "Spec" gives the fix shape and pointers.

**Baseline:** local test suite is fully green (147/147 pytest, 142/142 unittest
CI-parity, evals self-test 7/7, `compileall` clean). Every finding below is a
production/wiring/process gap that unit tests don't cover.

---

## P0 — Production down or feature structurally broken

### P0-1. Bot appears hung in production since 2026-07-03 09:30 ET
- **Evidence:** Railway deployment `b769c43b` (commit `20c92c2`, current) logged
  `broker_positions_reconciled` at 2026-07-03 09:30:48 and *nothing since*
  (verified with `railway logs --since`). Not holiday-related:
  `execution/position_monitor.py:76` `_is_market_hours()` has no holiday
  calendar, so on Jul 3 (market closed) it believed the market was open and
  should have ticked every 60s until 16:00 ET, as it did all day Jul 2.
  Newest Langfuse trace is 2026-07-02 16:02 UTC. Railway restart policy is
  `ON_FAILURE`; a hang never triggers restart.
- **Likely cause:** monitor loop (or whole event loop) blocked inside
  `_check_positions`/`_check_portfolio_thresholds` on a broker/price call with
  no timeout.
- **Spec:** (a) immediate op action: poke the bot via Telegram `/status`; if
  silent, restart the Railway service. (b) Code fix: wrap broker/price calls in
  `execution/position_monitor.py` and `execution/order_monitor.py` with
  explicit timeouts (`asyncio.wait_for` or requests timeout), add a heartbeat
  log + watchdog (if no loop iteration in N minutes, log CRITICAL and
  self-terminate so Railway restarts), and add a US-market holiday calendar to
  `_is_market_hours()` (e.g. `pandas_market_calendars` or a small static list).

### P0-2. Historical pattern engine can never return analogs in production (the eval failure)
- **Root cause (reproduced locally):** `data/analog_ranker.py:91-100`
  `_candidate_events` does an **INNER JOIN on `EventOutcome`** — but
  `EventOutcomeEngine.compute_outcome` is only called from
  `scripts/backfill_historical_events.py` / `backfill_event_outcomes.py`, which
  have **never run in production** (no cron in `railway.toml`, no worker in
  `main.py`). Events stored during live scans never get outcomes, the join
  yields zero rows, every ticker returns `no_matches`. The Phase-6 bakeoff eval
  (`scripts/evaluate_pattern_analog_engine.py`) replays the same store → all 6
  DEFAULT_CASES (DFTX/OSCR/HNGE/AAPL/MSFT/LLY) fail with
  `"No stored historical events for this catalyst type."`
- **Rollout-sequencing violation:** the spec
  (`specs/historical-pattern-analysis-robust-fix.md`, rollout section ~line 113)
  gates flag-enable on backfill + bakeoff on known failure tickers. PR #17's
  body promised the Railway flag flip *after* the fix; Railway now has
  `PATTERN_ANALOG_ENGINE_ENABLED=true` but no backfill/bakeoff was ever
  recorded as run. The engine went live cold.
- **Compounding wiring bugs:**
  - Backfill queue is write-only: `agents/pattern_agent.py:440-456` appends to
    `.pattern_backfill_queue.jsonl` at **cwd** (ephemeral container FS, wiped
    each deploy; default in `config/settings.py:129`), and nothing consumes it.
    Prod logs: all 11 scanned tickers hit `pattern_backfill_enqueued`.
  - Cost burn: each scan spends ~75–82s + multiple Gemini calls per ticker on
    discovery that structurally cannot warm the cache.
- **Spec:** one delegable task, in order:
  1. Fix the join: LEFT JOIN + rank outcome-less events as `partial` (the spec
     already requires immature events be stored as partial, not discarded), OR
     compute outcomes inline for mature events at store time.
  2. Point `pattern_backfill_queue_path` at `/data/` and add a consumer
     (simplest: scheduled call in `orchestrator/scheduler.py`, or nightly
     `railway run python -m scripts.backfill_historical_events --queue`).
  3. Run the one-time backfill against the prod volume:
     `railway run python -m scripts.backfill_historical_events` then
     `backfill_event_outcomes.py`, `backfill_event_contexts.py`.
  4. Re-run `scripts/evaluate_pattern_analog_engine.py` (the failing eval) as
     acceptance: expect `active` with analog rows on the known cases and the
     spec's >75%-win-rate bias sanity check.
  - Confirm-first query (H1 verification):
    `railway run sqlite3 /data/swing_trader.db "SELECT (SELECT COUNT(*) FROM historical_events),(SELECT COUNT(*) FROM event_outcomes);"`
    — expected: N > 0 events, 0 outcomes.

### P0-3. Scheduler is OFF in production — no scans run at all
- **Evidence:** Railway var `SCHEDULER_ENABLED=false`; log
  `scheduler_disabled ... ⏸ Scheduler PAUSED` at deploy (2026-07-01). The only
  scan since deploy was manual (Jul 2, 49 min, 2 memos).
- **Cascade:** the scoring corpus (currently **22 traces**: 11 on 06-11, 11 on
  07-02) can never reach the N_min=150 gate for the BRY-243 Opus→Sonnet parity
  eval, which in turn blocks PR #18's deferred model swaps (opus-4-6→4-8,
  gemini preview→GA pin). At 3 scans/day × ~11 scoring calls, the corpus fills
  in ~4–5 trading days once enabled.
- **Spec:** decision for Bryan — if intentional (pausing during pattern-engine
  debugging), record it in the scratchpad; if not, flip
  `SCHEDULER_ENABLED=true` after P0-1/P0-2 land. Before enabling, also fix
  P1-3 (scan overlap lock) since 3 daily scans + 49-min scan durations are the
  overlap scenario.

### P0-4. Tier-2 Gemini screener is effectively dead (213 JSON parse failures per scan)
- **Evidence:** Railway logs Jul 2: 213× `gemini_json_parse_failed` across ~209
  flagged tickers — raw output always truncated mid-JSON (`max_output_tokens`
  cap on `gemini-2.5-flash`), plus 4× `gemini_screen_failed 'NoneType' object
  has no attribute 'strip'` and a `TOO_MANY_TOOL_CALLS is not a valid
  FinishReason` warning. Tier-2 scoring is running entirely on fallbacks.
- **Spec:** in the tier-2 screener (`screening/`, gemini call site): raise
  `max_output_tokens`, use JSON response schema / `response_mime_type=
  application/json`, batch fewer tickers per call, guard `None` response text,
  and add a parse-failure-rate metric that alerts >10%. Acceptance: a full scan
  with <5% parse failures.

---

## P1 — Quality-degrading, fix soon

### P1-1. Pattern discovery stores ungrounded, current-dated "historical" events (PIT violation)
- **Evidence:** 26 of 32 `gemini_grounded_search_call`s returned
  `grounded=False queries=0 sources=0`; `historical_event_stored
  event_date=2026-07-31 ticker=CPRT` (a **future** date logged Jul 2), and
  same-day events for GIS/PANW stored as history (T+10 outcome can't exist).
  Extraction truncates at `max_tokens=4096` (`data/event_discovery.py:270-276`);
  `data/event_extractor.py:161-175` trusts model-declared `event_date_source`
  with no grounding check.
- **Spec:** reject candidates when `grounded=False`; enforce
  `event_date <= today - outcome_maturity_window` at store time (store newer as
  explicit `partial`); raise/stream past the 4096 extraction cap; investigate
  why grounding fails 81% of the time on `gemini-3.1-pro-preview` (likely the
  preview model / tool-config — relates to PR #18's deferred GA pin).

### P1-2. FMP peer-screener endpoint 404s — analog peer sets degraded
- **Evidence:** 18× `peer_fmp_request_failed 404` on
  `https://financialmodelingprep.com/stable/stock-screener` — FMP renamed the
  endpoint (now `company-screener`).
- **Spec:** update the URL in the peer-resolution module (grep
  `stock-screener`), add a regression test with a mocked 404→fallback, verify
  `get_peers("OSCR")` returns non-empty (spec acceptance criterion).

### P1-3. No scan mutual exclusion + scheduler failures don't alert
- **Evidence:** `orchestrator/scheduler.py:104-122` — three daily jobs call
  `_run_scan()` with no lock; Jul 2's manual scan took 49 minutes, so overlap
  is realistic once the scheduler is re-enabled (duplicate PipelineRuns, double
  API spend, possible duplicate orders). Same block catches all exceptions with
  `log.error` only — no Telegram notification, so a dead scan is invisible.
- **Spec:** add an `asyncio.Lock`/flag so a triggered scan skips (and logs +
  notifies) if one is running; on scan exception, send
  `notification_manager` system message to Telegram.

### P1-4. Broker/DB state drift bugs (Alpaca paper)
- **Evidence:** BBIO Jul 2 12:58 — `limit_sell_failed` / `target_1/2_order_failed`
  `insufficient qty available (requested: 19, available: 0)` — all 39 shares
  `held_for_orders` by existing order `8d721b3b` (target orders conflict with
  the existing stop/bracket). HNGE Jul 2 09:38 — `time_exit_triggered
  days_held=20` → `close_position_failed "position not found: HNGE"` +
  `trade_reconciled_missing_position trade_id=2` (DB thought a position existed
  that the broker had closed).
- **Spec:** in `execution/order_manager.py`: before placing target/stop orders,
  check/cancel conflicting open orders holding qty (or use OCO/bracket
  natively); make time-exit path handle 404 position-not-found by reconciling
  the trade as closed rather than erroring. Add regression tests for both.

### P1-5. SQLite lock contention during parallel scans
- **Evidence:** 10× `web_research_cache_write_failed ... database is locked`
  during the Jul 2 scan (parallel workers=3); cache writes silently lost.
- **Spec:** enable WAL mode + `busy_timeout` on the SQLAlchemy engine
  (`database/`), retry cache writes once. Longer-term this feeds the Alembic/
  Postgres decision (BRY-107; `ARCHITECTURE_EVOLUTION_TRIGGERS.md` trigger #4).

### P1-6. No retry on direct LLM calls in catalyst/discovery agents
- **Evidence:** `agents/catalyst_agent.py`, `agents/discovery_agent.py` call the
  LLM without the retry decorator used in `anthropic_client.py`; a transient
  429/503 fails the agent and cascades (parallel stages then time out).
- **Spec:** route all agent LLM calls through the retrying client (or apply the
  same `@retry`), consistent across agents.

### P1-7. Silent degradation statuses mask real failure modes
- **Evidence:** budget exhaustion returns `no_matches` instead of a distinct
  status (`agents/pattern_agent.py:343-370`); Gemini provider exceptions also
  collapse to `no_matches` (`data/event_discovery.py:247-251, 283`) though the
  spec requires a typed `provider_error`; missing `HistoricalContext` defaults
  similarity to neutral 0.5 with no data-completeness warning
  (`pattern_agent.py:1015-1016`).
- **Spec:** add `time_budget_exhausted` and `provider_error` statuses through
  discovery → pattern output → scoring (scoring already renormalizes on
  non-active status, `scoring/engine.py:219-233`, so downstream is ready);
  log missing-context rate and warn >30%.

---

## P2 — Hygiene, process, tracker debt

### P2-1. Deprecation rot
- 33 `datetime.utcnow()` sites (prod code: `execution/order_monitor.py` ×7,
  `order_manager.py` ×2, `position_monitor.py` ×1, rest tests) — breaks on a
  future Python; 16 SQLAlchemy `Query.get()` legacy calls in
  `tests/test_safety_regression.py`; manual event-loop helper in
  `tests/test_deep_research_client.py:11`; `websockets.legacy` transitive
  warning (alpaca stack).

### P2-2. DB indices + migration story
- No indices on `memos(status, created_at)`, `trades(status/broker)` — full
  scans as tables grow on the Railway volume. Alembic adoption (BRY-107) is
  more urgent now that the pattern engine added 6 tables via inline
  `create_all()` migrations.

### P2-3. Config/docs drift
- `.env.example` missing PATTERN_*/PARALLEL_* tuning knobs; settings define 4
  separate Gemini model vars with no consistency validation; orphaned
  `reddit_sentiment` table still declared (`database/models.py:318-330`) after
  the 465c835 retirement — drop it with a migration.

### P2-4. Tracker/process hygiene (one cleanup sweep)
- **PR #20** (codex scratchpad reconcile) still an open draft — `todoscratchpad.md`
  on main is stale/contradicted; merge or close.
- **BRY-237** (Reddit retirement) shipped at `465c835` but Linear stuck
  "In Review" (bypassed PR) — close it.
- **Stale May duplicates** BRY-15/16/17/18/22 still Todo, duplicating
  done/newer issues — reconcile.
- **Hermes worker crash-looping** ("pid not alive" ×2 each) — 5 swingtrader
  cards (BRY-235/236/106/107/97) blocked on infrastructure, not substance;
  t_b08f0ee7 shows ~20 stale-lock reclaims + a phantom "running" run.
- **BRY-97 email backup**: full implementation exists but failed review (4
  blockers) and is parked as a patch outside the repo — decide: fix & merge or
  formally drop.
- **BRY-60**: unmerged local patch `.hermes/patches/t_806b113c.patch` — rot risk.
- **Process rule to adopt:** feature-flag flips are release steps — record them
  (who/when/pre-flip evidence) in the tracker. The pattern engine went live
  without its spec-mandated backfill + bakeoff gate; that's how a green test
  suite coexisted with a broken production feature.

### P2-5. Local dev env
- Repo `.venv` was empty until this audit (deps now installed via
  `uv pip install -r requirements.txt`); repo `.env` holds only the
  Anthropic/Gemini/DB/Langfuse-URL subset (Langfuse keys are in `~/.env`) — 8
  doctor FAILs locally are expected, but document which env file feeds what.

---

## Suggested delegation order

1. **Ops now:** restart Railway service (P0-1 immediate action); verify with
   `/status`.
2. **Agent A — pattern engine rescue (P0-2 + P1-1 + P1-2 + P1-7):** the join
   fix, queue-to-volume + consumer, one-time prod backfill, grounding/PIT
   guards, FMP endpoint, typed statuses. Acceptance = bakeoff eval passes.
3. **Agent B — production reliability (P0-1 code fix + P1-3 + P1-6):**
   monitor timeouts/watchdog/holiday calendar, scan lock, error → Telegram,
   LLM retries.
4. **Agent C — tier-2 screener repair (P0-4):** token caps/response schema,
   parse-rate metric.
5. **Agent D — broker edge cases (P1-4) + SQLite WAL (P1-5).**
6. **Bryan decisions:** scheduler re-enable timing (P0-3), BRY-97 fate,
   PR #20 merge, Alembic go/no-go.
7. **Agent E — hygiene sweep (P2-1/2/3) + tracker cleanup (P2-4).**
