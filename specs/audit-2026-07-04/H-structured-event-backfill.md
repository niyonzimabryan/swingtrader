# Spec H — API-first historical event ingestion (pattern library warm-up)

Read first: `specs/audit-2026-07-04/README.md` (ground rules),
`docs/audits/2026-07-04-system-audit.md` §P0-2, and skim
`specs/historical-pattern-analysis-robust-fix.md` for the event/outcome model.
Specs A–D are merged; build on latest `origin/main`.

## Problem

The pattern engine's event library warms up only via LLM web search (Gemini
grounded discovery), which yields ~0–2 usable events per ticker per pass under
the quality guards. A useful library needs ~50+ matured events per setup class;
via search alone that's hundreds-to-thousands of grounded calls drained 20
tickers/night — weeks of grinding and real Gemini spend.

Two of the highest-frequency catalyst classes have **structured API sources**
that can bulk-load years of events in a few dozen calls, with exact dates and
zero hallucination risk:

- **Earnings surprises** → FMP (repo already uses the FMP stable API:
  `data/event_outcomes.py` `FMP_BASE`, `data/peer_resolver.py._fmp_request`).
- **Analyst upgrades/downgrades** → FMP grades/upgrades-downgrades endpoints.

Search-backed discovery stays as-is for the genuinely unstructured classes
(product launches, regulatory approvals, sector catalysts).

## Changes

### H1. Bulk loader script: `scripts/bulk_load_structured_events.py`
- CLI: `--tickers AAPL,MSFT` or `--universe` (reuse the ~503-ticker universe
  from `orchestrator/universe.py`), `--years N` (default 3), `--classes
  earnings,upgrades` (default both), `--limit-tickers N`, `--dry-run`.
- For each ticker:
  - **Earnings**: fetch surprise history from FMP (verify the exact stable
    endpoint name against FMP docs AND the repo's existing FMP usage — do not
    guess; peer_resolver shows the request helper pattern). Each report date
    with actual vs estimate becomes a candidate event: polarity from beat/miss
    sign, magnitude scaled from surprise % (cap/clamp sensibly), headline and
    summary generated from the numbers ("Q1 2024 EPS 2.18 vs 1.95 est, +11.8%
    surprise"), `source_type="fmp_structured"`, `source_url` = the FMP endpoint
    reference. NO LLM calls anywhere in this path.
  - **Upgrades**: fetch the grades/upgrades-downgrades history. Cluster
    same-direction actions within a small window (e.g. ≥2 upgrades within 5
    trading days → one `analyst_upgrade_cluster` event dated at the cluster
    start; single isolated upgrades are NOT a cluster — skip, don't inflate).
    Downgrade clusters map to the bearish-polarity equivalent if the taxonomy
    supports it; otherwise skip and note.
- Store via the SAME model/validation path discovery uses (reuse
  `EventExtractor`/store helpers where practical): respect PIT rules
  (`event_date <= today`), dedupe against existing rows by (ticker,
  event_type, event_date ±2 days) before inserting, and skip immature events'
  outcome computation the same way backfill does.
- Compute outcomes with the existing `EventOutcomeEngine` (batched; reuse the
  rate limiter; `--outcomes-per-run` cap, default generous since this is
  offline). Context via `compute_context` best-effort as in
  `scripts/backfill_historical_events.py`.
- Progress visibility: log `bulk_load_progress ticker=... class=...
  events_stored=... outcomes_computed=... skipped_dupes=...` every N tickers,
  and a final `bulk_load_complete` summary. Idempotent: re-running must not
  duplicate (assert via test).

### H2. Taxonomy matching decision (the one design judgment — read carefully)
The ranker matches `event_type == setup_type` exactly, and live catalysts are
classified into guidance-specific earnings classes (`earnings_beat_guide_up`,
`earnings_beat_guide_flat`). FMP surprise data does NOT contain guidance, so:
- Do NOT fabricate guidance direction.
- Store structured earnings events as a guidance-agnostic type (e.g.
  `earnings_beat_structured` / `earnings_miss_structured` — check
  `EVENT_SUPPORTED_TYPES` in `data/event_discovery.py` and extend additively).
- Extend candidate matching in `data/analog_ranker.py` so guidance-specific
  earnings requests ALSO match the guidance-agnostic structured class as a
  **fallback tier** (search-sourced guidance-specific events rank above
  structured-generic ones — implement as a compatible-types set in
  `_candidate_events` plus a small similarity discount, mirroring how evidence
  tiers already discount broad_base_rate). Keep the change minimal and tested;
  no behavior change for non-earnings classes.
- `analyst_upgrade_cluster` has no such ambiguity — exact match.

### H3. Wire nothing into the live scan path
This is an offline/ops tool + a small ranker matching change. No scheduler
job changes, no new live API calls. (The nightly 3 AM drain and live inline
outcomes from Spec A continue unchanged.)

## Out of scope
- New data vendors, transcripts/guidance NLP, M&A/regulatory structured
  sources, Alembic, any live-scan behavior beyond the H2 matching change.

## Acceptance criteria
1. Suite green (pytest + unittest CI parity + compileall). New tests: surprise→
   event mapping (polarity/magnitude/date), upgrade clustering (≥2 in window;
   singles skipped), dedupe/idempotency (run twice → same row count), PIT
   (future/today dates rejected), H2 fallback matching (guide_up request finds
   structured events at discounted similarity; exact-class events preferred),
   dry-run stores nothing.
2. Live proof (FMP key in `.env`, read-only API): run the loader on ~10 real
   tickers with `--years 2`, include the `bulk_load_complete` summary (expect
   dozens+ of events with computed outcomes, zero LLM calls). Then run
   `scripts/evaluate_pattern_analog_engine.py` locally against that scratch DB
   and show an earnings/upgrade case returning `active` with structured analogs.
3. PR body documents the ops runbook for prod:
   `railway ssh python -m scripts.bulk_load_structured_events --universe --years 3`
   (Bryan or reviewer executes; NOT the agent).
