# Brief: a memory-bounded bulk backfill (`--bulk years=10` without killing the bot)

Branch: `claude/bulk-backfill-streaming`. Model: sonnet.

## The problem, precisely

`SharadarPricePlane.load_bulk_bars` → `_read_bulk_csv` (`data/prices/sharadar.py`
~L404–470) reads the whole-market `stocks` zip into a `dict[ticker, list[dict]]`,
then builds a `DailyBar` per row, then `with_derived_series` per ticker: at least
two full in-memory copies of ~10 years × ~10k tickers. Instrumented in production
it reached 3.4 GB RSS 15 s in and the platform SIGKILLed the bot container three
times (`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md`
§4). `--tickers` does not help because `backfill_bulk` filters after the parse.
Only the ten tickers in `historical_events` are loaded today, via the slice path.

## What to build

1. **Stream, stage, then per-ticker derive.** Replace the dict-of-everything
   with: iterate the CSV inside the zip row by row (`zipfile` + `csv.DictReader`
   already stream; the accumulation is the problem), validate the header once,
   and write rows into an on-disk SQLite staging file (`tempfile`, one table,
   index on `ticker`) in batches of ~50k. Then iterate distinct tickers, load
   one ticker's rows, apply factors (the `actions` zip is ~5 MB and may stay in
   memory, grouped by ticker), build bars, `with_derived_series`, remap the
   placeholder uid through the security master, `upsert_bars` in one
   transaction per ticker, and drop the staged rows. Peak memory is one
   ticker's history plus the batch buffer. Keep `load_bulk_bars` as a thin
   wrapper over the streaming path for callers that want the dict (tests), but
   `backfill_bulk` must not call it.
2. **`--tickers` filters during staging**, so a subset run never touches the
   rest of the file.
3. **Resumable.** Write progress (ticker done, bars written) to the summary and
   to a small checkpoint file next to the staging DB; `--resume <path>` skips
   finished tickers. A killed run must be continuable, not restarted.
4. **A memory ceiling that is measured, not assumed.** Add a
   `--max-rss-mb` guard (default 1500) that samples `resource.getrusage` between
   tickers and aborts cleanly with a checkpoint if exceeded. Add a test that
   generates a synthetic bulk zip of, say, 2,000 tickers × 2,500 sessions
   (~5M rows, written by a generator, never held in memory) and asserts the
   streaming path's peak RSS stays under a bound while the old path's would
   not (mark the old-path comparison `@skip` unless `SLOW_TESTS=1`; keep the
   streaming assertion in the default suite with a smaller file and a
   `tracemalloc` peak bound).
5. **Run it off the bot.** Document (and add a `railway.jobs.md` or a section
   in `docs/OWNER_SETUP.md` §4) the way to run the backfill without bouncing
   the trading process: `railway ssh --service swingtrader -- python -m
   scripts.price_backfill --bulk years=10 --max-rss-mb 1500` is what exists
   today and it shares the container's cgroup; state plainly whether the
   streaming path makes that safe (measure locally at ~1 GB and say so) and,
   if not, what a one-off Railway service would need. Do not create Railway
   resources.
6. **Docs**: `docs/PRICE_PLANE.md`, `docs/ENV_SETUP.md` §7, the module
   docstring of `sharadar.py` (the bulk section), `docs/OWNER_SETUP.md` §4.

## Tests

Streaming parse equals the old parse on `tests/fixtures/sharadar_direct/`
(bar-for-bar); staging survives a simulated kill and `--resume` finishes with
identical rows; `--tickers` never stages other tickers; header validation
errors still raise `PricePlaneSchemaError`; the RSS guard aborts with a
checkpoint; the existing bulk tests (`test_bulk_download_streams_the_zip_to_disk`
and friends) untouched.

## Not in scope

Funds/SFP (separate brief); changing what a bar is; the security-master
resolution rules.
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
