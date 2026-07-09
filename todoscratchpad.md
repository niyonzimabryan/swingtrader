# Swing Trader — Public Project Ledger

> Public-safe running list of release tasks, follow-ups, and ideas.
> Private operator notes should live outside the tracked repo.

> **2026-07-04 system audit:** findings + evidence in
> `docs/audits/2026-07-04-system-audit.md`; remediation specs in
> `specs/audit-2026-07-04/` (A–G). P0/P1 packages A–D shipped (PRs #21–24),
> P2 hygiene E in review (PR #25). This ledger reflects post-remediation state.

## Open Prioritized List

- [ ] **P0 (ops) — Re-enable `SCHEDULER_ENABLED=true` on Railway** (audit §P0-3) — scans are off in prod, so no memos and the scoring corpus can't grow. Safe now that A (pattern engine) + B (scan lock + hang recovery) shipped. Record the flip as a release step (who/when/pre-flip evidence).
- [ ] **P0 (ops) — Run the one-time production backfill** (audit §P0-2, spec A) — `railway run python -m scripts.backfill_historical_events` then `backfill_event_outcomes.py` / `backfill_event_contexts.py`, then re-run `scripts/evaluate_pattern_analog_engine.py` as the bakeoff acceptance gate.
- [ ] **P1 — Finish public launch assets, README hero, and social preview cleanup** (`BRY-235`, `t_04e108a3`, blocked) — launch assets still local/untracked; README/social-preview cleanup not shipped. Hermes card is infra-blocked (worker crash, `pid not alive` ×2), not substance-blocked.
- [ ] **P1 — Run production ops smoke with private keys and Robinhood tiny caps** (`BRY-236`, `t_1bdee59c`, blocked) — `scripts.doctor --skip-live`, Telegram broker/account/mode checks, Robinhood review-only/tiny-cap validation; blocked on private credentials + Hermes worker crash.
- [ ] **P2 — Populate PipelineRun token and cost attribution** (`BRY-106`, `t_df750a07`, blocked) — cost/token attribution still unshipped; Hermes infra-blocked. Relates to G-observability (Gemini call ledger).
- [ ] **P2 — Add Alembic/Postgres migration plan before more schema churn** (`BRY-107`, `t_cf765615`, blocked) — decision still pending. Spec E added indices + the `reddit_sentiment` drop via the existing inline `create_all()` migration mechanism; the pattern engine added 6 tables the same way, so the Alembic trigger is closer.
- [ ] **P2 — Email backup channel** (`BRY-97`, `t_b2db97d7`, blocked) — reconciled 2026-07-03/07-08: no open PR, no merged implementation, `origin/main` has no `EmailBackupChannel`/`email_backup`/SMTP/Resend code beyond PRD mentions. A full implementation reportedly exists as a parked patch outside the repo that failed review (4 blockers) — fix-and-merge vs. drop is Bryan's call.
- [ ] **P3 — Add own trade history to pattern evidence after 30+ closed trades** (`BRY-60`, `t_806b113c`, blocked) — implementation exists only as a local patch handoff (`.hermes/patches/t_806b113c.patch`, branch `hermes/806b113c`); private evidence must stay aggregate-only.
- [ ] **P3 — Tune scoring weights from real closed-trade data after 50+ trades** (`BRY-96`, `t_a6fac794`, blocked) — wait for 50+ closed trades, then rebalance only if attribution evidence supports it.
- [ ] **P4 — Run scoring parity eval once trace corpus clears N_min=150** (`BRY-243`, `t_b08f0ee7`, blocked) — SCORING tier only (Opus→Sonnet-5). Corpus is 22 tagged / 29 actual scoring calls as of 2026-07-04 (7 ad-hoc calls untagged — fix specced in `specs/audit-2026-07-04/G-observability.md`; ad-hoc scoring counts toward the corpus). Growth is gated on `SCHEDULER_ENABLED=true`, so the card stays blocked until scans resume. Analyst/discovery-tier `sonnet-4-6`→`sonnet-5` is a separate approved upgrade, NOT gated on this.

## Completed

- [x] **Audit A — Pattern engine rescue** (`PR #23`, audit §P0-2/P1-1/P1-2/P1-7) — LEFT-JOIN outcomes, backfill queue→volume + consumer, grounding/PIT guards, FMP `company-screener` endpoint, typed discovery/pattern statuses.
- [x] **Audit B — Production reliability** (`PR #22`, audit §P0-1/P1-3/P1-6) — monitor broker-call timeouts, watchdog self-restart, market-holiday calendar, scan mutual-exclusion lock, scan-failure → Telegram, LLM retries, daily pre-market self-restart.
- [x] **Audit C — Tier-2 Gemini screener repair** (`PR #24`, audit §P0-4) — token-budget fix for mid-JSON truncation + robust extraction (structured output incompatible with Search grounding), parse-rate metric.
- [x] **Audit D — Broker & DB integrity** (`PR #21`, audit §P1-4/P1-5) — bracket-order qty conflict handling, position-not-found reconcile, SQLite WAL + busy_timeout.
- [x] **Audit E — Hygiene sweep** (`PR #25`, in review, audit §P2-1/2/3) — `datetime.utcnow()` → naive-UTC helper, `Query.get()`→`Session.get()`, event-loop test helper, DB indices, dropped orphaned `reddit_sentiment` table, `.env.example` completeness + env-split docs, Gemini model startup log. Warnings 342→1.
- [x] **Pattern-library API warm-up** (`spec H`) — FMP earnings/upgrades bulk load to seed the analog store API-first.
- [x] **Model-eval adapter (Problem B)** — `evals/` adapter shipped via PR #19 (merged 2026-07-01). Pulls the scoring corpus live from Langfuse traces; scoring/filter TaskSpecs + P&L rollback monitor. Report-only; not imported by the bot runtime.
- [x] **Legacy Reddit surface retired** (`BRY-237`, `t_ce9d1ea0`) — shipped on `origin/main` at `465c835`: deleted runtime Reddit agent/data modules, removed Reddit env/settings/onboarding surface, added `tests/test_legacy_reddit_retired.py`. The orphaned `reddit_sentiment` table was fully dropped in Audit E (PR #25).
- [x] **Open-source baseline docs** — MIT license, financial disclaimer, contributing guide, README setup path, CI, secret-scan workflow.
- [x] **Paper-first broker safety** — Alpaca paper is the default broker; live trading gated behind explicit config.
- [x] **Robinhood broker option** — Robinhood MCP broker, Telegram broker/mode controls, micro-trading caps, review-first flow, audit events.
- [x] **Robinhood OAuth store** — encrypted MCP SDK token storage plus bootstrap/status commands.
- [x] **Public repo hygiene** — removed private handoff docs from tracked files; local archived copies under ignored `.claude/private_docs/`.

## Future Improvements

- [ ] **Interactive Brokers support** — evaluate after the Robinhood path is stable and documented.
- [ ] **Backtest framework** (`t_5333196b`, no Linear mirror, triage) — replay historical candidates through the pipeline to calibrate scoring before wider live use.
- [ ] **Batch approval UX** (`t_459e910e`, no Linear mirror, triage) — decide whether scheduled scan memos should queue for a morning review workflow.

## Tech Debt & Bugs

- [ ] **Run logging and cost tracking** — persist per-scan token/cost/duration metrics beyond provider dashboards (see G-observability Gemini call ledger + `BRY-106`).
- [ ] **Ad-hoc scoring stage tags** — 7 ad-hoc scoring calls are untagged and miss the BRY-243 corpus; fix specced in `specs/audit-2026-07-04/G-observability.md`.

## Process notes

**Release steps — feature-flag flips are releases, not config tweaks.** Any
Railway feature-flag flip (e.g. `PATTERN_ANALOG_ENGINE_ENABLED`,
`SCHEDULER_ENABLED`) is a release step: record who flipped it, when, and the
pre-flip evidence (backfill/bakeoff run, smoke result) in the tracker before and
after the flip. The pattern-engine went live when its flag was enabled without
the spec-mandated backfill + bakeoff gate, which is how a green test suite
coexisted with a structurally broken production feature.

*Last updated: 2026-07-08*
