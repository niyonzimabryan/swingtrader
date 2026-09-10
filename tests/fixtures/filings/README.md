# Phase 4 filings fixtures

The suite must pass offline. Every test in `tests/test_filings_plane.py` serves
these files through an `httpx.MockTransport`, so the whole client path —
throttle, User-Agent, retry, decode, adapter, ledger write — runs without a
network.

## These are synthetic, and that is a compromise, not a preference

The Phase 4 contract asks for *recorded* responses. They are not here because
**the environment this branch was written in has no route to SEC**:
`www.sec.gov`, `data.sec.gov` and `efts.sec.gov` are all refused at the egress
proxy (`403` to `CONNECT`), so no response could be recorded. That is the same
block Phase 3a hit and documented.

So the fixtures are built to the shape the SEC ownership XML and the
`submissions` index are documented to have — the same element names, the same
`2026-03-03T22:30:44.000Z` acceptance format, the same parallel-array
`filings.recent` layout. What they are **not** is evidence that the real
documents still have that shape. Only a real recording is that.

**One field-level uncertainty is baked into the fixtures deliberately.**
Verification claim 14 could not confirm how the Rule 10b5-1 checkbox added to
Form 4 in 2023 is represented in the XML. The fixtures therefore carry *both*
candidate mechanisms — a per-transaction `<rule10b5-1Flag>` element and a
footnote mentioning "Rule 10b5-1" — plus a sale with neither, and
`filings/form4.py` accepts any of the documented spellings and records which
one fired. Confirming the real element name against a live filing is one REPL
session and is listed as an owner action in the pull request.

The entities are deliberately fictional (`INSD`, `ACTV`, `OLDR`, `RCYC`, CIKs
in the `00020000xx` block, people named after the case they exercise) so that
no reader can mistake a generated number for a filed one.

## Replacing them with real recordings

On any machine that can reach SEC:

```bash
export SEC_USER_AGENT="Your Name your.address@example.com"
python -m scripts.record_filings_fixtures --tickers AAPL MSFT --forms 4 "SC 13D"
```

That writes `submissions_CIK*.json` and the Form 4 / Schedule 13 documents for
the real CIKs beside these. The tests key off the fixture files present in this
directory, so real recordings extend the coverage rather than replacing the
synthetic edge cases — **keep both**.

## What each fixture is for

| CIK | Ticker | Exercises |
|---|---|---|
| `0002000001` | `INSD` | Every transaction-code category on one issuer: `P` (two insiders, two days apart, so a cluster of two), `A`, `M`+`F` on one form, `G`, and three `S` rows — one flagged 10b5-1 by element, one by footnote, one not flagged at all. Plus a `4/A` that restates the first purchase 12,000 → 11,500, and 8-K items `2.02`, `5.02`, `1.01`, `7.01`, `4.02`, `1.05` and an unknown `9.99`. |
| `0002000002` | `ACTV` | `SC 13D`, its `SC 13D/A`, and an `SC 13G` from a different filer, each with a structured cover page carrying percent of class and share count. |
| `0002000003` | `OLDR` | Held `RCYC` until 2019, renamed in 2019 (`formerNames` with real `from`/`to` dates), and filed an 8-K in 2018 while it still held the symbol. |
| `0002000004` | `RCYC` | Picked the symbol up in 2021. Together with `0002000003` this is `test_ticker_reuse_resolved_by_date`. |

Every Form 4 in `0002000001` is accepted **after 16:00 UTC**, and the first one
at `22:30:44Z` — the verified claim-13 case that makes `filingDate` a
one-session lookahead leak.

Regenerate with `python tests/fixtures/filings/make_fixtures.py` from the
repository root.
