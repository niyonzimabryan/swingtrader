# Audit remediation spec pack — 2026-07-04

Source of truth for findings: `docs/audits/2026-07-04-system-audit.md` (read it
first — it has evidence, log lines, and root-cause analysis for every item).

## Work packages

| Spec | Priority | Scope | Depends on |
|------|----------|-------|------------|
| [A-pattern-engine-rescue.md](A-pattern-engine-rescue.md) | P0 | Analog engine outcome join, backfill queue/consumer, grounding+PIT guards, FMP endpoint, typed statuses | — |
| [B-production-reliability.md](B-production-reliability.md) | P0/P1 | Monitor hang fix (timeouts, watchdog, holidays), scan lock, error→Telegram, LLM retries | — |
| [C-tier2-screener-repair.md](C-tier2-screener-repair.md) | P0 | Gemini tier-2 JSON truncation, parse-rate metric | — |
| [D-broker-and-db-integrity.md](D-broker-and-db-integrity.md) | P1 | Bracket-order qty conflict, position-not-found reconcile, SQLite WAL | — |
| [E-hygiene-sweep.md](E-hygiene-sweep.md) | P2 | utcnow(), DB indices, reddit table drop, .env.example, test rot | A–D merged (touches same files) |
| [F-tracker-cleanup.md](F-tracker-cleanup.md) | P2 | Linear/Hermes/scratchpad reconciliation (no code) | — |
| [G-observability.md](G-observability.md) | P2 | Ad-hoc stage tags (BRY-243 corpus), Gemini call ledger | — |
| [H-structured-event-backfill.md](H-structured-event-backfill.md) | P1 | Bulk pattern-library warm-up from FMP structured data (earnings, upgrades) | A merged |
| [I-flywheel-shadow-ledger-paper-autonomy.md](I-flywheel-shadow-ledger-paper-autonomy.md) | P1 | Funnel caps/telemetry, shadow calibration ledger, paper auto-approval sandbox | A–H merged; enable after credits restored |

A, B, C, D are independent — safe to run in parallel on separate branches.
E must run **after** A–D merge (it edits `execution/` files B and D touch).

## Ground rules (all packages)

- **Repo mode: production** — surgical, conservative changes. Every changed
  line traces to the spec. No drive-by refactors.
- **Branch naming:** `claude/audit-<letter>-<slug>` (e.g. `claude/audit-a-pattern-rescue`).
  One PR per spec, targeting `main`. Reference the spec file in the PR body.
- **NEVER run `python main.py` locally** — Telegram allows one polling
  connection and Railway holds it. Tests and scripts only.
- **Tests:** CI runs `python -m unittest discover -s tests -p "test_*.py"` on
  Python 3.12 plus `compileall`. Also run `python -m pytest tests -q` (147
  currently green — keep it green). Deps: `.venv` at repo root (populated via
  `uv pip install --python .venv/bin/python -r requirements.txt pytest`).
- **Env:** repo `.env` has Anthropic/Gemini/DB/Langfuse-URL; full keys live on
  Railway. Never commit secrets. Anything needing prod (Railway CLI, prod DB)
  is an **ops step** — do the code, document the command, do NOT execute
  against production unless the spec explicitly says so.
- **Config changes:** new settings go in `config/settings.py` with defaults +
  `.env.example` entries with a comment.
- **Done =** acceptance criteria in the spec verified and shown in the PR body
  (test output, script output). "Should work" is not done.

## Ops decisions held by Bryan (not in any package)

1. Restart the hung Railway service (audit P0-1 immediate action).
2. Re-enable `SCHEDULER_ENABLED=true` after A + B land (unblocks the scoring
   corpus → BRY-243 → PR #18 model swaps).
3. Run the one-time production backfill after A merges (command in spec A).
4. BRY-97 email-backup fate; PR #20 merge; Alembic go/no-go (BRY-107).
