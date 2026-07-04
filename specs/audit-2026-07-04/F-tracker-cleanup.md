# Spec F — Tracker & process cleanup (P2-4) — no code changes

Read first: `docs/audits/2026-07-04-system-audit.md` (§P2-4) and the
`linear-kanban` skill conventions. This package touches Linear, Hermes, GitHub
PR #20, and `todoscratchpad.md` — NOT application code.

## Tasks

### F1. Land PR #20 (codex scratchpad reconcile)
- Review draft PR #20 (`codex/reconcile-swingtrader-scratchpad`) — it
  restructures `todoscratchpad.md` into an open prioritized list. Verify its
  claims against current reality (several changed since it was drafted:
  the audit doc now exists; pattern-engine work is specced in
  `specs/audit-2026-07-04/`). Update the branch to reference the audit +
  spec pack, mark PR ready, merge. If fundamentally stale, close it with a
  comment and rewrite `todoscratchpad.md` directly on a fresh branch instead.

### F2. Linear reconciliation (team BRY)
- **BRY-237** (Reddit retirement): shipped at commit `465c835` on main;
  Linear stuck "In Review". Move to Done with a comment linking the commit.
- **Stale May duplicates**: BRY-15, BRY-16, BRY-17, BRY-18, BRY-22 — verify
  each against its newer counterpart (BRY-16≈BRY-182 Done, BRY-17≈BRY-181
  Done, BRY-18≈BRY-106 open, BRY-22≈BRY-107 open) and cancel/duplicate-link
  them with a one-line comment each.
- **File new issues from the audit** (one per spec A–E, linking the spec file
  path and audit section; set priority to match P0/P1/P2). Skip any that
  already exist — search first.
- **BRY-243** (scoring parity eval): add a comment that the corpus is 22/150
  as of 2026-07-04 and is gated on `SCHEDULER_ENABLED=true` (audit §P0-3), so
  its Hermes card should stay blocked and stop being retried (see F3).

### F3. Hermes hygiene (`~/.hermes`)
- 5 swingtrader cards (t_04e108a3, t_1bdee59c, t_df750a07, t_cf765615,
  t_b2db97d7) are blocked solely on worker crashes ("pid not alive" ×2 each) —
  clear the stale blocked state so they're workable once the worker is fixed,
  or annotate them as infra-blocked (follow whatever status conventions the
  kanban.db/CLI supports — inspect `~/.hermes` structure first; do not
  hand-edit sqlite without understanding the schema).
- t_b08f0ee7 (BRY-243): ~20 stale-lock reclaims + a phantom "running" run on a
  data-gated card — clear the phantom run and ensure the dispatcher won't
  churn it (mark blocked/on-hold per convention).
- **Do not attempt to fix the Hermes worker daemon itself** — report its crash
  evidence (log paths, pids) as a summary for Bryan; that's a separate
  ~/.hermes project decision.

### F4. Orphaned work inventory (report, don't decide)
- **BRY-97 email backup**: locate the parked patch/implementation (Hermes
  comments say it was backed up outside the repo), confirm it still exists,
  summarize the 4 review blockers, and present options (fix-and-merge
  estimate vs. drop) — Bryan decides.
- **BRY-60**: local branch `hermes/806b113c` commit `2dd63ba` + patch
  `.hermes/patches/t_806b113c.patch` — verify both still exist, push the
  branch to origin as backup if it only exists locally, note it on the Linear
  issue.

### F5. Process rule
- Add a short "Release steps" section to `CONTRIBUTING.md` (or the scratchpad
  if CONTRIBUTING is public-facing and this is operator-specific): feature-flag
  flips on Railway are release steps — record who/when/pre-flip evidence in
  the tracker. Cite the pattern-engine incident (flag enabled without the
  spec-mandated backfill+bakeoff gate) in one neutral sentence.

## Acceptance criteria
- PR #20 merged or closed-with-replacement; `todoscratchpad.md` on main
  reflects reality (audit + spec pack referenced, stale items gone).
- Linear: BRY-237 Done; the 5 May dupes closed; new audit issues filed and
  linked; BRY-243 annotated.
- Hermes: no phantom running runs; blocked-reason annotations accurate.
- A short final report: what was changed where, plus the BRY-97/BRY-60/worker
  findings for Bryan's decisions.
