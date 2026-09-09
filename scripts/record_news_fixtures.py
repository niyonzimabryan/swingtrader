#!/usr/bin/env python3
"""Record real Alpaca news payloads for **local** use.

    export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...
    python -m scripts.record_news_fixtures --symbols AAPL --start 2026-01-01 \
        --out /some/path/outside/the/repo

**Do not commit what this writes.** Alpaca's terms bar sharing or publishing
the data "or any derived products" (verification claim 11), and this repository
is public. The committed fixtures in ``tests/fixtures/news/`` are synthetic *by
policy*, not only because the host was unreachable — see the README there.

The script therefore refuses to write inside ``tests/fixtures/news`` unless
``--i-understand-the-licence`` is passed, and prints the reason. Use a
recording to check the adapter against the live payload shape, then delete it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from news.alpaca_news import NEWS_PATH, client_from_settings  # noqa: E402

COMMITTED_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "news"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--i-understand-the-licence", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    if out_dir == COMMITTED_FIXTURES and not args.i_understand_the_licence:
        raise SystemExit(
            "Refusing to write recorded Alpaca content into the committed "
            "fixture directory. Alpaca's terms bar sharing the data or any "
            "derived products, and this repository is public. Write it "
            "somewhere outside the repo, or pass "
            "--i-understand-the-licence if you have written permission."
        )

    settings = Settings()
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY are required.")

    out_dir.mkdir(parents=True, exist_ok=True)
    with client_from_settings(settings) as client:
        articles = client.fetch(
            args.symbols,
            start=date.fromisoformat(args.start) if args.start else None,
            end=date.fromisoformat(args.end) if args.end else None,
            max_pages=args.max_pages,
        )

    # Re-serialise to the payload shape so the recording is a drop-in for the
    # committed fixtures rather than a different format nothing else reads.
    payload = {
        "news": [
            {
                "id": article.provider_id,
                "headline": article.headline,
                "summary": article.lead,
                "content": article.body,
                "source": article.publisher,
                "url": article.url,
                "symbols": list(article.symbols),
                "created_at": (
                    article.published_at.isoformat().replace("+00:00", "Z")
                    if article.published_at
                    else None
                ),
                "updated_at": None,
            }
            for article in articles
        ],
        "next_page_token": None,
    }
    name = "_".join(sorted(s.upper() for s in args.symbols))[:40] or "news"
    path = out_dir / f"recorded_{name}.json"
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"  wrote {path} ({len(articles)} articles, {path.stat().st_size:,} bytes)")
    print(f"  endpoint: {NEWS_PATH}")
    print("  Do not commit this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
