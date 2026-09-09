"""Nothing news-derived leaves Postgres — Spec O section 5.

Alpaca's market-data terms bar sharing, selling or publishing the data "or any
derived products or services", and bar combining it with other sources for
redistribution (verification claim 11). Spec K section 3.3 draws the line: a
dossier may cite a story **by URL and date**, and that is all. No article text,
no novelty score, no news-derived feature appears in the ``research/`` mirror.

The mirror does not exist yet — it is Spec M's. The rule is written now, as
code with a test, because the alternative is a rule in a document that the
person building the mirror has to remember. ``test_news_derivatives_stay_in_postgres``
is the check; this module is what it checks.

Two ways in are closed:

**The marker.** Every ledger row this plane writes carries
``mirror_allowed=False`` in its payload (``filings.observations``). The guard
refuses any row that says so, whatever else it contains.

**The content.** A file is refused when it carries an article body, a novelty
score, or any field name this plane owns — because a well-meaning exporter that
never looked at the marker is exactly the failure mode a marker alone does not
stop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable

from filings.observations import MIRROR_ALLOWED_KEY

#: Field names that only exist because of the news plane. Any of them in a
#: mirror payload means a news derivative is being copied out.
NEWS_DERIVED_FIELDS: frozenset[str] = frozenset({
    "body", "article_body", "lead", "headline", "novelty_score",
    "novelty_basis", "new_facts", "prior_facts", "cluster_id",
    "article_uid", "content_hash", "consensus_eps_news", "mentions",
    "matched_text", "structured_facts", "member_uids", "publisher",
})

#: Tables whose contents may never be mirrored at all.
FORBIDDEN_TABLES: frozenset[str] = frozenset({"news_articles", "news_clusters"})

#: What a dossier *may* carry about a story: the citation, and nothing else.
ALLOWED_CITATION_FIELDS: frozenset[str] = frozenset({"url", "source_url", "published_at", "date", "title"})

_NEWS_SOURCE = re.compile(r"^(?:alpaca_news|finnhub_news|news_)", re.IGNORECASE)


class MirrorRefused(RuntimeError):
    """A payload carrying news-derived content was offered to the mirror."""


@dataclass
class MirrorCheck:
    allowed: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.allowed


def is_news_source(source: str) -> bool:
    return bool(_NEWS_SOURCE.match((source or "").strip()))


def _walk(payload: Any, path: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, str(key), value
            yield from _walk(value, here)
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            here = f"{path}[{index}]"
            yield from _walk(value, here)


def check_payload(payload: Any, *, context: str = "payload") -> MirrorCheck:
    """Whether one JSON-shaped object may be copied to the mirror."""
    reasons: list[str] = []

    if isinstance(payload, dict):
        if payload.get(MIRROR_ALLOWED_KEY) is False:
            reasons.append(f"{context}: carries {MIRROR_ALLOWED_KEY}=false")
        table = str(payload.get("table") or payload.get("__table__") or "")
        if table in FORBIDDEN_TABLES:
            reasons.append(f"{context}: table {table!r} is news-plane state")
        if is_news_source(str(payload.get("source") or "")):
            reasons.append(f"{context}: source {payload.get('source')!r} is a news source")

    for path, key, value in _walk(payload):
        if value is False and key == MIRROR_ALLOWED_KEY:
            reasons.append(f"{context}: {path} is false")
        if key in NEWS_DERIVED_FIELDS and key not in ALLOWED_CITATION_FIELDS:
            reasons.append(f"{context}: {path} is a news-derived field ({key})")

    return MirrorCheck(not reasons, tuple(dict.fromkeys(reasons)))


def check_file(path) -> MirrorCheck:
    """Whether a file staged for the mirror may be written.

    JSON is parsed and walked; anything else is scanned for the field names as
    text, because a CSV header row is just as much a leak as a JSON key.
    """
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        return check_payload(json.loads(text), context=file_path.name)
    except ValueError:
        pass

    lowered = text.lower()
    reasons = [
        f"{file_path.name}: contains the news-derived field name {field!r}"
        for field in sorted(NEWS_DERIVED_FIELDS - ALLOWED_CITATION_FIELDS)
        if field in lowered
    ]
    if f'"{MIRROR_ALLOWED_KEY}": false' in lowered or f"{MIRROR_ALLOWED_KEY}=false" in lowered:
        reasons.append(f"{file_path.name}: carries {MIRROR_ALLOWED_KEY}=false")
    return MirrorCheck(not reasons, tuple(reasons))


def require_mirrorable(payload: Any, *, context: str = "payload") -> None:
    """Raise unless ``payload`` may be mirrored. The enforcement entry point."""
    check = check_payload(payload, context=context)
    if not check.allowed:
        raise MirrorRefused(
            "Refusing to mirror news-derived content (Spec O section 5; Alpaca's "
            "terms bar sharing the data 'or any derived products'). A dossier may "
            "cite a story by URL and date and nothing else.\n  "
            + "\n  ".join(check.reasons)
        )
