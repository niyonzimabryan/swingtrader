# Worker briefs

One brief per PR. `<RR>` = the research repo (`niyonzimabryan/research-bench`).
Standing rules for every worker: read the target repo's AGENTS.md / README
first; commit and push after each coherent unit; never weaken a test; no
secrets in the repo; open the PR as soon as the work is pushed and validated
locally, then stop — no check-ins, no babysitting. Python 3.12. Validation:
`python -m compileall -q .` and `python -m unittest discover -s tests`.
PR body: contract items ticked, what was exercised and what was not, anything
touched outside scope.

| # | Brief | Repo | Model | Depends on |
|---|---|---|---|---|
| 1 | `01-skeleton.md` | `<RR>` | sonnet, opus pass on AGENTS.md | repo exists |
| 2 | `02-edgar-cli.md` | `<RR>` | sonnet | 1 merged |
| 3 | `03-rh-snapshot.md` | `<RR>` | sonnet | 1 merged; owner runs auth |
| 4 | `04-alphasense-skill.md` | `<RR>` | sonnet | 1 merged; owner supplies skill |
| 5 | `05-research-export.md` | `<RR>` | sonnet | 1 merged; local-agent step 4 done |
| 6 | `06-freeze.md` | `swingtrader` | sonnet | none |
