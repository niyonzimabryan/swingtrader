# Swing Trader — Public Project Ledger

> Public-safe running list of release tasks, follow-ups, and ideas.
> Private operator notes should live outside the tracked repo.

## Current Tasks

- [ ] **Run scoring parity eval once trace corpus clears N_min=150** (`BRY-243`, `t_b08f0ee7`) — model-eval adapter is live (`evals/`), pulls the scoring corpus from Langfuse traces. Only ~11–19 scoring calls exist so it's UNDERPOWERED for now; when the bot has ≥150 scoring calls across ≥2 regimes, run `python -m evals.run_evals scoring --from-traces <ts> --candidate claude-sonnet-5` to test Opus→Sonnet-5. Report-only.
- [ ] **End-to-end paper drill** — Run a scheduled or manual scan, approve one Alpaca paper trade, and verify order submission plus monitor reconciliation.
- [ ] **Onboarding doctor with private keys** — Run `python -m scripts.doctor --skip-live` from a populated local `.env`.
- [ ] **Robinhood review-only smoke test** — Bootstrap OAuth, run `/broker accounts`, select a dedicated Agentic account, and verify review-only order flow before live mode.

## Completed

- [x] **Model-eval adapter (Problem B)** — `evals/` adapter shipped via PR #19 (merged 2026-07-01). Pulls the scoring corpus live from Langfuse traces (already captured by OTEL — no bot change/deploy needed), scoring/filter TaskSpecs, and a P&L rollback monitor. Report-only; not imported by the bot runtime.
- [x] **Open-source baseline docs** — Added MIT license, financial disclaimer, contributing guide, README setup path, CI, and secret-scan workflow.
- [x] **Paper-first broker safety** — Kept Alpaca paper as the default broker and gated live trading behind explicit config.
- [x] **Robinhood broker option** — Added Robinhood MCP broker, Telegram broker/mode controls, micro-trading caps, review-first flow, and audit events.
- [x] **Robinhood OAuth store** — Added encrypted MCP SDK token storage plus bootstrap/status commands.
- [x] **Public repo hygiene** — Removed private handoff docs from tracked files and kept local archived copies under ignored `.claude/private_docs/`.

## Future Improvements

- [ ] **Interactive Brokers support** — Evaluate after the Robinhood path is stable and documented.
- [ ] **Database migrations** — Replace lightweight `create_all()`/inline migrations with Alembic before schema churn grows.
- [ ] **Email backup channel** — Add a secondary memo/alert delivery path for Telegram outages.
- [ ] **Backtest framework** — Replay historical candidates through the pipeline to calibrate scoring before wider live use.
- [ ] **Batch approval UX** — Decide whether scheduled scan memos should queue for a morning review workflow.

## Tech Debt & Bugs

- [ ] **Run logging and cost tracking** — Persist per-scan token/cost/duration metrics beyond provider dashboards.
- [ ] **Deep research polling cleanup** — Continue hardening persistent-error handling and event-loop cleanup warnings in tests.
- [ ] **Legacy Reddit surface** — Remove or fully retire old Reddit-related files if web research remains the replacement.

*Last updated: 2026-06-10*
