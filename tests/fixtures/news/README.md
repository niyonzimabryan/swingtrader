# News fixtures

Alpaca-shaped payloads, read by `tests/test_news_plane.py` both directly and
through an `httpx.MockTransport`, so the suite runs with no network and no
Alpaca credentials.

## These are synthetic, and there is a second reason here

`data.alpaca.markets` is refused at this environment's egress proxy (`403` to
`CONNECT`), so no response could be recorded — the same block the other two
planes hit.

**But even with a route, real Alpaca content could not be committed.** Alpaca's
terms bar sharing or publishing the data "or any derived products"
(verification claim 11), and this repository is public. So these fixtures stay
synthetic *by policy*, not only by circumstance: `scripts/record_news_fixtures.py`
writes recordings for local use and the directory it writes to is
`.gitignore`-worthy on any machine where it is run. The company (`FCTX`,
Fictional Example Corp) and every number in these files are invented.

Each file is `{"news": [...], "next_page_token": null}` with the fields Alpaca
documents: `id`, `headline`, `summary`, `content`, `source`, `url`, `symbols`,
`created_at`, `updated_at`.

## What each fixture is for

| File | Exercises |
|---|---|
| `wire_pickup.json` | One wire story republished by twenty outlets across three tiers, with rewritten headlines and tracking parameters on the URLs. Must produce **one** story (`test_news_cluster_counts_once`) whose `known_at_utc` is the 11:00 wire timestamp. |
| `earnings_preview_and_reaction.json` | A 07:00 preview (a different story), a 16:30 wire release stating the actual EPS and no consensus, and a 16:45 reaction piece that states the consensus. The last two are one story: the story's `known_at_utc` is 16:30 and the consensus figure's is 16:45 (`test_fact_known_at_is_article_not_cluster`). |
| `consensus_disagreement.json` | Two established publishers covering the same preview and citing different consensus figures — $1.23 and $1.18. Both are recorded with the spread (`test_consensus_news_disagreement_recorded`). |
| `tiering.json` | One article per source tier — primary, established, aggregator — plus one with no publisher timestamp at all. The eligibility matrix (`test_news_eligibility_matrix`) and the quarantine rule (`test_untimestamped_news_not_replay_eligible`). |

## Calibration note

The clustering threshold in `news/clustering.py` was set from these fixtures,
and the measurement is reproducible: same-story pairs score 0.56–0.57 Jaccard
over 5-word shingles of title-plus-lead, and the genuinely-different preview
scores 0.08. The threshold sits at 0.5, in the gap.

Regenerate with `python tests/fixtures/news/make_fixtures.py` from the
repository root.
