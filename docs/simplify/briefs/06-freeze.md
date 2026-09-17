# Brief 6 — freeze swingtrader

Branch `claude/freeze` off `main` of `niyonzimabryan/swingtrader`.

## Goal
Make the repo an honest, readable portfolio piece and make any future push
cheap. No code deleted.

## Contract
1. Tag `workspace-final` at the current `main` (the PR body says the tag is
   the owner's to push if the worker cannot).
2. `README.md` rewritten, under 150 lines: what it is (the scan → agents →
   score → Telegram digest pipeline, paper on Alpaca; the investment-workspace
   layer, Specs K–Q); what worked; what was over-built and why (one honest
   paragraph — velocity without a stopping rule); what I would do differently;
   status: frozen, not maintained, successor is `<RR>`; how to run the legacy
   bot in one screen. Link `docs/SYSTEM_OVERVIEW.md` for the rest.
3. Root tidy: move `todoscratchpad.md`, `ARCHITECTURE_EVOLUTION_TRIGGERS.md`,
   `swing-trader-prd.md` under `docs/archive/`; fix any relative links.
4. CI: delete `attestation-check.yml` and `model-lint.yml` and their vendored
   checkers (`tools/`, `evals/`) only if no test imports them — otherwise
   delete the workflows alone. `ci.yml`: SQLite only, `SHARD_COUNT: 1`,
   remove `test-count-check`, `cancel-in-progress: false` on `main`. Keep
   the secret scan.
5. `docs/simplify/` stays as the record of the decision.

## Acceptance
CI green on the trimmed workflow; README reads as judgment, not apology.
