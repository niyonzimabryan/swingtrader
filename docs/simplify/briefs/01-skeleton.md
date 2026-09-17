# Brief 1 — skeleton of the research repo

Branch `claude/skeleton` off `main` of `<RR>` (empty repo).

## Goal
The repo a research-and-teaching session opens: instructions, formats, the
three subagent briefs, the learning workspace, one-job CI.

## Contract
1. `AGENTS.md`, imported by `CLAUDE.md` line 1 (`@AGENTS.md`). Keep from
   swingtrader's AGENTS.md (read it at `niyonzimabryan/swingtrader` main):
   sourced facts only, unsourced recall is not evidence; the critic is never
   asked to be balanced; journal every decision including passes; write
   invalidators before conviction; untrusted-document rule (§5 there); not a
   licensed advisor, say so when asked for advice. Drop: the tool table,
   scopes, the four non-negotiables, budgets, SetupSpecs, cohorts, MCP.
   Add: the session loop — recall (`research/INDEX.md`, the dossier,
   `learning/learning-records/`) → frame → gather (edgar CLI first, an
   AlphaSense report in `sources/` if present, web last) → attack (critic) →
   situate (`portfolio/snapshot.json` if present) → record. Add the teaching
   rules from PLAN.md §6 verbatim in spirit: mission-tied, ZPD from the
   records, at most one lesson per session and only when the research hit
   it, knowledge short and cited, skill effortful, reference docs over
   lessons. Under 250 lines.
2. `research/INDEX.md` (empty table: ticker, status, last touched, thesis in
   one sentence), `research/_template/{dossier,thesis,invalidators}.md`,
   `journal.md` with the entry format.
3. `learning/` per the `teach` skill: `MISSION.md` (draft it: read a 10-K and
   a business model well enough to form and defend a thesis; owner edits),
   `RESOURCES.md` (empty, with the format), `NOTES.md`, `learning-records/`,
   `lessons/`, `reference/`, `assets/` with one shared stylesheet. Copy the
   three `*-FORMAT.md` files from the skill into `learning/` so the repo is
   self-describing without the skill installed.
4. `.claude/agents/{company-researcher,thesis-critic,filings-analyst}.md`,
   adapted from swingtrader's: tools = `Read, Grep, Glob, Bash, WebSearch,
   WebFetch` (Bash is for the edgar CLI); `thesis-critic` on `opus`, the
   others on `sonnet`; `maxTurns` kept; no MCP tools; no `Agent`.
5. `.claude/skills/edgar/SKILL.md` and `.claude/skills/rh/SKILL.md` as stubs
   that say "lands in PR 2 / PR 3" so nothing invokes a missing CLI.
6. `README.md` first line: "A research partner that teaches while it works.
   Currently scoped to public companies." Then the loop in ten lines.
7. CI: one job, ubuntu, Python 3.12: `pip install -r requirements.txt`,
   `compileall`, `unittest discover`. `concurrency` without
   `cancel-in-progress` on main. `requirements.txt` = `httpx`, `pyyaml`.
8. Tests: `tests/test_layout.py` asserts the files above exist and that no
   agent brief lists an MCP tool or `Agent`.

## Acceptance
CI green; `AGENTS.md` under 250 lines; a fresh session can read `AGENTS.md`
and know what to do next with no other file.

## Off limits
Nothing else is in flight in this repo.
