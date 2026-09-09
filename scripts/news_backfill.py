#!/usr/bin/env python3
"""Backfill the news plane: fetch, cluster, store, write.

    python -m scripts.news_backfill --symbols AAPL MSFT --start 2026-01-01
    python -m scripts.news_backfill --symbols FCTX --fixtures tests/fixtures/news

Articles come from Alpaca (Benzinga), are clustered into stories, and produce
two kinds of ledger row: one ``news_story`` per cluster at the earliest
publisher timestamp, and one ``consensus_eps_news`` per (cluster, symbol) where
the deterministic extractor found a figure — at **that article's** timestamp.

**Nothing this job writes may be mirrored.** Every ledger row carries
``mirror_allowed=false`` and article bodies stay in ``news_articles``; Alpaca's
terms bar sharing the data or any derived products (Spec O section 5).

Writing requires ``PLANE_NEWS_ENABLED=true``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from database.db import get_session, init_db  # noqa: E402
from news.alpaca_news import article_from_payload, client_from_settings  # noqa: E402
from news.ingest import NewsCoverage, ingest_articles  # noqa: E402


def fixture_articles(directory: Path, symbols: list[str]) -> list:
    """Replay recorded Alpaca payloads from ``directory``."""
    out = []
    wanted = {s.upper() for s in symbols}
    for path in sorted(Path(directory).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "news" not in payload:
            continue
        for entry in payload["news"]:
            article = article_from_payload(entry)
            if not wanted or wanted & set(article.symbols):
                out.append(article)
    return out


def format_report(coverage: NewsCoverage) -> str:
    lines = [
        "News plane — coverage",
        f"  articles seen                {coverage.articles_seen}",
        f"  articles stored (new)        {coverage.articles_stored}",
        f"  articles without a timestamp {coverage.undated_articles}  (quarantined)",
        f"  stories (clusters)           {coverage.clusters}",
        f"  stories with a timestamp     {coverage.clusters_dated}",
        f"  story rows written           {coverage.story_rows}",
        f"  consensus_eps_news rows      {coverage.consensus_rows}",
        f"  observations written         {coverage.written}",
        f"  observations already present {coverage.duplicates}",
    ]
    if coverage.articles_seen:
        ratio = coverage.clusters / coverage.articles_seen
        lines.append(f"  dedup ratio                  {ratio:.2f} stories per article")
    if coverage.tier_counts:
        lines.append("")
        lines.append("  source tiers")
        for tier, count in sorted(coverage.tier_counts.items()):
            lines.append(f"    {tier:<16} {count}")
    lines.append("")
    lines.append("  every row written carries mirror_allowed=false (Spec O section 5)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", help="YYYY-MM-DD publication floor")
    parser.add_argument("--end", help="YYYY-MM-DD publication ceiling")
    parser.add_argument("--fixtures", help="replay recorded payloads from this directory")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--report", help="write the coverage report as JSON to this path")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = parser.parse_args(argv)

    settings = Settings()
    init_db(settings.database_url)

    if args.fixtures:
        articles = fixture_articles(Path(args.fixtures), args.symbols)
    else:
        if not settings.alpaca_api_key or not settings.alpaca_secret_key:
            raise SystemExit(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY are required. The News API "
                "is free on the account the broker adapter already uses; or pass "
                "--fixtures to replay recorded payloads."
            )
        with client_from_settings(settings) as client:
            articles = client.fetch(
                args.symbols,
                start=date.fromisoformat(args.start) if args.start else None,
                end=date.fromisoformat(args.end) if args.end else None,
                max_pages=args.max_pages,
            )

    with get_session() as session:
        coverage = ingest_articles(session, articles, settings=settings)
        if args.dry_run:
            session.rollback()

    print(format_report(coverage))
    if args.report:
        Path(args.report).write_text(
            json.dumps(asdict(coverage), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n  report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
