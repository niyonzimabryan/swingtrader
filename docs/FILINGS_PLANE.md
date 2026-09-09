# The filings plane (Phase 4)

Spec O §3. Form 4, Schedules 13D/G, the full 8-K item index, entity history and
CUSIP resolution — all landing in `source_observations` through the one seam
Phase 3a built, behind `PLANE_FILINGS_ENABLED` (default `false`).

**13F is out of scope**, and the reason is structural rather than schedule
pressure: at a 1–20 session horizon a quarterly snapshot filed up to 45 days
late is nearly useless, and it is the most work of the three planes (Spec O
§3.1). Nothing here reads it, so nothing here can render a 13F position without
its staleness and portfolio share.

## What lands, and with which timestamp

| Fact type | Source | `valid_at` | `known_at_utc` |
|---|---|---|---|
| `insider_open_market_purchase` / `_sale` | `sec_form4` | transaction date | filing `acceptanceDateTime` |
| `insider_award`, `insider_derivative_exercise`, `insider_tax_withholding`, `insider_gift`, `insider_other_transaction` | `sec_form4` | transaction date | filing `acceptanceDateTime` |
| `beneficial_ownership_13d_event` / `_13g_event` | `sec_schedule_13` | acceptance | acceptance |
| `beneficial_ownership_percent_of_class`, `_shares` | `sec_schedule_13` | acceptance | acceptance |
| `eight_k_item` | `sec_8k_items` | acceptance | acceptance |

Every row is `precision='second'`, `provenance_class='vendor_pit'`,
`source_trust='primary_regulator'`, `replay_eligible=true`.

**A Form 4's `valid_at` is the transaction date and its `known_at_utc` is the
acceptance stamp, and they are up to two business days apart.** That gap is the
whole value of the plane: the insider knew on the transaction date and nobody
else could until acceptance. Using the transaction date as availability hands a
backtest two days of the insider's own information; using `filingDate` hands it
the rest of that day, since EDGAR routinely accepts after the close (the
verified claim-13 case is a Form 4 dated 2026-09-03 and accepted at 22:30 UTC).
`filings/observations.py` refuses both by name.

Phase 3a's `earnings_release_8k_item_202` fact type is **untouched** — Phase 3b
reads it. `eight_k_item` is a separate, additional index over every item.

## Transaction codes are never pooled

Spec O §3.1 rule 3. Conflating them is the most common insider-data error and
it inverts the signal: a vesting award is not a vote of confidence, and a sale
to cover withholding tax on that award is not a vote of no confidence.

The separation is **structural, not a convention**: each code category has its
own `fact_type`, so pooling is not something a caller can do by forgetting a
filter, and `filings.form4.net_open_market_shares` **raises** on any row that
is not `P`/`S` rather than silently filtering it out. A silent filter would
make "insiders bought 16,500 shares" true of a set the caller believed also
contained awards, and nobody would ever find out.

| Code | Category | Pooled with P/S? |
|---|---|---|
| `P`, `S` | `open_market` | — these are the informative pair |
| `A`, `D` | `award` | never |
| `M`, `C`, `X` | `exercise` | never |
| `F` | `tax_withholding` | never |
| `G` | `gift` | never |
| `V`, `I`, `J`, `K`, `L`, `U`, `W`, `Z`, `E`, `H`, `O` | `other` | never |

A code outside SEC's table raises `AdapterSchemaError`. It is a schema change
to look at, not an "other" to absorb.

### The Rule 10b5-1 flag

Verification claim 14 could **not** confirm the field-level representation of
the checkbox added to Form 4 in 2023. So `filings/form4.py` looks for any of
the documented candidate elements (`aff10b5One`, `rule10b5-1Flag`,
`rule10b5-1Plan`, `rule10b51Flag`), falls back to a footnote referenced by the
transaction that mentions "Rule 10b5-1", and records which mechanism fired in
`plan_10b5_1_basis`.

**When none fires the value is `None` — unknown, not `False`.** Encoding "we
could not tell" as "not a plan" is what would make a planned sale read as a
discretionary one, which is the exact inversion this module exists to prevent.
The row carries `rule_10b5_1_flag_unknown`, and `net_open_market_shares`
reports `rows_with_unknown_plan_flag` beside the totals.

Confirming the real element name against a live filing is one REPL session and
is an owner action.

## Amendments

A `4/A` or `SC 13D/A` **supersedes** its original via
`superseded_observation_id`; the original is never deleted and stays queryable,
because `known_at_utc <= t` must still return what was known at `t` — and what
was known then included the un-amended filing.

EDGAR does not put the amended accession in the ownership XML, so the target is
found by natural key: same issuer, same reporting owner, same transaction date,
same security. Where that leaves more than one candidate the amendment is
written **unlinked** and counted (`amendments_unlinked`), because linking an
amendment to the wrong original corrupts a supersession chain silently and an
unlinked amendment is a number in a coverage report.

Aggregates read through `filings.observations.current_view`, which drops rows
an amendment in the same set supersedes — so an amended filing is counted once,
at its amended size.

## Entity history

`filings/entities.py` and the `entity_history` table. Three bases, and they are
not equally good:

| Basis | Where the dates come from | Strength |
|---|---|---|
| `submissions_former_names` | EDGAR states `from`/`to` on `formerNames` | a real dated range, from the regulator |
| `filing_cover_page` | the filer wrote its own trading symbol on a filing EDGAR dated to the second (Form 4 `issuerTradingSymbol`) | a real dated statement — **this is what gives historical ticker coverage at all** |
| `snapshot_observed` | we saw a value in a snapshot on one date and a different one later | bounded by *our* observations; `valid_from` is an upper bound |

`ticker_at` and `cik_for_ticker` return an **unresolved** result rather than a
guess when no interval covers the date. There is no "nearest interval"
fallback: two CIKs can hold the same symbol in different eras, and answering
with today's holder for a 2018 event is exactly the bug the table exists to
prevent. `cik_for_ticker` returning several CIKs for one date is a genuine
ambiguity and is returned as one, with every candidate.

### Answering Phase 3a's `ticker_from_current_snapshot`

Phase 3a stamped that warning on every row it wrote, because at the time there
was no history to consult. `filings.entities.resolve_snapshot_warnings` answers
it:

```bash
python -m scripts.filings_backfill --ciks 0000320193 --resolve-tickers          # dry run
python -m scripts.filings_backfill --ciks 0000320193 --resolve-tickers --apply  # rewrite
```

It is a dry run by default. With `--apply` the row's `ticker_at_time` is
corrected and the warning dropped; `payload_hash` is computed from the
observation's identity and value, which does not include the ticker, so the
rewrite cannot collide with an existing row or change what a re-ingest does.
Rows whose date no interval covers **keep their warning** — an admitted
snapshot is better than a wrong point-in-time ticker.

**A first backfill of a long history resolves very little, and that is
correct.** Cover-page intervals only exist for CIKs that have filed Form 4s,
and snapshot intervals only start when we first looked. Coverage improves with
every run. The report prints the rate rather than hiding it.

## CUSIP → ticker

`filings/openfigi_client.py` (the one module that talks to OpenFIGI) and
`filings/cusip.py`. Mapping is **one-way** — OpenFIGI accepts `ID_CUSIP` as
input but does not return CUSIP as output, because those are proprietary
identifiers with redistribution restrictions (verification claim 15). A reverse
lookup table cannot be built from this API.

Rate limits are constants, and the client picks by whether `OPENFIGI_API_KEY`
is set: **25 requests/minute and 10 jobs per request without a key, 25 per 6
seconds and 100 jobs with one.** Raising the batch size without the key that
earns it is not something a caller can do by passing an argument.

Three outcomes, and every input gets one:

- **`mapped`** — exactly one US-listed ticker.
- **`unmapped`** — no data, or only non-US venues. The holding is **kept** with
  `ticker=None` and lands in the warnings list. A position you cannot name is
  still a position; dropping it understates a portfolio in a way nothing
  downstream can detect.
- **`ambiguous`** — several distinct US tickers (dual class, ADR beside its
  ordinary, units/warrants). **Every candidate is returned and no pick is
  made.** Picking the first result is right most of the time, which is exactly
  what makes the wrong times invisible.

## Reading it

`filings/api.py` ships the stable Python API, and **`filings_recent` is
registered as an MCP read tool** on the workspace service (`workspace/tools.py`,
scope `read`) — Phase 0b and Phase 1 landed that service while this phase was
being built, so the registration is done rather than deferred. The tool layer
holds no query logic: it parses arguments, authorises, and delegates here.

Both functions return a `provenance` block (`as_of_utc`, per-field sources,
staleness flags, `data_quality` tier), per Spec K §4.2. Both are reads. Neither
can write, place an order, or call a model.

`insider_activity` is the aggregate view: open-market totals with 10b5-1 sales
reported separately, the non-open-market categories counted **beside** the
totals rather than inside them, and insider purchase clusters (distinct owner
CIKs, anchored on `known_at_utc` — a cluster is only a cluster once it is
visible).

## Running it

```bash
export PLANE_FILINGS_ENABLED=true
export SEC_USER_AGENT="Your Name your.address@example.com"

python -m scripts.filings_backfill --tickers AAPL MSFT --since 2020-01-01
python -m scripts.filings_backfill --ciks 0002000001 --fixtures tests/fixtures/filings
```

`--since` is a **knowledge** cutoff: it selects filings *accepted* on or after
that date. Idempotent — a second run over the same window inserts nothing.

## Why not `edgartools`

Phase 3a declined it and the reasons still hold, plus one more:

1. The contract requires throttling and retry in **one** client module.
   `edgartools` brings its own rate limiter, which fragments the guarantee
   across two implementations rather than centralising it.
2. Its behaviour could not be verified — this environment has no route to SEC —
   so adopting it would trade a tested 300-line parser for an unexercised one.
   Verification claim 14 could not confirm that it exposes the transaction code
   or the 10b5-1 flag as fields, which are precisely the two things this plane
   depends on.
3. The ownership schema is small and flat. `xml.etree` parses it in 300 lines
   including the code table and the pooling rule, and the pooling rule is the
   part that is ours either way — "`edgartools` exposes the codes; the
   discipline is ours" (Spec O §3.1).

Nothing here obstructs adopting it later: the adapter interface is
`source_observations` rows and nothing downstream knows where they came from.
