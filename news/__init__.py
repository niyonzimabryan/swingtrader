"""Timestamped, deduplicated news — Spec O section 5.

News exists in this repository already (``data/news_data.py``), but publication
time fidelity, dedup and novelty are not modelled, so "what was known on the
day" is unanswerable. This package answers it.

* ``news.articles``            — the article record, canonical URL, source tier.
* ``news.alpaca_news``         — the primary source (Benzinga via Alpaca).
* ``news.finnhub_news``        — the cross-check for the earliest timestamp.
* ``news.clustering``          — canonical URL + MinHash/LSH into stories.
* ``news.novelty``             — deterministic novelty from structured facts.
* ``news.consensus_from_news`` — the ``consensus_eps_news`` regex extractor.
* ``news.eligibility``         — the section 5.3 matrix, enforced.
* ``news.mirror_guard``        — nothing news-derived leaves Postgres.
* ``news.ingest``              — articles, clusters and ledger rows.
* ``news.api``                 — ``news_timeline(...)``.

**No model is called anywhere in this package.** The novelty score compares
extracted structured facts; the consensus extractor is a regex grammar. Spec N
section 9: a model may never produce a statistic.

**Nothing here may be mirrored.** Alpaca's terms bar sharing the data "or any
derived products" (verification claim 11), so article bodies live in Postgres
and every ledger row this package writes carries ``mirror_allowed=False``.
"""
