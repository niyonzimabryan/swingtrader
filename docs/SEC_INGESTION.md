# SEC ingestion — the minimum contract (Phase 3a)

Three free, credential-less SEC feeds, written as point-in-time rows into
`source_observations`. This is the floor the comparable-setups engine (Spec N)
stands on; Phase 4 builds the remaining evidence planes on the same ledger.

Flag: **`PLANE_SEC_MINIMAL_ENABLED`, default `false`.** Nothing is fetched or
written until it is turned on. Reading whatever is already in the ledger is not
gated — the ledger is a table, not a capability.

Specs: [Spec O §2, §3.4](../specs/investment-workspace/O-evidence-planes.md),
[Spec N §4.0](../specs/investment-workspace/N-comparable-setups-engine.md),
[Spec Q §8](../specs/investment-workspace/strategy-lab/strategy-lab-architecture.md).

---

## 1. The three feeds

| Feed | Endpoint | What it gives | Written as |
|---|---|---|---|
| XBRL `companyfacts` | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | Every reported fact with its accession number, form, `fy`/`fp`/`frame` and `filed` date | `revenue`, `eps_diluted`, `eps_basic`, `net_income`, `shares_outstanding`, `shares_diluted_weighted_average` |
| `submissions` | `data.sec.gov/submissions/CIK##########.json` | Per-filing `acceptanceDateTime` — second resolution, explicit UTC `Z` | Not a fact of its own; it is the `known_at_utc` for the other two |
| 8-K index, Item 2.02 | the same `submissions` document, filtered | The earnings-announcement clock | `earnings_release_8k_item_202` |

`submissions` is not optional garnish. Without the acceptance join an XBRL fact
has **no defensible availability time**, and a fact with no availability time
cannot qualify a `clean_pit` cohort.

`filings.recent` covers roughly the last year. Older filings live in the pages
listed under `filings.files`; the client fetches only the pages whose date range
overlaps the requested window.

### What is deliberately not here

13D/G, Form 4, entity/ticker history and news are **Phase 4**. Form 4s appear in
the fixtures only as the after-the-close case that proves the filing-date rule;
nothing ingests them.

---

## 2. The point-in-time rules, and where they are enforced

Everything below is enforced in `filings/observations.py`, on the one seam every
write passes through, so a new plane cannot bypass it by writing rows directly.

**`known_at_utc` is `acceptanceDateTime`, never `filingDate`.** Every
observation names the field its timestamp came from (`known_at_source`). For an
EDGAR source that field must be `acceptanceDateTime`; `filed`, `filingDate`,
`reportDate` and `period` are refused by name, and an EDGAR timestamp landing on
exact midnight UTC is refused too — that is what a date silently promoted to a
datetime looks like.

Why it matters, concretely (verification claim 13): Apple's Form 4 with
`filingDate: 2026-09-03` was accepted at `2026-09-03T22:30:44Z`, after the
close. Keying on the date treats post-close information as available intraday.
The bias is systematic and it flatters every result.

**Every row carries `precision` and a provenance class.** `precision` is
`second` for all three SEC feeds. The provenance class is `vendor_pit` for all
of them: the acceptance stamp is a regulator's observation, not ours (Spec O
§2). `replay_eligible` is `true`, because the timestamp is measured rather than
guessed.

**A `day`-precision fact is known at the close of its day.**
`day_precision_known_at(d)` maps a date to the last instant of that UTC day, so
one `known_at_utc <= cutoff` comparison is correct for both precisions and a
cohort cutting off at 10:00 cannot inherit the rest of the day. Nothing in
Phase 3a is day-precision; the machinery is here because Spec O's macro plane
needs it and `test_day_precision_known_at_close` covers it now.

**Nothing is written half-formed.** An adapter meeting an unexpected payload
shape raises `AdapterSchemaError` — a missing `accn`, a non-numeric `val`, a
renamed `facts` block, `submissions` field arrays that stop lining up
positionally, an `acceptanceDateTime` that is absent, null, or has lost its
UTC marker. It never coerces the surprise into a null, because a null in the
ledger is indistinguishable from a fact that genuinely did not exist, and the
second one is a legitimate answer to a point-in-time question.

**A fact whose accession is not in the acceptance index gets no row at all.**
The count is reported (`facts skipped, no acceptance`) rather than papered over.

---

## 3. The alias map

XBRL coverage is **per tag, not per company**. A filer moving from `Revenues` to
`RevenueFromContractWithCustomerExcludingAssessedTax` has not stopped reporting
revenue, but a one-tag pipeline sees the series stop, and because tag migration
correlates with filer size the resulting dropouts are **non-random**. That is a
bias, not a coverage problem.

`filings/xbrl_aliases.py` holds one ordered tag chain per fact type. Preference
order is most specific and most current first; when one filing reports the same
period under two tags in the chain, the higher-preference tag wins and the
collision is reported.

| Fact type | Taxonomy | Chain (in preference order) |
|---|---|---|
| `revenue` | us-gaap | `RevenueFromContractWithCustomerExcludingAssessedTax`, `…IncludingAssessedTax`, `Revenues`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, `SalesRevenueServicesNet`, `RevenuesNetOfInterestExpense` |
| `eps_diluted` | us-gaap | `EarningsPerShareDiluted`, `IncomeLossFromContinuingOperationsPerDilutedShare`, `EarningsPerShareBasicAndDiluted` |
| `eps_basic` | us-gaap | `EarningsPerShareBasic`, `IncomeLossFromContinuingOperationsPerBasicShare`, `EarningsPerShareBasicAndDiluted` |
| `net_income` | us-gaap | `NetIncomeLoss`, `ProfitLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic` |
| `shares_outstanding` | dei | `EntityCommonStockSharesOutstanding` |
| `shares_diluted_weighted_average` | us-gaap | `WeightedAverageNumberOfDilutedSharesOutstanding`, `WeightedAverageNumberOfSharesOutstandingDiluted` |

Each chain mixes tags that are *not* perfectly interchangeable, and the ones
that differ economically carry a `note` in the code:

- **Excluding vs Including assessed tax** — the difference is sales taxes
  collected as agent. Excluding matches what the rest of the chain reports and
  is preferred; a filer that only tags Including is used as-is.
- **Continuing-operations EPS** is not total EPS for a filer with discontinued
  operations. It sits third in the EPS chains and only fires when nothing else
  is tagged.
- **`ProfitLoss` includes non-controlling interests**, `NetIncomeLoss` does not.

### Coverage alerts, not silent gaps

`coverage_alerts()` reports four kinds, and none of them stops an ingest:

| Kind | Means |
|---|---|
| `tag_migration` | The series continued under a different tag. The merged series **is** continuous; the alert exists so a human confirms the two tags mean the same thing for that filer, rather than finding out in a backtest. |
| `series_gap` | After merging every alias, consecutive period ends are still further apart than the cadence allows. Real missing coverage. |
| `no_coverage` | The company reports none of the tags in the chain. |
| `dual_tagged` | One filing reported a period under two tags in the chain; the higher-preference one was kept. |
| `alias_map_used` | The series needed more than one tag to assemble. Informational. |

Gap thresholds are cadence-aware and configurable in one place: 130 days for a
quarterly series (normal spacing ~91, one missing quarter ~182), 500 for annual,
200 for instant facts like the cover-page share count. Duration decides cadence
(80–100 days quarterly, 340–400 annual) because 52/53-week fiscal calendars move
period ends by up to a week. Year-to-date durations are stored but not
gap-checked.

**`fy`/`fp` are the *filing's* fiscal period, not the fact's.** A FY2018 10-K
carries FY2016 comparatives tagged `fy: 2018, fp: FY`. Continuity is therefore
computed on the fact's own `start`/`end`, never on `fy`/`fp`.

---

## 4. Share count and market cap

`market_cap_decile` needs a share count and the price file does not carry one.
The input is `dei:EntityCommonStockSharesOutstanding` — a cover-page fact on
every 10-K and 10-Q, with `acceptanceDateTime` as its `known_at_utc`.

`companyfacts` **drops the share-class axis**, so a dual-class filer appears as
several entries sharing an accession and a date and differing only in value.
The aggregation is: group by `(accession, date)`, take the distinct values, sum
them. The row records `share_class_count` and `share_class_values` in its
payload and carries the `shares_outstanding_summed_across_classes` warning
whenever more than one value was summed.

Its blind spot, stated rather than hidden: **two classes with exactly equal
share counts collapse into one and the total is understated.** Recovering the
class axis needs the filing's inline XBRL rather than `companyfacts`.

`market_cap_as_of()` returns `None` when no share count was knowable at the
as-of instant. That is the point: a name with no known share count has no market
cap, rather than a market cap computed from a count it could not have had.

---

## 5. The client

`filings/client.py` is the only module that talks to SEC.

- **10 requests/second** is SEC's published maximum and a hard ceiling in code —
  `SEC_MAX_REQUESTS_PER_SECOND`. `SEC_MAX_REQUESTS_PER_SECOND` (the setting)
  may be lower; a higher value is refused by a validator on `Settings`. One
  throttle serves every caller, so adding a call site cannot quietly double the
  rate.
- **User-Agent from `SEC_USER_AGENT`**, in SEC's requested
  `Company Name contact@domain.com` form. There is **no default and no
  fallback**: a real contact address must never be committed here, and a
  plausible-looking invented one is worse than none. The client raises with
  setup instructions until it is set.
- **Retry** on 429/502/503/504 and transport errors, exponential with
  `Retry-After` honoured. A non-429 4xx is a caller bug and raises immediately
  rather than hammering a rate-limited host. A 404 on `companyfacts` is absence,
  not failure — small filers have no document at all.

`edgartools` was **not** adopted; see the PR for the reasoning. Nothing in
`filings/` imports a model client, and a test enforces that by walking the
import graph.

---

## 6. Running the backfill

```bash
export PLANE_SEC_MINIMAL_ENABLED=true
export SEC_USER_AGENT="Your Name your.address@example.com"

# a few names
python -m scripts.sec_backfill --tickers AAPL MSFT KO --since 2018-01-01

# the frozen 50-ticker coverage checkpoint
python -m scripts.sec_backfill --coverage-universe --since 2015-01-01 --report coverage.json

# fetch and report without writing
python -m scripts.sec_backfill --tickers AAPL --dry-run

# replay recorded responses; no network
python -m scripts.sec_backfill --tickers TAGM --fixtures tests/fixtures/sec
```

**`--since` is a knowledge cutoff, not a period cutoff.** It selects filings
*accepted* on or after that date, which is the bitemporal reading: the ledger
records when we could have known something. Re-running with an earlier
`--since` adds older rows without disturbing newer ones. Coverage alerts are
always computed over the company's whole reported history — "did this filer
migrate a tag" is not a question about the window.

**Idempotent.** An observation's natural key is the SHA-256 of its identity
*and its value*, unique in the table. A second run over the same window inserts
nothing and reports the rows as duplicates. A restatement is a different value,
so a different hash, so a new row **beside** the original — never an overwrite.
That is what makes `known_at_utc <= t` return the as-reported figure.

The checkpoint list is frozen in `filings/coverage_universe.py`
(`COVERAGE_UNIVERSE_50`) and deliberately not re-derived from the live universe:
a moving denominator makes two coverage readings incomparable, and dropping a
name that left the index is the survivorship bias Spec N §4.2 exists to avoid.

---

## 7. The ledger table

`source_observations` (revision `0002_source_observations`, branching from
`0001_baseline`) is Spec Q §8 plus Spec O §2. It carries no foreign key to any
other phase's table, so the integration merge revision is a no-op join.

| Column | Notes |
|---|---|
| `source`, `source_url`, `source_trust`, `accession` | Where it came from. `source_trust` is `primary_regulator` for all three feeds. |
| `entity_cik`, `ticker_at_time`, `fact_type` | What it is about. |
| `value_numeric`, `value_text`, `unit` | At least one of the values is required. |
| `period_start`, `valid_at` | When the fact applies. `valid_at` is the period end at midnight UTC for XBRL facts, the announcement instant for 8-Ks. |
| `known_at_utc`, `precision`, `provenance_class`, `replay_eligible` | When we could have known it, and how good that answer is. |
| `payload_json`, `payload_hash` | Normalised payload (including `known_at_source` and, for audit only, the filing date) and the idempotency key. |
| `quality_warnings` | JSON list; see below. |
| `superseded_observation_id` | Amendments (Phase 4). The original stays queryable. |
| `ingested_at` | Bookkeeping. |

Indexes exist for exactly one query shape — `known_at_utc <= t` narrowed by
entity and fact type — plus a ticker-scoped variant and a `(source, accession)`
lookup.

### Quality warnings you will see

| Warning | Means |
|---|---|
| `ticker_from_current_snapshot` | `ticker_at_time` came from the *current* `submissions` snapshot. Ticker history is Phase 4 (Spec O §3.2), so this rides on every Phase 3a row rather than letting a downstream join assume a point-in-time mapping. **Join on `entity_cik`, not on the ticker.** |
| `shares_outstanding_summed_across_classes` | See §4. |

---

## 8. Reading it

```python
from filings.observations import observations_known_at, latest_observation_as_of
from filings.sec_minimal import market_cap_as_of

rows = observations_known_at(
    session, fact_type="revenue", entity_cik="0000320193",
    cutoff=datetime(2024, 3, 15, 14, 30, tzinfo=timezone.utc),
)
cap = market_cap_as_of(session, entity_cik="0000320193", price=172.4, as_of=cutoff)
```

`observations_known_at` is the only sanctioned read. Latest `valid_at` wins;
among rows for the same period the latest `known_at_utc` wins, so a restatement
supersedes the original without either row being deleted.

---

## 9. Tests

Everything runs offline from `tests/fixtures/sec/` through an
`httpx.MockTransport`, so the client's throttle, headers, retry and decode all
execute without a network. **Those fixtures are currently synthetic** — see
`tests/fixtures/sec/README.md` for why, and for
`scripts/record_sec_fixtures.py`, which replaces them with real recordings on a
machine that can reach SEC.

| Test | Asserts |
|---|---|
| `test_xbrl_known_at_from_acceptance` | Every companyfacts row is stamped with its filing's `acceptanceDateTime`, precision `second` |
| `test_filing_date_never_known_at` | A filing date cannot become a `known_at_utc` — named, disguised as midnight, or renamed |
| `test_xbrl_alias_coverage_alert` | A tag migration produces a continuous series **and** an alert |
| `test_8k_202_timestamp` | An earnings event is stamped at its Item 2.02 8-K's acceptance |
| `test_market_cap_has_share_source` | No known share count at a date means no market cap |
| `test_adapter_schema_change_fails_loudly` | Seven payload-shape changes each raise; none writes a null |
| `test_day_precision_known_at_close` | A day-precision fact is invisible to a 10:00 cutoff on its date |

Plus: idempotency, restatement-beside-original, the rate-limit ceiling, the
User-Agent requirement, the flag defaulting off, and an import-graph test that
no module in `filings/` reaches a model client.
