# Spec C — Tier-2 Gemini screener repair (P0-4)

Read first: `docs/audits/2026-07-04-system-audit.md` (§P0-4).

## Problem

The tier-2 screener (`screening/gemini_screener.py`, model
`settings.gemini_flash_model` = `gemini-2.5-flash`) is effectively dead in
production. Railway logs from the 2026-07-02 scan:

- **213× `gemini_json_parse_failed`** across ~209 flagged tickers — the raw
  response is always truncated mid-JSON (e.g.
  `raw='{\n "score": 0.6,\n "direction": "bull'`). Root cause:
  `max_output_tokens=512` at `screening/gemini_screener.py:152` — the model's
  reasoning/preamble + JSON doesn't fit, so nearly every ticker falls back to
  the non-LLM default and tier-2 adds no signal.
- 4× `gemini_screen_failed error="'NoneType' object has no attribute 'strip'"`
  (EA, CPB, …) — response candidate with no text (safety block / empty
  candidate) is not guarded.
- 1× `TOO_MANY_TOOL_CALLS is not a valid FinishReason` warning from
  google-genai — SDK enum drift; should not produce a warning per ticker.

## Changes

### C1. Structured JSON output instead of parse-and-pray
- Use the google-genai structured-output path: set
  `response_mime_type="application/json"` and a `response_schema` matching the
  screener's expected fields (score, direction, reasoning, escalate — read the
  current parse code for the exact shape). This eliminates preamble and makes
  truncation detectable.
- Raise `max_output_tokens` 512 → 2048 (belt and braces with the schema).
- If the SDK/model rejects response_schema for this model, fall back to
  response_mime_type-only + robust extraction; state which path was taken in
  the PR.

### C2. Guard empty responses
- Before `.strip()`/parse: handle `response.text is None`, empty candidates,
  and safety-blocked finishes explicitly → log `gemini_screen_failed` with the
  finish reason, use the existing fallback. No `AttributeError`s.
- Detect truncation: if finish reason is MAX_TOKENS, log it as
  `gemini_screen_truncated` (distinct from parse failure) — these are the
  signal that C1's budget is wrong.
- Unknown FinishReason enums from the SDK must not raise/warn per ticker —
  coerce to string once.

### C3. Parse-failure-rate visibility
- Track per-scan counters in the screener (attempted / parsed / parse_failed /
  truncated / empty). Log a single summary event at scan end
  (`gemini_screen_summary attempted=... parse_failed=...`) and surface the
  failure rate in the pipeline's scan-complete log line. If
  `parse_failed/attempted > 0.10`, emit a WARNING-level
  `gemini_screen_degraded` event (Telegram alerting for this can ride Spec B's
  notifier; if B hasn't merged, log-only is acceptable — note it).

## Out of scope
- Model swaps (gemini GA pin is gated on PR #18's deferred work), discovery/
  pattern Gemini calls (Spec A), scanner tier-1 logic.

## Acceptance criteria
1. Suite green. New tests (mock the Gemini client): valid JSON parses; `None`
   text → fallback without exception; MAX_TOKENS finish → counted as
   truncated; summary counters correct across a mixed batch; >10% failure
   emits `gemini_screen_degraded`.
2. Live proof (keys in `.env`, safe — screener is read-only research): run the
   screener directly on ~10 real tickers via a scratch script (do NOT run
   `main.py`); include the summary line in the PR body showing parse failures
   at 0–1/10 vs. today's ~100%.
