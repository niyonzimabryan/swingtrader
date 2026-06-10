# Swing Trader — Public Project Ledger

> Public-safe running list of release tasks, follow-ups, and ideas.
> Private operator notes should live outside the tracked repo.

## Current Tasks

- [ ] **End-to-end paper drill** — Run a scheduled or manual scan, approve one Alpaca paper trade, and verify order submission plus monitor reconciliation.
- [ ] **Onboarding doctor with private keys** — Run `python -m scripts.doctor --skip-live` from a populated local `.env`.
- [ ] **Robinhood review-only smoke test** — Bootstrap OAuth, run `/broker accounts`, select a dedicated Agentic account, and verify review-only order flow before live mode.

## Completed

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
