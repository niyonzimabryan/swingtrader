"""Articles, stories and ledger rows — Spec O section 5, behind ``PLANE_NEWS_ENABLED``.

The shape of one ingest:

1. Articles from Alpaca (primary), optionally cross-checked against Finnhub for
   the earliest-timestamp rule.
2. Cluster into stories (canonical URL, then MinHash/LSH).
3. Store the articles and the clusters in Postgres — **bodies never leave**.
4. Write two kinds of ledger row, each with its own ``known_at_utc``:

   * one ``news_story`` per cluster, at the **minimum publisher timestamp**
     across its members. Twenty republications produce one row
     (``test_news_cluster_counts_once``).
   * one ``consensus_eps_news`` per (cluster, symbol) where the deterministic
     extractor found a figure, at **the timestamp of the article the figure
     came from** (``test_fact_known_at_is_article_not_cluster``).

Every ledger row carries ``mirror_allowed=False``.

An article with no publisher timestamp is stored with ``replay_eligible=False``
and its cluster contributes no story row unless some member is dated. It is
quarantined, not discarded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from database.models import NewsArticle, NewsCluster
from filings.observations import (
    PRECISION_SECOND,
    PROVENANCE_ARCHIVAL,
    PROVENANCE_VENDOR_PIT,
    TRUST_AGGREGATOR,
    TRUST_ESTABLISHED_PUBLISHER,
    TRUST_PRIMARY_ISSUER,
    TRUST_UNATTRIBUTED,
    Observation,
    write_observations,
)
from news import clustering, consensus_from_news, novelty as novelty_module
from news.articles import (
    TIER_AGGREGATOR,
    TIER_ESTABLISHED,
    TIER_PRIMARY,
    TIER_UNATTRIBUTED,
    Article,
)
from news.eligibility import USE_QUALIFY_COHORT, eligible_for
from utils.logger import get_logger

log = get_logger("news_ingest")

SOURCE_NEWS_PLANE = "news_plane"
FACT_TYPE_STORY = "news_story"
FACT_TYPE_CONSENSUS_EPS = consensus_from_news.FACT_TYPE_CONSENSUS_EPS

#: Ledger rows are not about a registrant, and the news plane resolves symbols
#: rather than CIKs. A reserved sentinel beats borrowing a CIK we did not check.
NEWS_ENTITY = "NEWS"

#: Article tier -> the ledger's ``source_trust`` vocabulary.
TRUST_BY_TIER = {
    TIER_PRIMARY: TRUST_PRIMARY_ISSUER,
    TIER_ESTABLISHED: TRUST_ESTABLISHED_PUBLISHER,
    TIER_AGGREGATOR: TRUST_AGGREGATOR,
    TIER_UNATTRIBUTED: TRUST_UNATTRIBUTED,
}


class NewsPlaneDisabled(RuntimeError):
    """``PLANE_NEWS_ENABLED`` is false, so nothing may be ingested."""


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


# --- storing articles and clusters ------------------------------------------


def store_articles(session, articles: Iterable[Article]) -> int:
    """Insert articles not already present, by ``article_uid``. Idempotent.

    A revision at the publisher changes the content hash and therefore the uid,
    so it lands as a **new row** (section 5.1) with ``revision_of_uid`` set
    when the caller knows the predecessor. Nothing is ever updated in place:
    "what did this article say, and when" has two answers after a revision, and
    an update destroys the first.
    """
    batch = {article.article_uid: article for article in articles}
    if not batch:
        return 0
    existing = {
        row[0]
        for row in session.query(NewsArticle.article_uid)
        .filter(NewsArticle.article_uid.in_(list(batch)))
        .all()
    }
    inserted = 0
    for uid, article in batch.items():
        if uid in existing:
            continue
        session.add(
            NewsArticle(
                article_uid=uid,
                source=article.source,
                provider_id=article.provider_id,
                publisher=article.publisher,
                tier=article.tier,
                canonical_url=article.canonical,
                url=article.url,
                headline=article.headline,
                lead=article.lead,
                body=article.body,
                symbols=json.dumps(list(article.symbols)),
                published_at_utc=_naive(article.published_at),
                first_seen_at_utc=_naive(article.first_seen_at)
                or datetime.now(timezone.utc).replace(tzinfo=None),
                content_hash=article.content_digest,
                revision_of_uid=article.revision_of_uid,
                replay_eligible=article.replay_eligible,
                provenance_class=(
                    PROVENANCE_VENDOR_PIT if article.has_timestamp else PROVENANCE_ARCHIVAL
                ),
                quality_warnings=json.dumps(list(article.warnings)),
            )
        )
        inserted += 1
    session.flush()
    return inserted


def store_clusters(
    session, clusters: Sequence[clustering.Cluster], *, novelty_by_cluster: dict
) -> int:
    """Upsert clusters and stamp each article with its cluster id."""
    touched = 0
    for cluster in clusters:
        row = (
            session.query(NewsCluster)
            .filter(NewsCluster.cluster_id == cluster.cluster_id)
            .one_or_none()
        )
        score = novelty_by_cluster.get(cluster.cluster_id)
        if row is None:
            row = NewsCluster(cluster_id=cluster.cluster_id)
            session.add(row)
        row.headline = cluster.headline
        row.known_at_utc = _naive(cluster.known_at_utc)
        row.member_count = cluster.member_count
        row.symbols = json.dumps(list(cluster.symbols))
        row.replay_eligible = cluster.replay_eligible
        if score is not None:
            row.novelty_score = score.score
            row.novelty_basis = json.dumps(score.as_dict())
        touched += 1

        session.query(NewsArticle).filter(
            NewsArticle.article_uid.in_(list(cluster.member_uids))
        ).update({NewsArticle.cluster_id: cluster.cluster_id}, synchronize_session=False)
    session.flush()
    return touched


# --- ledger rows ------------------------------------------------------------


def story_observation(
    cluster: clustering.Cluster, *, novelty_score: float | None = None
) -> Observation | None:
    """One row per story, at the earliest publisher timestamp in the cluster.

    Returns ``None`` for a cluster no member of which can be dated: a story
    with no timestamp anywhere has no ``known_at_utc``, and the ledger will not
    hold a guessed one. The articles stay in ``news_articles``, quarantined.
    """
    if cluster.known_at_utc is None:
        return None

    lead = cluster.members[0]
    best_tier = max(
        (a.tier for a in cluster.members),
        key=lambda t: list(TRUST_BY_TIER).index(t) if t in TRUST_BY_TIER else -1,
    )
    return Observation(
        source=SOURCE_NEWS_PLANE,
        entity_cik=NEWS_ENTITY,
        ticker_at_time=cluster.symbols[0] if cluster.symbols else None,
        fact_type=FACT_TYPE_STORY,
        # A story applies when it broke and is knowable at the same instant.
        valid_at=cluster.known_at_utc,
        known_at_utc=cluster.known_at_utc,
        known_at_source="publisher_timestamp_min_across_cluster",
        precision=PRECISION_SECOND,
        provenance_class=PROVENANCE_VENDOR_PIT,
        replay_eligible=True,
        value_numeric=float(cluster.member_count),
        value_text=cluster.headline[:500],
        unit="articles",
        source_url=lead.url,
        source_trust=TRUST_BY_TIER.get(best_tier, TRUST_UNATTRIBUTED),
        mirror_allowed=False,
        payload={
            "cluster_id": cluster.cluster_id,
            "member_count": cluster.member_count,
            "member_uids": list(cluster.member_uids),
            "symbols": list(cluster.symbols),
            "best_tier": best_tier,
            "publishers": sorted({a.publisher for a in cluster.members if a.publisher}),
            "novelty_score": novelty_score,
            "earliest_publisher_timestamp": cluster.known_at_utc.isoformat(),
        },
    )


def consensus_observation(
    cluster: clustering.Cluster, *, symbol: str
) -> Observation | None:
    """``consensus_eps_news`` for one symbol, at **its article's** timestamp.

    Spec O section 5.1. The cluster's earliest timestamp is deliberately *not*
    used: a consensus figure that first appears in the 16:45 reaction piece was
    not available at the 07:00 preview, and stamping it with the cluster's time
    makes it available before it existed.

    The article must also clear the section 5.3 cohort-qualification row —
    established tier or better, timestamped, deterministic extraction — or the
    figure is extracted and *not written*. A figure from an aggregator is not a
    worse fact, it is an ineligible one.
    """
    reading = consensus_from_news.read_consensus(cluster.members)
    if reading is None:
        return None

    by_uid = {a.article_uid: a for a in cluster.members}
    article = by_uid.get(reading.source_article_uid)
    if article is None:
        return None

    decision = eligible_for(article, USE_QUALIFY_COHORT, deterministic_extraction=True)
    if not decision.allowed:
        log.info(
            "consensus_not_cohort_eligible",
            cluster=cluster.cluster_id,
            symbol=symbol,
            reason=decision.reason,
        )
        return None

    return Observation(
        source=SOURCE_NEWS_PLANE,
        entity_cik=NEWS_ENTITY,
        ticker_at_time=symbol,
        fact_type=FACT_TYPE_CONSENSUS_EPS,
        valid_at=reading.known_at_utc,
        known_at_utc=reading.known_at_utc,
        known_at_source="publisher_timestamp_of_source_article",
        precision=PRECISION_SECOND,
        provenance_class=PROVENANCE_VENDOR_PIT,
        replay_eligible=True,
        value_numeric=reading.value,
        unit="USD_per_share",
        source_url=reading.source_url or article.url,
        source_trust=TRUST_BY_TIER.get(article.tier, TRUST_UNATTRIBUTED),
        mirror_allowed=False,
        payload={
            "cluster_id": cluster.cluster_id,
            "symbol": symbol,
            "extractor": "news.consensus_from_news",
            "extractor_deterministic": True,
            **reading.as_dict(),
        },
        quality_warnings=reading.warnings,
    )


# --- the ingest -------------------------------------------------------------


@dataclass
class NewsCoverage:
    articles_seen: int = 0
    articles_stored: int = 0
    undated_articles: int = 0
    clusters: int = 0
    clusters_dated: int = 0
    story_rows: int = 0
    consensus_rows: int = 0
    written: int = 0
    duplicates: int = 0
    tier_counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "articles_seen": self.articles_seen,
            "articles_stored": self.articles_stored,
            "undated_articles": self.undated_articles,
            "clusters": self.clusters,
            "clusters_dated": self.clusters_dated,
            "story_rows": self.story_rows,
            "consensus_rows": self.consensus_rows,
            "written": self.written,
            "duplicates": self.duplicates,
            "tier_counts": self.tier_counts,
        }


def ingest_articles(
    session,
    articles: Sequence[Article],
    *,
    settings,
    prior_texts_by_symbol: dict | None = None,
) -> NewsCoverage:
    """Cluster, store and write. Idempotent by ``article_uid`` and payload hash."""
    if not getattr(settings, "plane_news_enabled", False):
        raise NewsPlaneDisabled(
            "PLANE_NEWS_ENABLED is false. Set it to true to let the news plane "
            "write to news_articles, news_clusters and source_observations."
        )

    coverage = NewsCoverage(articles_seen=len(articles))
    for article in articles:
        coverage.tier_counts[article.tier] = coverage.tier_counts.get(article.tier, 0) + 1
        if not article.has_timestamp:
            coverage.undated_articles += 1

    clusters = clustering.cluster_articles(
        articles,
        permutations=getattr(settings, "news_minhash_permutations", 128),
        threshold=getattr(settings, "news_cluster_jaccard_threshold", 0.6),
        shingle_size=getattr(settings, "news_shingle_size", 5),
    )
    coverage.clusters = len(clusters)
    coverage.clusters_dated = sum(1 for c in clusters if c.known_at_utc is not None)
    coverage.articles_stored = store_articles(session, articles)

    prior = dict(prior_texts_by_symbol or {})
    novelty_by_cluster: dict = {}
    observations: list[Observation] = []

    for cluster in clusters:
        text = " ".join(f"{a.headline} {a.lead} {a.body}" for a in cluster.members)
        seen_before = [
            t for symbol in (cluster.symbols or ("*",)) for t in prior.get(symbol, [])
        ]
        score = novelty_module.novelty(text, seen_before)
        novelty_by_cluster[cluster.cluster_id] = score
        for symbol in cluster.symbols or ("*",):
            prior.setdefault(symbol, []).append(text)

        story = story_observation(cluster, novelty_score=score.score)
        if story is not None:
            observations.append(story)
            coverage.story_rows += 1
        for symbol in cluster.symbols:
            row = consensus_observation(cluster, symbol=symbol)
            if row is not None:
                observations.append(row)
                coverage.consensus_rows += 1

    store_clusters(session, clusters, novelty_by_cluster=novelty_by_cluster)
    result = write_observations(session, observations)
    coverage.written = result.inserted
    coverage.duplicates = result.duplicates
    log.info("news_plane_ingested", **coverage.as_dict())
    return coverage
