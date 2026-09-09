# The price plane

The historical price backbone Spec N's comparable-setups engine runs on. It is
additive: `data/market_data.py` (the incumbent yfinance path) is untouched, and
nothing here runs until `PRICE_PLANE_ENABLED=true`.

- Spec: `specs/investment-workspace/N-comparable-setups-engine.md` §4.2, §4.3, §4.5
- Schema: `migrations/versions/3p01_price_plane.py`, branching from `0001_baseline`
- Licences: [`DATA_LICENSES.md`](DATA_LICENSES.md)

## What is stored

Five tables, no foreign keys crossing a phase boundary.

### `securities`
One row per **(security, ticker validity interval)**. `security_uid` is a stable
id that survives a ticker rename, so a cohort spanning one does not split into
two names. Carries `exchange` as the vendor spells it and `venue`
(`nyse_amex` / `nasdaq` / `other` / `unknown`) because the Shumway delisting
convention is keyed on the venue, not the exchange. `delisting_date` plus
`delisting_reason` — one of `performance`, `merger_acquisition`, `other`,
`unknown`. **Delisted names are kept.** Dropping them is survivorship bias by
construction (Spec N §4.2).

### `price_bars`
One row per (security, session). Unique on `(security_uid, session_date)`,
indexed on the same pair for the range scans a cohort does.

Three series per name, not two (Spec N §4.3):

| Column | What it is | What it is for |
|---|---|---|
| `raw_open/high/low/close`, `volume` | What actually printed | Mapping a fill back to a real price; dollar volume |
| `split_adjusted_close` | Raw ÷ every split ex-dated after this session | Signals, covariates, and replay — a 2-for-1 mid-hold must not fire a fixed stop |
| `total_return_close` | Split *and* dividend, back-adjusted | Benchmark comparison. A price-return stock against a total-return index is a systematic negative bias equal to the yield gap |

plus the factors that map between them: `split_factor` (the share multiplier
whose **ex-date is this session**, 1.0 otherwise) and `dividend_cash` (cash per
share, same ex-date, 0.0 otherwise).

**Conventions, written down once** (`data/prices/derived.py`):

```
F(i)                   = product of split_factor(k) for every k > i
split_adjusted_close(i)= raw_close(i) / F(i)
r(i)                   = (raw_close(i) + dividend_cash(i)) * split_factor(i) / raw_close(i-1) - 1
total_return_close     = r compounded, anchored so the last bar equals the last raw close
```

Anchoring at the **end** is the ordinary back-adjustment convention, and it has a
consequence worth knowing: adding tomorrow's bar rescales the whole history. A
stored series is therefore only valid for the snapshot it was ingested under,
which is why `price_snapshots` exists and why a cohort records the snapshot it
ran on.

Every series is checked against the reconstruction identity at ingest
(`derived.check_reconstruction`, tolerance 1e-9). A vendor whose adjusted closes
disagree with its own factors fails the backfill rather than a cohort six weeks
later.

### `corporate_actions`
Splits, dividends and delistings with their ex-dates and the source that
supplied them. Unique on `(security_uid, ex_date, action_type, source)`, so two
sources can disagree in the open.

### `universe_membership`
`(universe_slug, security_uid, ticker, member_from, member_to, source, known_at_utc)`.
Half-open: a name is a member as of `d` when `member_from <= d < member_to`, and
`member_to IS NULL` means still a member. Indexed on
`(universe_slug, member_from, member_to)` for exactly that query. `known_at_utc`
is when the fact could first have been acted on, so the Spec N §4.1 bitemporal
filter applies to membership too.

### `price_snapshots`
A named vintage: `source`, a coverage summary, and the delisting-audit result as
JSON. A cohort records which snapshot it ran on; the audit is how you know what
the snapshot's terminal returns are worth.

## Adapters

`data/prices/base.py` defines `PricePlane`: daily bars (delisted names
included), corporate actions with ex-dates, security-master rows with
listing/delisting dates and reasons, and index membership history.

Two rules every implementation obeys:

1. **Raise on an unexpected payload shape.** A renamed or missing column is an
   error. A price plane that degrades quietly is worse than one that is down,
   because the cohort statistics it feeds look fine.
2. **Never write nulls.** Every field on a `DailyBar` is required and finite.

### `FixturePricePlane`
Backed by the committed CSVs in `tests/fixtures/prices/` — synthetic, offline,
no key. Everything in the test suite runs against it, and so does
`--source fixture` on every script.

### `SharadarPricePlane`
Against Nasdaq Data Link's tables API: **SEP** (prices), **ACTIONS** (corporate
actions), **TICKERS** (security master), **SP500** (constituent history).

Uses plain `httpx` — already a pinned dependency, so it adds none — rather than
the `nasdaq-data-link` SDK. The SDK returns a pandas DataFrame, which has
already coerced the payload: a renamed column becomes a `KeyError` somewhere
downstream and a null becomes a `NaN` that flows into a stored bar. Reading the
raw JSON is what lets the adapter compare the returned column list against the
one it was written against, before a single value is parsed.

**Two verified column semantics are load-bearing:**

- SEP's `open`/`high`/`low`/`close` are adjusted for splits and stock dividends;
  `closeunadj` is not. So the **raw** OHLC is reconstructed exactly as
  `field × (closeunadj / close)`, and the raw close is `closeunadj` itself.
- `closeadj` is Sharadar's own total-return close. **This adapter does not store
  it.** The stored total-return series is derived from the raw closes and the
  stored factors, so the §4.3 identity holds against the factors actually kept;
  Sharadar's anchoring and its spinoff handling are both unverified, and a
  series that cannot be reproduced from stored factors is not inspectable.

**TICKERS carries no delisting-reason field.** The reason is derived from the
ACTIONS `action` value where one exists and is `unknown` otherwise — which Spec N
§4.4 treats as *censored* rather than matured, i.e. it does not enter a headline
mean. `delisting_date` uses `lastpricedate` as a proxy, and is labelled as one.

#### How the column names were verified — read this before trusting them

This session's egress proxy blocks `data.nasdaq.com`, `sharadar.com`,
`quantrocket.com` and `flounderteam.github.io`, which is the same failure that
left verification Claim 7 `UNVERIFIED` across the board. The column names above
come from **search extracts, not fetched pages** — `[S]`, not `[V]`, in the
README §5 convention. Pages the extracts were drawn from:

- `https://data.nasdaq.com/databases/SEP` — Sharadar Equity Prices: `closeadj`
  ("adjusted for stock splits, stock dividends, cash dividends and spinoffs"),
  `closeunadj` ("unadjusted … does not apply corporate adjustments"),
  `open/high/low/close/volume` ("adjusted for stock splits and stock dividends
  but not for cash dividends or spinoffs"), `lastupdated`, `ticker`, `date`.
- `https://www.quantrocket.com/sharadar/` — the four datasets and their purpose;
  TICKERS fields `permaticker`, `exchange`, `isdelisted` (Y/N), `category`,
  `firstpricedate`, `lastpricedate`.
- `https://sharadar.com/` and `https://sharadar.com/prices` — the Prices tier's
  contents; ACTIONS columns `date, action, ticker, name, value, contraticker,
  contraname`; SP500 as current constituents plus additions/deletions with
  effective dates plus quarterly snapshots.

The adapter treats its column list as **a hypothesis it checks**, not as a fact:
`_rows` raises `PricePlaneSchemaError` naming the absent column. Extra columns
are tolerated on purpose — a vendor adding a field should not stop an ingest; a
rename must.

**Not exercised against a live key. No key exists.** Everything above was
tested against stub payloads.

## Running it

### Environment

| Variable | Default | What it does |
|---|---|---|
| `PRICE_PLANE_ENABLED` | `false` | Master flag. Every script refuses while it is false. |
| `PRICE_PLANE_SOURCE` | `fixture` | `fixture` or `sharadar`. |
| `NASDAQ_DATA_LINK_API_KEY` | *(empty)* | Sharadar key. Never hardcoded, never logged; the Sharadar plane refuses to construct without it rather than calling anonymously. |
| `PRICE_PLANE_SNAPSHOT` | `dev` | Snapshot slug backfills and audits write to. |
| `LIQUID_UNIVERSE_TOP_N` | `500` | Names in `liquid_us_equity_v1` per month-end. |
| `LIQUID_UNIVERSE_WINDOW_SESSIONS` | `20` | Sessions in the median-dollar-volume window. |
| `DELISTING_AUDIT_WINDOW_SESSIONS` | `10` | Sessions the terminal return is measured over. |
| `DELISTING_AUDIT_COLLAPSE_THRESHOLD` | `-0.60` | Below this, the series carried the collapse. |

`DATABASE_URL` selects the engine as everywhere else — see
[`DATABASE_ENGINES.md`](DATABASE_ENGINES.md).

### Backfill

```bash
export PRICE_PLANE_ENABLED=true

# Offline, against the committed fixtures — no key, no network.
python -m scripts.price_backfill --source fixture --since 2023-01-01

# Against Sharadar.
export NASDAQ_DATA_LINK_API_KEY=...
python -m scripts.price_backfill --source sharadar --since 2015-01-01 \
    --tickers AAPL,MSFT,BBBY --snapshot 2026-09
```

Idempotent by natural key: the same `--since` rewrites the same rows rather than
duplicating them, so a run that dies half way is resumed by running it again.
`--tickers` is required for Sharadar (it has no enumerable universe through this
interface); the fixture source defaults to everything it knows. The coverage
summary lands on the snapshot.

`--skip-reconstruction-check` exists for triage only and prints a warning saying
the stored bars may not reproduce from their own factors.

### Universes

```bash
# The free MIT membership source, from the committed CSV.
python -m scripts.build_universes --universe sp500_wikipedia_v1

# Computed from price_bars alone.
python -m scripts.build_universes --universe liquid_us_equity_v1 --top-n 500
```

Both rewrite their own slug wholesale, so re-running is how you update. Rows for
other slugs are untouched.

### The delisting audit

```bash
python -m scripts.audit_delisting_returns --source fixture
python -m scripts.audit_delisting_returns --source sharadar --snapshot 2026-09 --strict
```

Exit 0 the audit ran, 1 (`--strict` only) the file needs synthesised terminal
returns, 2 refused to run.

## `liquid_us_equity_v1` — what it means

Spec N §4.2: no affordable source has Russell membership history, S&P history is
a hand-maintained Wikipedia scrape, and "liquid US equities" computed from
*today's* liquidity is survivorship bias wearing a disguise, because the names
that died were illiquid on the way down. So the primary universe is
self-defined, reproducible, and applied point-in-time to the price file.

At each **month-end** (the last session in `price_bars` on or before the last
calendar day of the month):

1. For every security with a full 20-session window **ending on that session**,
   take the median of `raw_close × volume` over those sessions.
2. Rank descending, take the top *N*. Ties break on `security_uid`, so the answer
   is a pure function of the stored bars.
3. Membership runs from that month-end inclusive to the next month-end
   exclusive; the last month's interval is left open.
4. `known_at_utc` is the month-end close — the first moment the ranking could
   have been computed.

Step 1's "ending on that session" is what stops a delisted name from carrying a
stale rank forever: a security whose history ended in March has no liquidity as
of July, and ranking it on its last twenty live sessions would keep it in the
universe indefinitely. A name that simply did not trade that day drops out too,
which is the same answer for a weaker reason and errs conservative.

**Market cap is not in the rule.** Spec N §4.2 names "rank-by-market-cap and
dollar volume"; market cap needs a share count, the price file does not carry
one, and the SEC `dei:EntityCommonStockSharesOutstanding` feed that supplies it
lands in **Phase 3a**. `liquid_us_equity_v1` is liquidity-only, and it is
versioned `_v1` precisely so that adding the cap leg later is a *new slug*
rather than a silent redefinition — the same immutability rule setup specs get.

## `sp500_wikipedia_v1` — and its provenance

The MIT-licensed `fja05680/sp500` history, committed at
`data/prices/sp500/sp500_historical_components_fja05680.csv` (upstream commit
`a2430f2`). Per-change-date constituent snapshots from 1996, converted into
half-open intervals.

Read the provenance before trusting it (verification §22): the original list came
from Andreas Clenow's *Trading Evolved* and runs 1996–2019; everything after is
the maintainer reconciling Wikipedia's "Selected Changes" against Google
searches, updated every couple of months. It is more history than any affordable
vendor and it is **not** an authoritative membership record. Its provenance class
is `archival_reconstructed`, and a cohort that leans on it inherits that tier.

Two structural truncations: every name in the first row gets
`member_from = 1996-01-02`, so earlier durations are unknowable; and a name that
leaves and rejoins produces two intervals, which is why the natural key includes
`member_from`.

## The delisting audit — what it is actually testing

Verification §24 names the failure precisely: a vendor can carry a delisted
ticker's full history and still simply *stop* at the last quoted price,
producing a file that looks survivorship-free while omitting the terminal loss.
Nothing in the data flags it, and the cohort statistics are biased upward.

`data/prices/delisting_audit_list.py` holds twenty known **performance-related**
delistings, 2015–2024, 14 NYSE/AMEX and 6 Nasdaq. Only performance delistings
test anything: Shumway finds ~99.8% of returns missing for those versus at most
1% for mergers. Each row carries the event, the venue, and whether the primary
record is a Form 25 or an exchange notice.

`scripts/audit_delisting_returns.py` pulls each name's final bars and classifies:

- **collapse** — the terminal return over the last 10 sessions, on the
  split-adjusted close, is below -60%. The file carries the decline.
- **stop** — it is not. The terminal return must be synthesised at Shumway's
  **-30% NYSE/AMEX / -55% Nasdaq** (`comparables.config.DELISTING_TERMINAL_RETURN`).
- **missing** — no bars at all. Reported separately from `stop` because the
  remedy differs: `stop` needs a synthesised return, `missing` needs a different
  vendor.
- **too_short** — fewer bars than the window; nothing can be concluded.

The threshold is configuration rather than a constant inside a function, because
"how far down is a collapse" is a judgement and it should be visible and
movable. -60% over 10 sessions sits well below the -55% Nasdaq correction, so a
series that merely drifts to the correction level is not credited with having
carried the collapse.

The result is written into `price_snapshots.delisting_audit_json`.

## Open items

These are known and unresolved, not oversights.

1. **The twenty delisting facts are not verified against their primary filings.**
   `www.sec.gov` is blocked by this session's egress proxy, so `source_url` on
   each row is an EDGAR *lookup* for that company's Form 25 filings rather than a
   specific accession number, and the dates are month-reliable, session-
   approximate. That is enough for what the audit does — compare a series'
   terminal behaviour against a known collapse — and it is **not** enough to
   publish as a delisting-date reference. The audit blob carries
   `sources_verified_against_primary_filing: false` and the script says so.
2. **No Sharadar run has happened.** No key exists. Everything in
   `SharadarPricePlane` was exercised against stub payloads, and its column list
   is verified from search extracts only (above).
3. **Cross-check `closeadj` once a key exists.** Comparing our derived
   total-return series against Sharadar's own is cheap and would either confirm
   the conventions or surface a real disagreement.
4. **Sharadar's SP500 reconstruction is unverified** and is not what this repo
   loads. `sp500_wikipedia_v1` is. The adapter method exists so the interface is
   complete, and it deliberately ignores the quarterly snapshot rows: treating a
   snapshot as a join would date every current constituent's membership to the
   snapshot.
5. **`liquid_us_equity_v1` rebuilds in memory.** `universes.rebuild` loads every
   stored bar to rank month-ends. That is fine for the fixture and for a few
   hundred names; a full 5,000-name, 10-year file is ~12M rows and will need a
   windowed rebuild (one month-end at a time, bars restricted to the window).
   The rule itself is unchanged by that — only how the bars are fetched.
6. **Legacy schema adoption.** `database.schema.classify` now compares a
   pre-Alembic database against `BASELINE_TABLES` rather than against
   `Base.metadata`, because the two diverge the moment any phase adds a table.
   Its column check still reads the ORM declaration for those tables, which is
   correct only while no phase *alters* a baseline table. A phase that does must
   extend `BASELINE_TABLES` to carry the baseline's columns too.

## Tests

`tests/test_price_plane.py` and `tests/test_price_plane_schema.py`, both offline
and both on either engine. The named ones:

| Test | What it holds down |
|---|---|
| `test_three_price_series_stored` | Three genuinely distinct series, with the split visible in raw and invisible in adjusted |
| `test_series_reconstruct_from_factors` | Any one series from the other two plus factors, to 1e-9, for every fixture name |
| `test_universe_membership_is_point_in_time` | A name that joined after `d` is not a member as of `d` — in memory and through the database |
| `test_delisted_security_retained_with_reason` | Delisted names keep their row, date, reason and venue |
| `test_delisting_audit_classifies_collapse_vs_stop` | Both classifications, from fixtures containing one of each |
| `test_snapshot_records_audit` | The audit round-trips through `price_snapshots` |
| `test_adapter_schema_change_fails_loudly` | A renamed vendor column raises; an added one does not |
| `test_liquid_universe_rule_reproducible` | Same bars in, identical membership out, in any dict order |
| `test_no_network_in_tests` | The offline surface opens no socket |
| `tests/test_schema_discipline.py` | Single head, single base, every revision descends from the baseline |
