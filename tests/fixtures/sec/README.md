# SEC fixtures

The suite must pass offline. Every SEC test in `tests/test_sec_minimal.py`
serves these files through an `httpx.MockTransport`, so the whole client path —
throttle, User-Agent, retry, JSON decode, adapter, ledger write — runs without
a network.

## These are synthetic, and that is a compromise, not a preference

The Phase 3a contract asks for *recorded* responses: a handful of real
`companyfacts` and `submissions` documents, fetched once and committed. They
are not here because **the environment this branch was written in has no route
to SEC**: `data.sec.gov`, `www.sec.gov` and `efts.sec.gov` are all refused at
the egress proxy (`403` to `CONNECT`), so no response could be recorded.

So the fixtures are built to the shape that
`docs/research/2026-09-research-verification.md` claim 13 verified against the
live API — the same field names, the same `acceptanceDateTime` format
(`2026-09-03T22:30:44.000Z`), the same parallel-array `filings.recent` layout,
the same `accn`/`filed`/`form`/`fy`/`fp`/`frame` on every fact. What they are
**not** is evidence that the API still has that shape. Only a real recording is
that, and getting one is an owner action.

The entities are deliberately fictional (`TAGM`, `GAPC`, `DUAL`, CIKs
`0001000001`–`0001000003`) so that no reader can mistake generated numbers for
filed ones.

## Replacing them with real recordings

On any machine that can reach SEC:

```bash
export SEC_USER_AGENT="Your Name your.address@example.com"
python -m scripts.record_sec_fixtures --tickers AAPL MSFT KO
```

That writes `submissions_CIK*.json` and `companyfacts_CIK*.json` for the real
CIKs beside these. The tests key off the fixture files present in this
directory, so real recordings extend the coverage rather than replacing the
synthetic edge cases — keep both.

## What each fixture is for

| CIK | Ticker | Exercises |
|---|---|---|
| `0001000001` | `TAGM` | A revenue tag migration (`Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax`) mid-series. The alias map must produce a continuous series **and** a `tag_migration` alert. |
| `0001000002` | `GAPC` | A genuinely missing quarter that no alias covers. Must produce a `series_gap` alert. |
| `0001000003` | `DUAL` | A dual-class cover-page share count: two `dei:EntityCommonStockSharesOutstanding` entries sharing an accession and a date. Must sum, and must flag that it summed. |

Every company also carries 8-Ks with Item `2.02,9.01` (earnings releases), 8-Ks
with `5.02,7.01` (not earnings releases, must be filtered out), and Form 4s
accepted at `22:30:44Z` — the after-the-close case from claim 13 that makes
`filingDate` a lookahead leak.

Regenerate with `python tests/fixtures/sec/make_fixtures.py` from the repo root.
