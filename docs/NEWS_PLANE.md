# The news plane (Phase 4)

Spec O §5. Timestamped, deduplicated, tiered news, behind `PLANE_NEWS_ENABLED`
(default `false`).

News already existed in this repository (`data/news_data.py`), but publication
time fidelity, dedup and novelty were not modelled, so "what was known on the
day" was unanswerable. This plane answers it — and is the plane with the
hardest licence constraint attached.

## Nothing news-derived leaves Postgres

Alpaca's market-data terms bar sharing, selling or publishing the data "or any
derived products or services", and bar combining it with other sources for
redistribution (verification claim 11). Spec K §3.3 draws the line: **a dossier
may cite a story by URL and date, and that is all.** No article text, no
novelty score, no news-derived feature reaches the `research/` mirror.

The mirror does not exist yet — it is Spec M's — so the rule ships now, as code
with a test, because the alternative is a rule in a document that whoever builds
the mirror has to remember. Two ways in are closed:

- **The marker.** Every ledger row this plane writes carries
  `mirror_allowed=false` in its payload. `filings.observations.mirror_allowed`
  reads it; absent means allowed, so Phase 3a's SEC rows keep their meaning
  without a rewrite. (It is a payload key rather than a column because
  `source_observations` was created by migration `0002` and Phase 4's migration
  branches from `0001_baseline`, where the table does not exist.)
- **The content.** `news/mirror_guard.py` refuses any payload or staged file
  carrying an article body, a novelty score, or any field name this plane owns
  — because a well-meaning exporter that never looked at the marker is exactly
  the failure a marker alone does not stop. A CSV header row leaks as surely as
  a JSON key, so non-JSON files are scanned as text.

`news_articles` and `news_clusters` are the licence boundary as a *table-level*
rule: bodies live there, and that table is on the guard's forbidden list.

Also removed from every cohort path, per Spec O §5: the Gemini-search +
Firecrawl route cannot establish publication time. It remains a live research
tool and reaches nothing this plane writes. FNSPID is dropped outright — its
licence is CC BY-NC-4.0 with an explicit prohibition on commercial use, and a
system that informs real trades is commercial use.

## Timestamp fidelity — the two `known_at_utc` values

Spec O §5.1, and this is the subtlest rule in the phase.

| | `known_at_utc` |
|---|---|
| **A story** (a cluster) | the **minimum** publisher timestamp across its members |
| **A fact extracted from an article** | **that article's** timestamp |

They must not be merged. A consensus figure that first appears in a 16:45
reaction piece must not inherit the 16:30 release's timestamp — or, in the
preview case, the 07:00 preview's — or it becomes available before it existed.
`test_fact_known_at_is_article_not_cluster` is that case, and the ledger rows
say which rule produced them (`known_at_source` is
`publisher_timestamp_min_across_cluster` or
`publisher_timestamp_of_source_article`).

An article whose publication time cannot be established is stored with
`replay_eligible=false`, `provenance_class='archival_reconstructed'`, and is
**quarantined, not discarded** — "we saw this and could not date it" is worth
keeping. Our own fetch time is never substituted: an article stamped when we
noticed it looks available the moment we started looking, which is lookahead
shaped like diligence. Finnhub's `datetime: 0` is treated as *missing*, not as
1970 — a 1970 stamp sorts first and would become its cluster's `known_at_utc`.

Article revisions are new rows, not overwrites: `article_uid` hashes the
content, so a revised article is a new identity and "when was this available"
keeps both of its answers.

## Clustering

`news/clustering.py`. Two deterministic passes:

1. **Exact canonical-URL match** — scheme forced, host lower-cased and
   de-`www`-ed, tracking parameters stripped, query sorted, fragment dropped.
2. **MinHash/LSH over 5-word shingles of title-plus-lead** (`datasketch`, MIT)
   for wire pickups.

Embedding cosine is named in the spec as an *optional* third pass and is
deliberately not built: it would put a model in a path whose whole selling
point is reproducibility, and the case that actually inflates counts is a
near-duplicate, not a paraphrase.

**LSH proposes; exact Jaccard decides.** `MinHashLSH` is a banded index and its
recall is probabilistic — a genuinely 0.57-similar pair was reproducibly
*missed* at a 0.45 index threshold with 128 permutations. So the index is
queried loosely to generate candidates and every candidate pair is confirmed by
the exact Jaccard of the shingle sets. The decision is then exact and
independent of the permutation count, and LSH does the one thing it is good at:
avoiding the quadratic comparison.

The threshold is **0.5**, and the calibration is reproducible from the
committed fixtures: same-story pairs score 0.56–0.57, a genuinely different
preview scores 0.08. 0.6 — the first value tried — split the same-story pairs,
which is the failure that manufactures momentum.

Determinism matters more than recall here: the seed is fixed, edges are
collected in sorted order, and the cluster id is the `article_uid` of the
earliest-published member. A cluster id that moved between runs would make
`news_clusters` unjoinable to anything.

## Source tiering

primary > established > aggregator > unattributed. The tier travels with the
fact, because §5.3 gates use on it.

**An unrecognised publisher is `unattributed`, never `established`.** That is
the fail-closed direction: an unknown publisher can still be dossier evidence,
and mis-tiering it upward would let it qualify a cohort.

## The eligibility matrix (§5.3)

Enforced by `news/eligibility.py`. A use not in the matrix **raises** — a
missing rule must not read as permission.

| Use | Requires | Provenance class |
|---|---|---|
| Date an event | publisher timestamp + primary tier | `vendor_pit` |
| Qualify a cohort as a structured fact | publisher timestamp + tier ≥ established + deterministic extraction | `vendor_pit` |
| Covariate or novelty context | publisher timestamp | `vendor_pit` |
| Dossier evidence, invalidator trigger | any tier, rendered with its tier | — |
| Spec Q promotion evidence | **never** | — |

The last row is not a threshold anybody can meet. It is why this is a function
and not a comment: "news may never support a promotion" is only true if there
is one place that says no to every argument.

## `consensus_eps_news`

Spec N §4.0. A **regex grammar**, not a model — `news/consensus_from_news.py`.
A model reading a number out of a sentence would be parsing rather than
producing a statistic, so it would not violate the letter of Spec N §9, but it
is not reproducible, and a consensus that changes when the model is upgraded
silently relabels every cohort built on it.

Seven named patterns, each anchored on **both** consensus vocabulary and EPS
vocabulary, and every match records which pattern produced it. A sentence
carrying revenue vocabulary is skipped entirely: "$4.1 billion consensus" is
not an EPS figure, and reading it as one puts a four-billion-dollar EPS
estimate into a cohort.

**Disagreement is recorded, not resolved away.** Zacks, FactSet and Refinitiv
consensus differ and articles cite whichever their author uses. When articles
disagree, every value is kept with the count behind each and the spread, and
the row carries `consensus_sources_disagree`. The headline value is chosen by a
stated rule — most-cited wins; the earliest-published article breaks a tie —
not by taking the first match seen, and never by averaging, which would invent
a number no source published.

The source article must also clear the §5.3 cohort row. A figure from an
aggregator is extracted and **not written**: it is not a worse fact, it is an
ineligible one.

> **Note on the module path.** Spec N §4.0 names this
> `filings/consensus_from_news.py`. It lives in `news/` because it operates on
> news articles and the Phase 4 contract places the news plane there; putting a
> news-body parser inside the SEC package would also put article text on the
> wrong side of the licence boundary. Same code, different file.

## Novelty

`news/novelty.py`. The fraction of a story's structured facts absent from the
prior story for the same symbols — money amounts (canonicalised, so "$1.2
billion" and "$1,200,000,000" are one fact), percentages, share counts, and a
fixed event vocabulary. Computed by comparing extracted facts, **not by asking
a model whether it feels new**.

Its limits are stated rather than papered over: a genuinely new development
described without a number scores 0.0, and a restatement that rounds
differently scores above 0.0. Both are visible in `novelty_basis`, which lists
the facts on each side. It is a covariate, not a signal.

## Reading it

`news/api.py` ships the stable Python API, and **`news_timeline` is registered
as an MCP read tool** on the workspace service (`workspace/tools.py`, scope
`read`). Its tool description states the licence constraint in the text an
agent actually reads, not only in this file.

It returns one row per **story**, a `provenance` block, and — with
`include_quarantined=True` — the undated articles, for a human to look at.
Article bodies are not returned by this function at all, and the provenance
notes say `mirror_allowed=false` on every row so a caller copying it into an
export hits the guard rather than discovering the licence problem later.

## Running it

```bash
export PLANE_NEWS_ENABLED=true
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...     # the existing broker keys

python -m scripts.news_backfill --symbols AAPL MSFT --start 2026-01-01
python -m scripts.news_backfill --symbols FCTX --fixtures tests/fixtures/news
```

Idempotent by `article_uid` and by ledger payload hash. The report prints the
dedup ratio, the tier breakdown, and how many articles were quarantined for
having no timestamp.

## Rate limits

One client module per source. Alpaca: **200 requests/minute** on the free
market-data plan — verification claim 11 rates that figure *strong secondary*,
not confirmed from the docs page, so it is a setting with the published number
as its default and a comment saying to lower it rather than trust it. Finnhub
stays behind the existing `utils.rate_limiter` bucket in `data/news_data.py`;
`news/finnhub_news.py` reuses it rather than opening a second connection,
because Spec O's "one client module per source" is a rule about the *source*.
