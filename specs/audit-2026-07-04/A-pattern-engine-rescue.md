# Spec A — Pattern engine rescue (P0-2, P1-1, P1-2, P1-7)

Read first: `docs/audits/2026-07-04-system-audit.md` (§P0-2, P1-1, P1-2, P1-7)
and the original feature spec `specs/historical-pattern-analysis-robust-fix.md`.

## Problem

The historical pattern analog engine is live in production
(`PATTERN_ANALOG_ENGINE_ENABLED=true` on Railway) but structurally cannot ever
return analogs:

1. `data/analog_ranker.py:92` and `:104` INNER JOIN `HistoricalEvent` with
   `EventOutcome`, but `EventOutcomeEngine.compute_outcome` is only called from
   `scripts/backfill_event_outcomes.py` / `backfill_historical_events.py`,
   which never run in production. Live-discovered events have no outcome rows →
   join yields zero → every ticker returns `no_matches`. Reproduced locally:
   all 6 DEFAULT_CASES in `scripts/evaluate_pattern_analog_engine.py` fail with
   "No stored historical events for this catalyst type."
2. The cold-ticker backfill queue is write-only: `agents/pattern_agent.py:441`
   writes to `pattern_backfill_queue_path` (default
   `.pattern_backfill_queue.jsonl`, `config/settings.py:129`) — a relative path
   on Railway's ephemeral container FS, wiped each deploy; no consumer exists.
3. Discovery stores junk: prod logs show 26/32 Gemini grounded-search calls
   returning `grounded=False queries=0 sources=0`, yet events still stored —
   including a FUTURE-dated event (`event_date=2026-07-31` for CPRT, logged
   07-02) and same-day events for GIS/PANW that can't have mature outcomes.
   Extraction truncates at `max_tokens=4096` (`data/event_discovery.py:272,280`).
   `data/event_extractor.py:161-175` trusts model-declared `event_date_source`
   with no grounding requirement.
4. Peer resolution degraded: `data/peer_resolver.py:289` calls FMP
   `/stock-screener`, which now 404s (FMP renamed it `/company-screener`).
   18 failures in the last prod scan.
5. Failure modes are masked: provider exceptions collapse to `no_matches`
   (`data/event_discovery.py:247-251, 283`) though the feature spec requires a
   typed `provider_error`; pattern-stage budget exhaustion also returns
   `no_matches` (`agents/pattern_agent.py:343-370`).

## Changes

### A1. Rank outcome-less events as partial (the core fix)
- Replace the INNER JOINs in `data/analog_ranker.py` (`:92`, `:104`) with LEFT
  OUTER JOINs (`isouter=True`). Events without an `EventOutcome` row rank as
  the existing `partial`/immature tier the feature spec already defines
  ("immature-return events stored as partial, not discarded") — they must
  count toward match totals with reduced weight, not be invisible.
- Additionally compute outcomes inline at store time when the event is already
  mature (event_date old enough for the outcome window): after
  `discover_and_store` persists an event, call the same outcome computation
  used by `scripts/backfill_event_outcomes.py`, guarded by a per-scan cap
  (new setting `pattern_inline_outcome_max_per_scan: int = 10`) so scans don't
  balloon. Immature events stay outcome-less (→ partial tier) until backfill.

### A2. Backfill queue on the volume + a consumer
- Change `pattern_backfill_queue_path` default to honor the data dir: resolve
  relative paths against the directory of the SQLite DB file (prod:
  `/data/`) rather than cwd. Keep absolute paths as-is.
- Add a consumer: a scheduled job in `orchestrator/scheduler.py` (daily, off
  market hours, e.g. 03:00 ET) that drains the queue via the existing logic in
  `scripts/backfill_historical_events.py --queue` (import and call a shared
  function — refactor the script's core into an importable function rather
  than duplicating). Cap work per run (new setting
  `pattern_backfill_max_tickers_per_run: int = 20`). Job must be a no-op when
  `pattern_analog_engine_enabled` is False. Log a summary event
  (`pattern_backfill_run tickers=N events_stored=M outcomes_computed=K`).

### A3. Grounding + point-in-time guards at store time
- In the discovery→extraction path (`data/event_discovery.py` /
  `data/event_extractor.py`): reject event candidates whose source search call
  returned `grounded=False` (no queries/sources) — do not store them; log
  `pattern_event_rejected reason=ungrounded`.
- Enforce dates at store time: reject `event_date > today` outright
  (`reason=future_date`); store events with `event_date` newer than the
  outcome maturity window as explicit partial (this is existing intended
  behavior — verify it actually happens; the CPRT/GIS cases suggest not).
- Raise the extraction cap: `max_tokens=4096` at `event_discovery.py:272,280`
  → 8192, and treat a truncated/unparseable extraction as a rejected
  candidate, not a stored one.

### A4. FMP screener endpoint
- `data/peer_resolver.py:289`: `/stock-screener` → `/company-screener` (verify
  param names against current FMP stable docs; adjust if renamed). Regression
  test: mock a 404 on the old path to prove the fallback chain still degrades
  gracefully, and a success test on the new path.

### A5. Typed failure statuses
- `data/event_discovery.py`: provider exceptions (`:247-251`, `:283`) must
  surface `status="provider_error"` (with provider + sanitized message in
  warnings), not empty-list→`no_matches`.
- `agents/pattern_agent.py:343-370`: when `_live_budget_remaining()` gates
  discovery off, return `status="time_budget_exhausted"` instead of
  `no_matches`.
- `scoring/engine.py:219-233` already renormalizes on any non-`active` status —
  verify both new statuses flow through it unchanged (add a scoring test per
  status: pattern-with-error must never score LOWER than pattern-absent, per
  the feature spec's renormalization criterion).
- Memo output: the pattern section must show the explicit unavailable reason
  for the new statuses (feature-spec acceptance criterion).

## Out of scope
- Enabling/disabling the Railway flag, running anything against the prod DB or
  Railway (ops steps below), scheduler re-enable, Alembic.

## Acceptance criteria
1. `python -m pytest tests -q` and the unittest CI command stay green; new
   tests cover: LEFT-JOIN partial ranking, inline outcome cap, queue path
   resolution to the DB dir, consumer drain (with mocked discovery), ungrounded
   rejection, future-date rejection, provider_error and time_budget_exhausted
   statuses end-to-end into scoring, FMP endpoint.
2. Local bakeoff proof: with a scratch SQLite DB, run
   `python -m scripts.backfill_historical_events` (mocked or real Gemini —
   real is fine locally, keys in `.env`) for 2–3 of the DEFAULT_CASES tickers,
   then `python -m scripts.evaluate_pattern_analog_engine` — at least the
   backfilled cases must return `active` with analog rows and non-zero
   analog_count. Include the JSON output in the PR body.
3. No discovery query contains outcome-conditioned tokens
   (`assert_outcome_neutral` guard tests still pass).

## Ops runbook (document in PR body; Bryan executes after merge)
```
railway run python -m scripts.backfill_historical_events
railway run python -m scripts.backfill_event_outcomes
railway run python -m scripts.backfill_event_contexts
railway run python -m scripts.evaluate_pattern_analog_engine   # expect active + bias sanity-check <75% win rate
railway run sqlite3 /data/swing_trader.db "SELECT (SELECT COUNT(*) FROM historical_events),(SELECT COUNT(*) FROM event_outcomes);"
```
