# Brief: make the delisting audit resolve Sharadar's symbology and report what it could not test

Branch: `claude/delisting-audit-symbols`. Model: sonnet.

## The problem, precisely

`scripts/audit_delisting_returns.py` is Spec N §4.2's survivorship check: it
pulls the final bars of twenty known performance-related delistings
(`data/prices/delisting_audit_list.py`) and classifies each as `collapse`,
`stop`, `missing` or `too_short`. Against Sharadar it aborts on the first case
(`RSH`) because the list names companies by pre-bankruptcy symbol and Sharadar
keys them by the post-bankruptcy `Q` symbol; only 4 of 20 (`ACI, SUNE, WLL,
CBL`) resolve as written; `RSH→RSHCQ`, `SHLDQ`, `BBBYQ`, `SIVBQ`, `FTRCQ`,
`RADCQ`, `BIGGQ` were verified live; `JCP→JCPNQ` does **not** resolve, so this
is not a suffix rule. Separately, the 10-year tier starts 2016-09-12, so the
2015 cases (`RSH`, `WLT`, `ZQK`) have no bars under the purchased history even
under the right symbol (`OWNER_SETUP_EXECUTION_2026-09-12.md` §6). An audit that
cannot run is a survivorship check that silently passes — a data-correctness
bug, not a script annoyance.

## What to build

1. **Vendor symbols on the case.** `DelistingCase` gains
   `vendor_symbols: Mapping[str, str]` (e.g. `{"sharadar": "RSHCQ"}`) and
   `resolution_note: str`. Fill in the seven verified mappings above and the
   four that resolve as-is; leave the rest empty. Keep `ticker` as the
   historical symbol the case is known by.
2. **A resolver, not a guess.** `data/prices/audit.py` resolves each case
   for the plane in use: explicit `vendor_symbols[plane.source]` first; else
   `security_master([ticker])`; else, for Sharadar, `tickers` filtered by
   company name and `relatedtickers` (read `docs/vendors/sharadar.md` and
   https://sharadar.com/docs/tickers for the field) — and only accepts a
   candidate whose name matches the case's `company` under a strict
   normalisation, recording the candidate and the reason in the result. A
   case with no accepted candidate is classified **`unresolved`**, a new
   terminal class distinct from `missing`: `missing` means "the vendor has the
   symbol and no bars"; `unresolved` means "we could not even ask". Never
   let a resolution failure look like a data answer.
3. **Window awareness.** The plane reports its purchased history start
   (Sharadar: from the earliest bar of a known always-listed ticker, or a
   configured `PRICE_PLANE_HISTORY_START`); a case whose delisting date
   predates it is classified **`out_of_window`**, also distinct from
   `missing`, and the summary says "N of 20 cases are testable under this
   tier". `terminal_returns_must_be_synthesised` is computed over testable
   cases only, and the audit **refuses** (exit 2) when fewer than a
   configurable minimum (default 10) are testable — a check with n=4 is not
   a check.
4. **`--resolve` mode** on the script: prints, for every unresolved case, the
   candidates found and a ready-to-paste `vendor_symbols` line, so the owner's
   terminal agent (which has the API key) can finish the mapping in one run
   and open a follow-up PR with the filled list. Workers in the cloud have
   no Sharadar key; design for that.
5. **The stored audit blob** (`price_snapshots.delisting_audit_json`) carries
   per-case `resolved_symbol`, `resolution`, `classification`, and the
   testable count; `comparables` readers of the audit (grep for
   `delisting_audit`) must tolerate the two new classes.
6. **Docs**: `docs/PRICE_PLANE.md` audit section, `docs/OWNER_SETUP.md` §4
   (run `--resolve` first, then the audit), Spec N §12 ruling: the audit's
   classes, the testable-minimum refusal, and that under the 10-year tier
   the 2015 cases are out of window by construction.

## Tests

Fixture plane with cases that resolve explicitly, via master, via
name-match, and not at all; `out_of_window` on a pre-history case; the
testable-minimum refusal; the summary counts; the stored blob shape;
`comparables` audit readers with the new classes; the existing audit tests
untouched.

## Not in scope

Verifying the twenty cases against their Form 25 filings (still an owner
item, `docs/PRICE_PLANE.md`); buying the `full` tier.
## Worker rules (every brief)

- Read `AGENTS.md`, then `docs/investment-workspace/handoff/HANDOFF.md`, before
  writing code. The four non-negotiables are asserted by tests; never weaken a
  test to pass. `mcp` stays pinned `<2`. No secrets anywhere in the diff.
- Work on the branch named in this brief, from `origin/main` (`git fetch origin
  main` first). Commit after each coherent unit; push often.
- Validate on Python 3.12 (CI's version): `python -m compileall -q .` and
  `python -m unittest discover -s tests -p "test_*.py"` (~14 min; 3 Postgres-only
  skips are expected). CI is sharded; `scripts/test_shard_weights.json` and the
  `test-count-check` job exist — if you add test modules, run
  `python scripts/ci_shard.py --help` and follow it so the count check passes.
- Every new capability ships behind a flag defaulting **off**. Production must
  not change behaviour when this PR merges with no variable set.
- When done: open the PR against `main` with a body that states what was built,
  every design decision you made where the spec was silent, what was verified
  and how, what was NOT verified, and what you deferred and why. Then **stop**.
  Do not schedule check-ins, do not subscribe to the PR, do not merge. The
  orchestrating session reviews and merges.
- If you hit a usage limit, the orchestrator will resume you; keep the branch
  pushed so nothing is lost.
