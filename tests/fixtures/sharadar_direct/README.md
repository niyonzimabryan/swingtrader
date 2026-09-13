# Sharadar direct API — recorded payload shapes

Recorded 2026-09-10 (equities) and **2026-09-13 (funds)** against
`https://api.sharadar.com/v1.0` using Sharadar's public `test-api-key` (free
sample universe: AAPL for `stocks`/`actions`, SPY for `funds`). No paid key was
used and none of these bodies contain a key — the `test-api-key` string itself
is public per `docs/vendors/sharadar.md` and is not a secret, but it is
stripped from these files anyway since the fixtures need only the response
bodies, not the request URLs.

## Files

- `stocks_aapl.json` / `stocks_aapl.csv` — `GET /data/stocks?ticker=AAPL&from=2024-01-02&to=2024-01-10`,
  `format=json` and `format=csv`. Envelope is `{"count": N, "data": [...]}` —
  `data` is a list of row **objects** (`{"ticker": ..., "date": ..., ...}`),
  not the Nasdaq Data Link `datatable.columns` + array-of-arrays shape.
- `actions_aapl.json` — `GET /data/actions?ticker=AAPL`. Same envelope; rows
  carry `date`, `action`, `ticker`, `name`, `value`, `contraticker`, `contraname`.
  Only `action=dividend` observed in the free sample (no split in AAPL's
  trailing-5-year sample window).
- `tickers_aapl_all_tables.json` — `GET /data/tickers?ticker=AAPL` with **no**
  `table` filter: returns one row **per plan** the ticker appears in
  (`table: "stocks"`, `"fundamentals"`, `"insiders"`), otherwise identical.
  The adapter must filter `table=stocks` (as a query param, confirmed to work
  server-side — see `tickers_aapl_stocks.json`/`.csv`) or it double-counts
  every ticker.
- `tickers_aapl_stocks.json` / `.csv` — the same query with `table=stocks`:
  exactly one row. This is what the adapter actually sends.
- `sp500_aapl.json` — `GET /data/sp500?ticker=AAPL`. Row `action` values
  observed are **`current`** (one row, the latest snapshot) and **`historical`**
  (one row per quarter-end AAPL was a member) — never `added`/`removed`,
  because AAPL never left the index in the sample window. This confirms the
  existing code comment that sp500 carries quarterly snapshot rows distinct
  from membership-change events; it does not confirm or refute the
  `added`/`removed` action values the adapter still assumes for an actual
  change, which remains unverified (see `sharadar.py`'s module docstring).
- `schema_stocks.sql`, `schema_actions.sql`, `schema_tickers.sql`,
  `schema_sp500.sql` — `GET /schema/{table}?format=sqlite`. Despite the
  `sqlite` format name these are plain SQL DDL text (a `CREATE TABLE`
  statement plus indexes), not a binary sqlite file. Confirms every column
  name and nullability this adapter depends on: `stocks.closeunadj` is
  nullable REAL (not NOT NULL) at the schema level, `tickers` primary keys on
  `(table, permaticker, ticker)`, `sp500` primary keys on
  `(date, action, ticker)`.
- `stocks_aapl_page1_limit3.json` / `stocks_aapl_page2_limit3_offset3.json` —
  `limit=3` and `limit=3&offset=3` on the same query. There is **no**
  remaining-row-count or cursor field anywhere in the envelope — `count` is
  simply `len(data)` for that page. Pagination has to be driven by the
  standard offset convention and terminated when a page comes back shorter
  than the requested `limit` (confirmed separately: an unpaginated request
  for AAPL's full ~5-year sample window returned `count: 1254` in one shot,
  under the default `limit=10000`).
- `error_401_bulk_no_key.json` — `GET /data/stocks?years=5` with `test-api-key`
  (as both `api_key` query param and `x-api-key` header): `HTTP 401`,
  `{"error": "Unauthorized", "description": "A valid API key is required for
  bulk downloads."}`. The free sample key is rejected for the bulk path
  entirely (not just rate- or plan-limited) — bulk was not otherwise
  reachable without a paid subscription, so the 302-to-zip redirect itself is
  unverified and the bulk path is exercised in tests only against a
  synthetic zip and a stubbed transport.
- `error_403_exceeds_free_tier.json` — `GET /data/stocks?ticker=XOM` (a name
  outside the free sample universe): `HTTP 403`,
  `{"error": "Exceeds free tier", "description": "Please sign up at
  /subscribe."}`. Same shape whether `format=json` or `format=csv` is
  requested — errors are always JSON.
- `error_403_unknown_table.json` — `GET /data/notatable`: `HTTP 403`,
  `{"error": "Forbidden", "description": "Unknown table."}`.

## Funds (SFP) — recorded live 2026-09-13

Every file in this section is a **real capture**, not a construction, except
the one explicitly marked otherwise at the end.

- `schema_funds.sql` — `GET /schema/funds?format=sqlite`. The column names are
  **byte-identical to `stocks`**: `ticker, date, open, high, low, close,
  volume, closeadj, closeunadj, lastupdated`, primary key `(ticker, date)`.
  That is why `FUNDS_COLUMNS` in the adapter differs from `STOCKS_COLUMNS` in
  exactly one entry — `closeadj`, which the fund path needs and the equity path
  does not.
- `tickers_spy_all_tables.json` — `GET /data/tickers?ticker=SPY` with **no**
  `table` filter. Exactly **one** row, and its `table` is `"funds"`
  (permaticker `118691`, NYSEARCA, `category: "ETF"`, `firstpricedate`
  1993-01-29). Compare `tickers_aapl_all_tables.json`, where the same
  unfiltered query returns three rows (`stocks`, `fundamentals`, `insiders`).
  This single file is the whole reason the adapter had to change: SPY has no
  `stocks` row, so an adapter that only ever sent `table=stocks` could not see
  the benchmark at all.
- `tickers_spy_funds.json` — the same query with `table=funds`. Identical
  single row, confirming the filter works server-side for `funds` exactly as
  it does for `stocks`.
- `funds_spy.json` / `funds_spy.csv` — `GET /data/funds?ticker=SPY&from=2024-03-11&to=2024-03-20`,
  in both formats. Eight sessions, chosen because **2024-03-15 is an SPY
  ex-dividend date** and it is inside the window: `closeadj/close` is
  0.970733 on 2024-03-14 and 0.973779 on 2024-03-15, and the step is what
  `fund_factors_from_quotes` reads a distribution out of. Same envelope as
  `stocks` (`{"count": N, "data": [ {...} ]}`), rows descending by date.
- `error_403_stocks_spy.json` — `GET /data/stocks?ticker=SPY`: `HTTP 403`,
  `{"error": "Exceeds free tier", ...}`. SPY is not in the `stocks` table.
- `error_403_actions_spy.json` — `GET /data/actions?ticker=SPY`: the same
  `HTTP 403`. **This is the one that shaped the design.** Sharadar's docs say
  `actions` covers "all tickers in fundamentals, stocks and funds tables", but
  the public key cannot read a fund's rows there, so this session could not
  observe a single fund distribution in `actions`. It could and did observe
  them in `funds.closeadj`. That is why a fund's bar factors are derived from
  `closeadj` (`data/prices/sharadar.py::fund_factors_from_quotes`) rather than
  from the action log, and why the design note says so out loud instead of
  trusting a documented claim it could not check.

### `unverified_live: true` — built from the schema, never asserted on as a value

- `actions_spy_unverified.json` — **not a live capture.** `actions?ticker=SPY`
  is a 403 on this key (above), so this file is constructed from
  `schema_actions.sql`'s column list to give `corporate_actions()` a fund
  payload of the right *shape* to parse. It carries an explicit
  `"unverified_live": true` key at the top level so nothing can mistake it for
  a recording. The two `value` numbers in it are the ones this session derived
  from SPY's own `closeadj`, and **no test asserts on them as values** — the
  tests that read this file assert on shape, on the table the request went to,
  and on the §4.3 reconstruction identity, all of which hold whatever the
  numbers are. Replace it with a real capture from a paid key and every test
  that reads it should still pass; if one does not, that test was wrong.

### What the fund capture does *not* cover

**No split.** SPY is the only fund in the free sample — QQQ, IVV, TQQQ, SOXL,
UVXY, DIA, IWM, VOO and GLD all return `403 Exceeds free tier` on `funds` — and
SPY has never split, so `closeunadj == close` on every session recorded here.
The live fixture therefore exercises the fund split arithmetic only at a ratio
of 1.0. The split derivation and its snap-to-1.0 threshold are tested against a
synthetic two-session case instead, which is honest about what it is: a test of
the arithmetic, not of the vendor.

## Things that did *not* produce a 403

`GET /data/tickers` is not free-tier-limited at all: SPY, IVV and QQQ each
returned a full master row on the public key even though only SPY's *prices*
are readable. So the auto-detect path in `security_master(asset_class=None)`
works on a free key for any name, and only the price call is gated.

## Things that did *not* produce a 401

An unrecognized/garbage `api_key` value, an empty `api_key=`, and no
`api_key` param at all were all accepted identically to `test-api-key` for
`ticker=AAPL` (sample-universe access), returning `HTTP 200`. The only
observed `401` is the bulk-download path above. A malformed but
previously-valid (e.g. revoked) key rejected with 401 on `/data/{table}`
slices is plausible per Sharadar's own docs but was not observed here — the
adapter still maps a non-2xx on the slice path to a typed error by status
code (401/403 first, anything else generic), it just could not be
independently confirmed against this sandbox for that one case.
