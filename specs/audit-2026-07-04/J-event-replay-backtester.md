# Spec J — Event-replay backtester (BRY-98, right-sized)

Read first: `specs/audit-2026-07-04/README.md` (ground rules). Related:
Spec H (event library), Spec I (shadow ledger — J4 integrates with it).

## Scope decision (read before building)

A "full pipeline" backtest — re-running the LLM agents on historical dates —
is **out of scope, permanently, by design**: the models know the future
relative to past dates and web research cannot reconstruct what was knowable
at time T. Any LLM-stage replay is lookahead-contaminated and would produce
confident garbage. What IS honestly replayable:

1. The **exit engine** (stops/targets/time-exits) over daily bars — pure math.
2. **Event-anchored entries**: the pattern library (structured + search
   events, exact dates, PIT-guarded) as historical entry signals.
3. (later) **Shadow-ledger rows** from Spec I as entry signals with real
   frozen scores.

This answers the questions that matter now: do the bot's catalyst classes
carry edge? Are the ATR stop/target/hold parameters any good? — with
thousands of samples, zero LLM cost, this week.

## J1. Exit-engine simulator (`backtest/simulator.py`)

- `simulate_trade(bars, entry_idx, direction, stop, target_1, target_2,
  max_holding_days, slippage_bps=10) -> TradeResult` — replays the bot's
  actual exit semantics over daily bars:
  - entry at the **open of the bar after** the signal date (T+1 open — the
    honest executable assumption; never T+0 close),
  - stop: if a bar's low (long) / high (short) crosses the stop, exit at
    stop price — but if the bar OPENS through the stop, fill at open
    (gap-through = worse fill; never pretend the stop price was available),
  - target_1: half position exits at T1 (same gap logic, favorable side),
    remainder continues with stop unchanged (mirror
    `execution/order_monitor.py` semantics — read it, don't guess),
  - target_2: remaining half,
  - time exit at max_holding_days close,
  - same-bar stop+target conflict resolves PESSIMISTICALLY (stop first).
- `TradeResult`: exit_date, blended exit price, pnl_pct, rule_fired
  (stop/t1_then_stop/t1_then_t2/t1_then_time/time), holding_days, mfe/mae.
- Deterministic, no network, unit-tested against hand-computed bar
  sequences (gap-through-stop, T1-then-T2, T1-then-time, same-bar conflict).

## J2. Event-anchored replay (`backtest/event_replay.py` + CLI)

- For each `HistoricalEvent` with sufficient bars (reuse the outcome-engine
  price cache; yfinance fallback OK): direction = polarity, entry per J1,
  params from the bot's actual ATR-based logic (reuse the trade-params
  code path from memo generation — import, don't reimplement; compute ATR
  from the same bars).
- Output per event_type (and magnitude buckets, and source_type search vs
  fmp_structured): n, win rate, median/avg pnl_pct, profit factor, avg
  holding days, rule_fired distribution, and win rate vs the audit's >75%
  bias sanity flag.
- CLI: `python -m backtest.run_event_replay [--classes ...] [--years N]
  [--json out.json]` printing a markdown summary table + JSON artifact.
  Never touches the live scan path; reads whatever DB `DATABASE_URL` points
  at (ops runs it in prod container against the warmed library).

## J3. Parameter sensitivity (`--sweep`)

- Small grid: stop-width multiplier {0.75, 1.0, 1.25}, target multiples
  {as-is, ±25%}, max_holding_days {10, 15, 20} → expectancy per combo per
  class, best-combo table with n and a caution when n < 30. Pure recompute
  over cached bars (fast). No auto-application of results — report only.

## J4. Shadow-ledger adapter (thin; skip cleanly if Spec I unmerged)

- If the `scored_candidates` table exists: same replay over ledger rows
  (entry at T+1 open after scored_at; params from stored stop/targets;
  cohort + score-bucket grouping) so the calibration curve becomes
  trade-realistic (with exits) rather than point-return based. Guard with a
  table-exists check; do not import Spec I code paths that may not exist.

## Out of scope
- LLM-stage replay (see scope decision), intraday bars, portfolio-level
  simulation (position interactions/sizing), auto-tuning weights, live-path
  changes of any kind.

## Acceptance
1. Suite green (pytest + unittest CI parity + compileall). Simulator tests
   as listed in J1; replay test on seeded synthetic events+bars produces a
   deterministic class table; sweep test verifies grid shape + n-caution;
   J4 test with a fake table and with the table absent (clean skip).
2. Real-data artifact: run `--classes upgrades,earnings --years 2` locally
   against a scratch DB bulk-loaded via `scripts/bulk_load_structured_events`
   (FMP key note: Railway-only — mirror Spec H's offline approach with
   canned rows if quota/key blocks; label the artifact accordingly).
   Include the markdown table in the PR body.
3. PR body: assumptions list (T+1 open entry, pessimistic same-bar, slippage
   default), ops runbook for the prod-container run, and explicit statement
   of the LLM-replay exclusion + reason.
