# Spec G — LLM observability gaps (P2, from Langfuse addendum)

Read first: `docs/audits/2026-07-04-langfuse-addendum.md` (P2-LF-1, P2-LF-3)
and `specs/audit-2026-07-04/README.md` (ground rules). Small package — good
for a cheap agent.

## Problem

1. **Ad-hoc runs lose per-stage Langfuse tags.** The scheduled scan path wraps
   each stage in its own context (`orchestrator/pipeline.py:505/522/530/569/573/581`
   — catalyst/scoring/memo/fundamental/pattern/web_research + ticker), but the
   ad-hoc path (`orchestrator/pipeline.py:903`) wraps the whole run in
   `["ad_hoc", ticker]` and `_run_ad_hoc_inner` adds no stage contexts. Result:
   7 scoring-shaped ad-hoc Opus generations lack the `scoring` tag, so
   `evals/build_dataset.py` (which pulls `traces_by_tag("scoring", ...)`)
   undercounts the BRY-243 corpus (22 counted vs 29 actual). Decision made by
   Bryan 2026-07-04: ad-hoc scoring SHOULD be tagged and included.
2. **Gemini calls are invisible in Langfuse.** All 172 observations in the
   June–July window are Claude-only. Tier-2 screening (`screening/
   gemini_screener.py`), web research, discovery, and pattern event discovery
   (`data/event_discovery.py`) — the stages with the worst production problems
   — have zero cost/latency/behavior telemetry. (Overlaps intent of BRY-106
   run-cost attribution.)

## Changes

### G1. Per-stage tags in the ad-hoc path
- In `_run_ad_hoc_inner` (orchestrator/pipeline.py), wrap each stage call with
  the same `_langfuse_context(tags=[<stage>, ticker])` wrappers the scheduled
  path uses (match the exact tag strings: catalyst, scoring, memo,
  fundamental, pattern, web_research). The outer `ad_hoc` session context
  stays; nested `propagate_attributes` contexts merge tags.
- Verify `evals/build_dataset.py` needs no change (it keys on the `scoring`
  tag and excludes non-ticker tags when deriving the ticker — confirm `ad_hoc`
  is excluded from ticker derivation the way `scheduled_scan`/`test_analyze`
  are at build_dataset.py:76; add it if missing).
- The 7 historical untagged generations are listed with trace IDs in the
  addendum (P2-LF-1 table) — do NOT try to retro-tag via API; instead note in
  `evals/README.md` that pre-2026-07 ad-hoc scoring calls are excluded from
  the trace-pulled corpus.

### G2. Gemini call ledger
- Add a lightweight internal ledger for every Google GenAI call (screener,
  discovery, web research, pattern event search): one structlog event
  `llm_call` with fields: provider, model, stage, ticker (if any), duration_ms,
  input/output token counts when the SDK returns usage metadata, finish
  reason, and a boolean ok/parse_ok. Emit at the existing call wrappers —
  find the shared Gemini client path (grep `genai`/`generate_content` in
  `utils/`, `screening/`, `data/`) and instrument once at the lowest shared
  layer, not per call site, if a shared layer exists.
- Optionally ALSO emit to Langfuse if the OTEL setup makes that cheap (the
  langfuse SDK is present in prod) — but the structlog ledger is the
  requirement; Railway logs become the queryable source.
- Do NOT build cost computation/persistence (that's BRY-106's scope); just
  make the calls visible and greppable.

## Out of scope
- Retro-tagging old traces, cost dashboards, DB persistence of costs
  (BRY-106), any behavior change to the calls themselves (Specs A/C own
  those).

## Acceptance criteria
1. Suite green (pytest + unittest CI command). New tests: ad-hoc flow emits
   stage tags (mock/no-op langfuse — assert the context calls or tag args);
   a mocked Gemini call emits one `llm_call` event with the required fields.
2. PR body: before/after tag behavior for an ad-hoc run, sample `llm_call`
   log line, and confirmation `build_dataset.py` ticker-derivation handles
   `ad_hoc`.
