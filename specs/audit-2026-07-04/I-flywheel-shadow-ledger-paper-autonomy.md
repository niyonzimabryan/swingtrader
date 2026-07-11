# Spec I — Flywheel: shadow calibration ledger + paper autonomy sandbox

Read first: `specs/audit-2026-07-04/README.md` (ground rules). Context:
`docs/audits/2026-07-04-system-audit.md` for architecture; funnel evidence
below is from Langfuse (all 22 healthy scoring calls, 2026-06-11 + 2026-07-02
scans) and the 2026-07-08 live screener test.

## Why (the funnel evidence)

Bryan is the learning-loop bottleneck and memos are rare. Measured causes:

1. **Top-of-funnel was broken in every healthy scan on record.** Tier-2 Gemini
   screening 100% parse-failed (fixed 2026-07-08, PR #24) and discovery was
   ungrounded (fixed 2026-07-09/11) on both evidenced scans — catalyst saw only
   14–21 of 503 tickers. Both fixes have NEVER run in a healthy scan (Anthropic
   credit outage since 07-07, BRY-301).
2. **Scoring is conservative, threshold sits on a cluster.** All 22 healthy
   final_scores: range 0.05–0.72, mean ~0.41, zero ever ≥0.75 (deep research
   has never triggered). memo_threshold=0.55 passes ~2/11 per scan; a
   0.45–0.55 band holds ~4 more per scan (MSM 0.53, CPRT 0.52, CPNG 0.50,
   INTC/HIMS 0.48…) that nobody has ever evaluated.
3. **No escalation cap.** The fixed screener escalated 8/10 tickers at the
   0.50 threshold in live testing; `_build_scan_list` Priority 0 admits ALL
   escalations. First healthy scan could push 100+ tickers into catalyst
   (each = Haiku+Sonnet+3 parallel agents + 45s pattern budget): cost and
   duration bomb, and scans already run ~45 min.

Design stance: do NOT loosen the Telegram memo bar (protect operator signal).
Instead: cap+instrument the funnel (I0), record every scored candidate with
forward returns (I1 — the calibration flywheel; ~30 labeled datapoints/day vs
~2 approvals), and let the PAPER account auto-trade both the memo tier and the
exploration band (I2 — zero-risk execution data, removes the human gate where
it controls nothing). Threshold changes then become empirical decisions from
I1's decile data, not guesses.

## I0. Funnel guardrails + telemetry (do first — protects the first healthy scan)

- New settings (+ .env.example, commented):
  - `tier2_max_escalations: int = 25` — after tier-2 screening, keep only the
    top-N escalated tickers by Gemini score (stable sort, ties by score then
    symbol). Log `tier2_escalation_capped kept=N dropped=M`.
  - `scan_max_catalyst_tickers: int = 40` — hard cap on the merged scan list
    entering catalyst, applied after the existing priority ordering (tier2 >
    tier1 > discovery > watchlist > universe) so the cap never evicts
    higher-priority sources in favor of lower. Log what was dropped.
- One `scan_funnel_summary` log event at scan end: universe, tier1_flagged,
  tier2_screened/escalated/capped, catalyst_analyzed, catalyst_gate_passed
  (score ≥0.3), scored, memo_threshold_passed, memos_sent, shadow_recorded
  (I1), paper_orders (I2). This is the single line Bryan reads per scan.

## I1. Shadow calibration ledger (the flywheel)

- New table `scored_candidates` (inline migration, matching existing style):
  id, run_id, ticker, scored_at (UTC), source (tier2/discovery/watchlist/...),
  final_score, catalyst_score, fundamental_score, pattern_score,
  pattern_status, web_research_score, direction, regime (str), entry_price
  (price at scoring time — reuse whatever price the pipeline already fetched;
  else close via the outcome engine's price cache), suggested_stop/target_1
  (from trade params if computed), memo_generated (bool), paper_traded (bool,
  I2), cohort ("memo" | "exploration" | "below"), plus forward-return columns:
  ret_t1, ret_t3, ret_t5, ret_t10, ret_t20 (nullable), returns_computed_at.
- Write one row for EVERY ticker that reaches scoring (both scheduled and
  ad-hoc), regardless of outcome. Never gate or alter pipeline behavior on
  ledger failures (wrap in try/except, log `shadow_ledger_write_failed`).
- Nightly job (extend the existing 3 AM ET scheduler slot or add 3:30 ET,
  gated like other jobs by enable_scans): for rows with missing forward
  returns whose horizon has matured (trading-day aware — reuse
  `utils/market_hours.is_trading_day`), compute ret_tN = close(T+N)/entry - 1
  via the outcome-engine price cache (yfinance fallback works on the current
  FMP plan). Cap per-run work (`shadow_returns_max_per_run: int = 300`).
  Commit per row batch (lesson: no giant transactions — see BRY-300).
- Weekly report (bot/weekly_report.py): add a calibration section — count of
  matured rows, win rate (ret_t10 > 0) and median ret_t10 by score bucket
  (0.0–0.3 / 0.3–0.45 / 0.45–0.55 / 0.55–0.65 / 0.65+), same split by
  direction. Plain math, no LLM. This is the decile curve that decides all
  future threshold changes.

## I2. Paper autonomy sandbox

- New settings: `auto_approve_paper: bool = False` (master switch),
  `auto_approve_min_score: float = 0.55` (memo cohort),
  `exploration_band_enabled: bool = True`,
  `exploration_min_score: float = 0.45` (band = [exploration_min,
  auto_approve_min)), `auto_max_concurrent_positions: int = 8`,
  `auto_max_new_positions_per_scan: int = 4` (memo cohort) and
  `exploration_max_new_per_scan: int = 2`, `auto_position_pct: float` —
  reuse existing sizing but allow smaller default for exploration
  (`exploration_position_pct_factor: float = 0.5`).
- **HARD SAFETY GUARD:** auto-approval must be structurally impossible outside
  paper: assert broker is the Alpaca PAPER adapter (existing paper/live flags,
  `ALLOW_LIVE_TRADING` false-path) at the auto-approve call site; if live mode
  or Robinhood is active, log + skip and Telegram-notify ONCE. Add a test that
  flips live config and proves no auto order is placed.
- Flow: after a scan completes, for candidates meeting cohort rules (score
  order, caps, not already holding the ticker, market hours not required —
  submit as the existing approval path does), submit the SAME order the human
  approve button would (reuse the approval handler's execution path — do not
  fork order logic). Tag the Trade row: `operator_notes` gets
  `AUTO:memo` / `AUTO:exploration`, and set `paper_traded=True` +
  cohort on the ledger row.
- Telegram: memos still send exactly as today (human signal unchanged), but
  auto-taken ones append "🤖 auto-executed (paper)". Exploration-band entries
  do NOT send memos (that's the point — no operator noise); they appear only
  in the scan funnel summary line and weekly report.
- Existing monitors/stops/targets/time-exits manage exits unchanged. Weekly
  report: add cohort P&L split (memo vs exploration) once trades close.
- Ops note in PR body: enable via Railway `AUTO_APPROVE_PAPER=true` after
  credits restored + one supervised scan (Bryan's release step per
  CONTRIBUTING release-steps rule).

## I3 (flagged extension — implement only if time permits, else file follow-up)

- Fast-lane: candidates whose catalyst class is time-sensitive (earnings
  reaction, FDA) MAY use `fast_lane_max_holding_days: int = 3` for the time
  exit instead of the global max, exploration cohort only. Keep trivial; skip
  if it grows.

## Out of scope
- Changing memo_threshold or any Telegram-facing gating; live trading;
  day-trading/intraday signals; RL/weight changes; deep-research threshold.

## Acceptance
1. Suite green (pytest + unittest CI parity + compileall); new tests: ledger
   row written per scored candidate incl. failure isolation; nightly job
   computes returns only for matured horizons (trading-day aware) and is
   idempotent; escalation + catalyst caps respect priority order; auto-approve
   places orders only in paper (live-config test proves refusal); cohort
   caps enforced; weekly report renders calibration buckets from seeded rows.
2. Local end-to-end proof with mocked scan: N scored candidates → N ledger
   rows → seeded prices → nightly job fills returns → weekly report shows
   buckets; auto-approve submits ≤ caps via the mocked broker.
3. PR body: funnel summary sample line, ledger schema, safety-guard test
   output, ops enable runbook.
