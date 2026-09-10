# Sharadar direct API — recorded payload shapes

Recorded 2026-09-10 against `https://api.sharadar.com/v1.0` using Sharadar's
public `test-api-key` (free sample universe, AAPL included). No paid key was
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
