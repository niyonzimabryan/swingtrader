"""``news_timeline`` — the stable read API for the news plane (Spec K section 4.2).

Timestamped, deduplicated coverage for a ticker, as it was knowable at a
cutoff. One row per **story**, not per republication.

No ``workspace/`` service package exists on ``main`` yet (Phase 0b), so this is
a plain function with a stable signature; registering it as an MCP tool is the
ten-line follow-up in ``docs/NEWS_PLANE.md``.

**What this returns may not be mirrored.** Every row carries
``mirror_allowed=False`` and the response says so in ``provenance.notes``, so a
caller copying it into a dossier export hits ``news.mirror_guard`` rather than
discovering the licence problem later. Headlines and URLs are included because
Spec K section 3.3 permits a dossier to cite a story by URL and date; article
bodies are not returned by this function at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from database.models import NewsArticle
from filings import provenance
from filings.observations import observations_known_at
from news.eligibility import USES, eligible_for
from news.ingest import FACT_TYPE_CONSENSUS_EPS, FACT_TYPE_STORY, SOURCE_NEWS_PLANE

NEWS_FACT_TYPES: tuple[str, ...] = (FACT_TYPE_STORY, FACT_TYPE_CONSENSUS_EPS)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def news_timeline(
    session,
    *,
    ticker: str,
    as_of: datetime | None = None,
    limit: int = 50,
    fact_types: Sequence[str] = NEWS_FACT_TYPES,
    include_quarantined: bool = False,
) -> dict:
    """Stories and news-derived facts knowable at ``as_of``, newest first.

    ``include_quarantined`` adds the undated articles — the ones stored with
    ``replay_eligible=False`` because their publication time could not be
    established. They are shown for a human to look at, never counted in the
    story rows, and they are not cohort-eligible under any row of the
    eligibility matrix except dossier evidence.
    """
    cutoff = _aware(as_of or datetime.now(timezone.utc))
    symbol = (ticker or "").strip().upper()

    rows = observations_known_at(
        session,
        fact_type=list(fact_types),
        cutoff=cutoff,
        ticker_at_time=symbol,
        source=SOURCE_NEWS_PLANE,
    )
    rows = sorted(rows, key=lambda r: (r.known_at_utc, r.id), reverse=True)[: max(0, limit)]

    stories = []
    for row in rows:
        payload = row.payload
        stories.append(
            {
                "fact_type": row.fact_type,
                "headline": row.value_text,
                "value_numeric": row.value_numeric,
                "unit": row.unit,
                "known_at_utc": row.known_at_utc.isoformat(),
                "valid_at": row.valid_at.isoformat(),
                "known_at_source": payload.get("known_at_source"),
                "cluster_id": payload.get("cluster_id"),
                "member_count": payload.get("member_count"),
                "publishers": payload.get("publishers"),
                "best_tier": payload.get("best_tier"),
                "novelty_score": payload.get("novelty_score"),
                "source_url": row.source_url,
                "source_trust": row.source_trust,
                "replay_eligible": row.replay_eligible,
                "warnings": row.warnings,
                "mirror_allowed": False,
            }
        )

    quarantined = []
    if include_quarantined:
        for article in (
            session.query(NewsArticle)
            .filter(NewsArticle.replay_eligible.is_(False))
            .order_by(NewsArticle.first_seen_at_utc.desc())
            .limit(limit)
            .all()
        ):
            if symbol and symbol not in article.symbol_list:
                continue
            quarantined.append(
                {
                    "article_uid": article.article_uid,
                    "headline": article.headline,
                    "publisher": article.publisher,
                    "tier": article.tier,
                    "url": article.url,
                    "first_seen_at_utc": article.first_seen_at_utc.isoformat(),
                    "why": "no publisher timestamp; replay_eligible=false",
                }
            )

    latest = max((r.known_at_utc for r in rows), default=None)
    return {
        "ticker": symbol,
        "stories": stories,
        "quarantined_articles": quarantined,
        "eligibility_matrix": {use: None for use in USES},
        "provenance": provenance.build(
            as_of=cutoff,
            rows=rows,
            sources={SOURCE_NEWS_PLANE: "Alpaca News (Benzinga), cross-checked against Finnhub"},
            staleness={"days_since_latest_story": provenance.staleness_days(cutoff, latest)},
            notes=[
                "mirror_allowed=false on every row: Alpaca's terms bar sharing "
                "the data or any derived products, so nothing here may reach the "
                "research/ mirror (Spec O section 5). A dossier may cite a story "
                "by URL and date, and that is all.",
                "A story's known_at_utc is the earliest publisher timestamp in "
                "its cluster; a fact extracted from an article carries that "
                "article's timestamp instead (Spec O section 5.1).",
            ],
        ),
    }


def eligibility_report(article, *, deterministic_extraction: bool = True) -> dict:
    """Every row of the section 5.3 matrix, decided for one article."""
    return {
        use: {
            "allowed": bool(decision),
            "reason": decision.reason,
            "provenance_class": decision.provenance_class,
        }
        for use in USES
        for decision in [
            eligible_for(article, use, deterministic_extraction=deterministic_extraction)
        ]
    }
