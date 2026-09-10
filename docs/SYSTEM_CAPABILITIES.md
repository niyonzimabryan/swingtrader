# System Capabilities — What SwingTrader Can Answer

*Updated 2026-07-14, after the July audit-remediation cycle (specs A–J, PRs #20–#37).
Companion docs: [`swing-trader-prd.md`](../swing-trader-prd.md) (product direction +
detailed spec), [`docs/audits/2026-07-04-system-audit.md`](audits/2026-07-04-system-audit.md)
(what was broken and why), [`specs/audit-2026-07-04/`](../specs/audit-2026-07-04/README.md)
(the remediation specs).*

## The arc

The July 4 audit found a system that *looked* healthy — green tests, scheduled
scans, memos in Telegram — while the pattern engine structurally couldn't
return evidence, tier-2 screening silently failed on ~100% of tickers, a hung
process could go unnoticed for days, and nothing measured whether any decision
was good. Thirteen merged PRs later, the system is honest at every layer:
failures are typed and paged, every stage is instrumented, historical evidence
is real and point-in-time-clean, and the system **measures its own judgment**.

## The changes, in five themes

### 1. It stays alive and tells you when it isn't
*(Spec B PR #22, BRY-301 PR #35)*
Timeouts on every broker call; a watchdog that force-restarts a stuck monitor
during market hours; a daily 08:57 ET self-restart (`exit(1)` + Railway
ON_FAILURE — no external dependency); holiday-aware market hours; a scan
mutual-exclusion lock; Telegram paging on the failure modes that actually
occurred: provider credit exhaustion (once per provider per process, fired
from the retry classifiers) and the zombie-scan pattern (>90% per-ticker
failures inside a "successful" scan).

### 2. The decision funnel actually flows
*(Spec C PR #24, model pins PR #32, funnel caps in Spec I)*
Tier-2 Gemini screening: ~100% JSON parse failure → 0% (token budget + robust
balanced-brace extraction; structured output is impossible alongside Search
grounding — documented). Discovery: 0% grounded (the preview Gemini model
refuses to invoke Search when JSON output is requested — verified by
side-by-side probes) → 100% grounded on `gemini-2.5-flash`, pinned across all
three JSON+search stages. Escalation caps (`tier2_max_escalations`,
`scan_max_catalyst_tickers`) keep the reopened funnel from becoming a cost bomb.

### 3. Memos cite real historical evidence
*(Spec A PR #23, Spec H PR #31, hotfixes #27/#29)*
The analog engine's outcome join, backfill queue/consumer, grounding +
future-date (PIT) guards, and FMP endpoints all fixed; every UNIQUE-guarded
existence check made pending-aware (`autoflush=False` blindness). The library
is warmed two ways: guarded Gemini search for unstructured catalysts, and bulk
FMP structured data (earnings surprises, analyst-grade clusters — exact dates,
real price outcomes, zero hallucination risk) with a discounted fallback tier
for guidance-agnostic matches. The originally failing eval now returns
`active` with 7–14 analogs per case in production, bias-sanity-checked.

### 4. It measures its own judgment — the flywheel
*(Spec I PR #34, Spec J PR #37, Spec G PR #30)*
- **Shadow calibration ledger**: every scored ticker (not just memo-worthy
  ones) persists with sub-scores; a nightly job attaches realized forward
  returns at T+1/3/5/10/20; the Sunday report renders win-rate-by-score-bucket.
- **Paper autonomy sandbox** (`AUTO_APPROVE_PAPER`, default off): auto-submits
  the memo tier (≥0.55) and the 0.45–0.55 exploration band (half size, no
  Telegram noise) through the human approval path's exact execution code —
  structurally incapable of live trading (five-vector fail-closed guard).
- **Event-replay backtester**: replays the bot's exact exit semantics (T+1
  open entry, gap-through fills, pessimistic same-bar, calendar-day time
  exits) over the whole event library — per-class expectancy, rule-fired
  distribution, and a stop/target/hold parameter sweep. LLM-stage replay is
  permanently excluded (lookahead contamination).
- **Observability**: per-stage Langfuse tags on every path, structlog
  `llm_call` ledger for all Gemini calls, one `scan_funnel_summary` line per
  scan.

### 5. Changes are governed
*(Spec E PR #25, Spec F PRs #20/#26, Codex extraction PR #33)*
Model swaps gate on eval attestations in CI (dormant until the first
attestation JSON is committed — expected from the BRY-243 parity run);
flag-flips are recorded release steps; hygiene debt cleared (346 → 1 test
warnings, tz-aware datetimes, DB indices, dead tables dropped); trackers
reconciled.

### 6. Strategies are versioned, compared, and promoted only by the owner
*(Spec Q PRs #54/#58/#60/#64/#65 + PR 6 — **shipped disabled**)*
The Strategy Lab turns "is the bot's judgment any good" into "which of several
*named, immutable* strategies is any good, under one snapshot and one cost
model". Four arms (the existing composite as champion, earnings drift, momentum,
short-term reversal), one `MarketSnapshot` per cutoff shared by the
cross-sectional ones, and a decision that is reproducible from that snapshot and
carries no portfolio state — so the same decision can be executed on Monday and
refused on Tuesday, and the row says which book it was judged against.

Three tiers run concurrently and the tier is part of an arm's identity, not a
setting: **shadow** records what a strategy would do and can reach no broker at
all (structurally — the package imports none); **paper** places at Alpaca paper
and only there, whatever `EXECUTION_MODE` or `BROKER_PRIMARY` say; **live** is one
global champion. Between an owner approval and a position sits the full §12 state
machine: the notional reservation persisted before the external call, the
protective stop placed *and read back*, partial fills resized idempotently,
unknown placements held (never guessed successful), reconciliation against the
broker after the close, and restart recovery that may re-place a stop but never an
entry.

Promotion is an append-only `promotion_events` row binding the source arm, its
evidence snapshot, a separate inactive target arm at the same immutable version,
and the requested mode and budget. The strongest label the system will produce is
`ready_for_owner_review`; it cannot promote, and no scheduled job can reach the
confirmation. A promotion is not an entry approval — each proposed execution gets
its own signed, expiring, single-use owner callback.

**All of it is off** (`STRATEGY_LAB_ENABLED`, `_SHADOW_ENABLED`, `_PAPER_ENABLED`,
`_LIVE_ENABLED`, `PHASE6_EXECUTION_ENABLED`, and a live risk budget of 0.0 —
six independent gates). The ladder for turning them on is
`docs/STRATEGY_LAB_RUNBOOK.md`.

## The questions the system can now answer

| Question | Instrument | Available |
|---|---|---|
| Is the bot alive and healthy? | Watchdog, daily restart, billing/zombie pages | Now |
| Where did every candidate die this scan? | `scan_funnel_summary` (one line per scan) | Every scan |
| What did this scan cost, per provider/stage? | `llm_call` ledger + Langfuse stage tags | Every scan |
| What happened historically after events like this? | Analog engine + warmed library, in every memo | Every memo |
| Which catalyst classes carry edge under our exit rules? | `python -m backtest.run_event_replay` | Days (one ops run) |
| Are our stops/targets/holds well-parameterized? | `run_event_replay --sweep` (27-combo grid) | Days |
| Do our scores predict returns? (**north star**) | Ledger decile curve in the Sunday report | ~2 weeks of scans |
| Is the 0.55 memo bar right? Is 0.45–0.55 gold or noise? | Exploration-cohort P&L vs memo-cohort P&L | ~2–4 weeks |
| Does the execution machinery work end-to-end? | 15–30 autonomous paper cycles/month | ~2–4 weeks |
| Is Sonnet-5 = Opus at scoring, at ~30% of cost? | BRY-243 parity eval + attestation gate | Corpus ≥150 (~1 week of scans) |
| Which agents earn their scoring weights? | Attribution over closed trades | 50+ closed trades |
| Does LLM judgment beat class base rates? | Score-sorted ledger outcomes vs backtester base rates | ~1 month |
| Does a *named* strategy beat the composite on the same opportunity set? | Strategy Lab scorecard (`scripts/strategy_lab_scoreboard.py`, `/strategy <slug>`) | 60 days + 100 matured decisions of shadow |
| Is an apparent winner an artifact of trying four variants? | Family-adjusted lower bound and step-M over every variant tried, printed on the card | Same data |
| Are two strategies the same bet wearing two names? | The overlap view: shared names, Jaccard, correlation | Same data |
| Does the execution machinery survive a partial fill, an unknown placement, a failed stop, a restart? | Paper tier + the resume/reconcile jobs | ~30 closed paper executions |
| What exactly authorized this live position? | `promotion_events` + the per-entry approval row, both append-only | Immediately, once live |

## What it deliberately cannot answer

- **What the pipeline would have said on past dates** — LLM historical replay
  is permanently excluded: models know the future relative to past dates and
  the web's state at time T is unreconstructable. Backtests replay *exits* and
  *events*, never LLM judgment.
- **Live fill quality** — paper fills ≠ live fills; the simulator's 10 bps
  slippage is an assumption, labeled as such.
- **Regime robustness** — until the data spans more than one market mood.
  Small-n cautions are printed, not hidden.
- **Whether a Strategy Lab arm has edge** — no arm has produced a single decision
  in production: every flag is off. The scorecard is built and tested; it has
  nothing to rank yet, and it says `insufficient_evidence` rather than guessing.
- **Whether Robinhood can actually protect a live position** — the adapter
  *declares* a standalone `gtc stop_market` capability and the real probe
  (`docs/EXECUTION_LIFECYCLE.md` §6) has not been run against a live account.
  Until it passes, every Strategy Lab live entry is refused before an order is
  formed. That is the designed behaviour, not a gap to route around.

## Enable runbook (current gate: Anthropic credit top-up)

1. Top up API credits (BRY-301).
2. `railway variables --set SCHEDULER_ENABLED=true`
3. Review one supervised scan's `scan_funnel_summary` line (first joint debut
   of fixed tier-2, fixed discovery, sonnet-5 analyst tier, warm library).
4. `railway variables --set AUTO_APPROVE_PAPER=true`
5. `railway ssh python -m backtest.run_event_replay --sweep` for the first
   real per-class expectancy read.
6. Read the Sunday report; let the decile curve drive the next decision.
