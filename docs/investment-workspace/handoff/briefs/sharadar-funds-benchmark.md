# Brief: funds (SFP) support in the Sharadar adapter, so SPY can be the benchmark

Branch: `claude/sharadar-funds-benchmark`. Model: opus.

## The problem, precisely

`data/prices/sharadar.py` is equities-only by construction: `security_master`
sends `table=stocks` to `tickers`, `daily_bars` reads `TABLE_STOCKS`, and the
bulk loader parses the `stocks` zip. Sharadar keeps funds in a separate table
(`funds`, legacy `SFP`): SPY's only `tickers` row is `table=funds` (permaticker
118691), `stocks?ticker=SPY` is empty, `funds?ticker=SPY` returns bars
(`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md` §4).
So `COMPARABLE_BENCHMARK_SECURITY_UID` cannot be set, `CohortContext` refuses
to build (`comparables/cohort.py:136`), `scripts/cohort_smoke.py` cannot run,
and the whole comparable-setups engine (Spec N) is dark on real data. A
stand-in equity benchmark was correctly refused: a cohort answer against a
benchmark that is not the benchmark is a wrong number (Spec N §5.2).

## What to build

1. **`asset_class` on securities.** Add `asset_class` (`equity` | `fund`,
   default `equity`) to `SecurityMasterRow` (`data/prices/base.py`), the
   `Security` model, `store.upsert_securities`, and an Alembic revision
   `0014_securities_asset_class` off `0013_merge_notify_owner` (nullable-free
   with a server default so existing rows read `equity`; `securities` is a
   Phase 3p table, not baseline-era, so a column is safe — see
   `migrations/README.md` on the baseline trap and confirm with
   `tests/test_schema_discipline.py`).
2. **Funds in the adapter.** `security_master(tickers, *, asset_class="equity")`
   queries `tickers?table=funds` for funds; `daily_bars` picks `funds` or
   `stocks` from the security's asset class (resolve through the master, never
   by guessing from the symbol); `corporate_actions` for a fund reads
   `actions` exactly as for an equity **if and only if** the vendor documents
   fund distributions there — read https://sharadar.com/docs/funds and
   https://sharadar.com/docs/actions (public pages; `docs/vendors/sharadar.md`
   is the local reference) and write down what you found. The Spec N §4.3
   three-series contract (raw, split-adjusted, total-return with factors by
   ex-date) must hold for a fund exactly as for an equity, because the
   benchmark comparison is total-return vs total-return (Spec N §4.3, §5.2):
   if `funds` carries `closeadj` but `actions` carries no fund distributions,
   derive the dividend factor from `closeadj/close` per session and say so in
   the provenance rather than storing a price-return benchmark and calling it
   total-return. The `reconstruction identity` check in
   `data/prices/derived.py` must pass for a fund series.
3. **Funds are never universe members.** `data/prices/universes.py` and every
   rank-by-liquidity rule must filter `asset_class == "equity"`; assert it. A
   benchmark ETF that sneaks into "liquid US equities" would be a cohort
   member and a benchmark at once. The `comparables/cohort.py` build already
   pops the benchmark uid from `bars_by_uid`; keep that and add the class
   filter upstream.
4. **`scripts/price_backfill.py --tickers SPY --asset-class fund`** (or
   auto-detect from `tickers` when the symbol is absent from `stocks`): loads
   the master row and the bars for a fund through the slice path. Print the
   resulting `security_uid` so the owner can set
   `COMPARABLE_BENCHMARK_SECURITY_UID`. Add `scripts/benchmark_uid.py` (or a
   flag on the backfill) that prints the uid for a given fund ticker from the
   stored master.
5. **Fixtures.** The public `test-api-key` may not cover `funds`. Record what
   it does return under `tests/fixtures/sharadar_direct/` (payload + the
   `schema_funds.sql` DDL from the docs); where the live payload cannot be
   recorded, build the fixture from the documented schema and mark it
   `unverified_live: true` in the fixture README so the owner's terminal
   agent can replace it with a real capture. Never invent a number in a
   fixture that a test asserts on as a *value*; assert on shape and on the
   reconstruction identity instead.
6. **Docs**: `docs/PRICE_PLANE.md`, `docs/ENV_SETUP.md` (§7 benchmark step
   becomes concrete: backfill SPY as a fund, read the uid, set the variable,
   run `cohort_smoke`), `docs/OWNER_SETUP.md` §4, `docs/vendors/sharadar.md`
   header (a line on SEP vs SFP), Spec N §12 ruling: benchmark is a fund
   series from SFP, funds excluded from universes, how total-return was derived.

## Tests

Master rows for a fund and an equity with the right class; `daily_bars` for a
fund hitting `funds` not `stocks`; the three-series identity on a fund with a
distribution; universes excluding funds; migration round trip; `cohort_smoke`
against the fixture plane with a fund benchmark producing one `ok` and one
`insufficient` (Spec N §11); existing Sharadar tests untouched.

## Not in scope

The bulk path for funds (a separate brief covers bulk); any change to cohort
math; changing the audit list.
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
