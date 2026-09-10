"""The article record — Spec O sections 5.1 and 5.2.

Three things travel with every article and none of them is optional:

**The publisher timestamp**, or its explicit absence. An article whose
publication time cannot be established is stored with ``replay_eligible=False``
and is unusable in a ``clean_pit`` cohort
(``test_untimestamped_news_not_replay_eligible``). It is *quarantined*, not
dropped — "we saw this and could not date it" is worth keeping.

**The source tier.** primary (company release, filing) > established
wire/publication > aggregator > unattributed. The tier travels with the fact,
because Spec O section 5.3 gates what a fact may be used for on it.

**A canonical URL.** The cheap half of deduplication: twenty outlets running a
wire story is one event, and one outlet's article reached through five tracking
URLs is one article.

An unrecognised publisher is ``unattributed``, never ``established``. That is
the fail-closed direction: an unknown publisher can still be dossier evidence
(section 5.3's last row), and mis-tiering it upward would let it qualify a
cohort.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

TIER_PRIMARY = "primary"
TIER_ESTABLISHED = "established"
TIER_AGGREGATOR = "aggregator"
TIER_UNATTRIBUTED = "unattributed"

#: Ordered worst to best, so ``TIER_ORDER.index`` compares tiers.
TIER_ORDER: tuple[str, ...] = (
    TIER_UNATTRIBUTED,
    TIER_AGGREGATOR,
    TIER_ESTABLISHED,
    TIER_PRIMARY,
)

WARN_NO_PUBLISHER_TIMESTAMP = "publisher_timestamp_absent"
WARN_UNKNOWN_PUBLISHER = "publisher_not_in_tier_table"

#: Wire services and issuer channels: the company's own words, undigested.
PRIMARY_PUBLISHERS = frozenset({
    "business wire", "businesswire", "pr newswire", "prnewswire",
    "globe newswire", "globenewswire", "accesswire", "ace news",
    "sec", "sec edgar", "u.s. securities and exchange commission",
    "company press release", "issuer",
})

#: Wires and publications with editorial standards and a masthead.
ESTABLISHED_PUBLISHERS = frozenset({
    "reuters", "bloomberg", "dow jones", "dow jones newswires",
    "the wall street journal", "wall street journal", "wsj",
    "associated press", "ap", "financial times", "ft",
    "cnbc", "barron's", "barrons", "marketwatch", "benzinga",
    "the new york times", "new york times", "the washington post",
    "the economist", "forbes", "fortune", "axios", "politico",
})

#: Republishers and syndicators. They may carry a story first, but the story
#: is someone else's and the timestamp is the republication's.
AGGREGATOR_PUBLISHERS = frozenset({
    "yahoo finance", "yahoo", "google news", "msn", "msn money",
    "seeking alpha", "zacks", "zacks investment research", "investorplace",
    "the motley fool", "motley fool", "simply wall st", "tipranks",
    "insider monkey", "gurufocus", "stocktwits", "investing.com",
    "247wallst", "24/7 wall st.", "newsfilecorp", "streetinsider",
})

#: Query parameters that never change what an article says.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "referrer", "source", "yptr", "guccounter", "guce_referrer",
    "guce_referrer_sig", "amp", "cmpid", "partner", "smid", "ito",
})


def tier_for_publisher(publisher: str) -> tuple[str, tuple[str, ...]]:
    """``(tier, warnings)``. An unknown name lands at ``unattributed``."""
    name = (publisher or "").strip().lower()
    if not name:
        return TIER_UNATTRIBUTED, (WARN_UNKNOWN_PUBLISHER,)
    if name in PRIMARY_PUBLISHERS:
        return TIER_PRIMARY, ()
    if name in ESTABLISHED_PUBLISHERS:
        return TIER_ESTABLISHED, ()
    if name in AGGREGATOR_PUBLISHERS:
        return TIER_AGGREGATOR, ()
    return TIER_UNATTRIBUTED, (WARN_UNKNOWN_PUBLISHER,)


def tier_at_least(tier: str, minimum: str) -> bool:
    """Is ``tier`` at or above ``minimum`` in the ordering?"""
    try:
        return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)
    except ValueError:
        return False


def canonical_url(url: str) -> str:
    """Strip everything that does not change which article this is.

    Scheme forced to ``https``, host lower-cased and de-``www``-ed, tracking
    parameters removed, remaining query sorted, fragment dropped, trailing
    slash removed. Two URLs that survive to the same string are the same
    article; that is the exact-match half of clustering.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "//" in raw else f"https://{raw}")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path.rstrip("/") or "/"
    rebuilt_query = "&".join(f"{k}={v}" for k, v in query)
    return urlunsplit(("https", host, path, rebuilt_query, ""))


_WHITESPACE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    """Lower-case, collapse whitespace. The basis of every hash here."""
    return _WHITESPACE.sub(" ", (text or "").strip().lower())


def content_hash(headline: str, body: str) -> str:
    blob = f"{normalise_text(headline)}\n{normalise_text(body)}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Article:
    """One article, as the plane stores it.

    ``published_at`` is the *publisher's* timestamp. ``first_seen_at`` is when
    we fetched it; they are different facts and neither substitutes for the
    other. A story republished tomorrow with the same body is a new article
    row, not an update, because "when was this available" has two answers.
    """

    source: str
    publisher: str
    headline: str
    url: str
    body: str = ""
    lead: str = ""
    symbols: tuple[str, ...] = ()
    published_at: datetime | None = None
    first_seen_at: datetime | None = None
    provider_id: str | None = None
    revision_of_uid: str | None = None
    tier_override: str | None = None
    extra_warnings: tuple[str, ...] = ()

    # -- derived ---------------------------------------------------------
    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def tier(self) -> str:
        if self.tier_override:
            return self.tier_override
        return tier_for_publisher(self.publisher)[0]

    @property
    def has_timestamp(self) -> bool:
        return self.published_at is not None

    @property
    def replay_eligible(self) -> bool:
        """Spec O section 5.1: no publication time, no replay. Full stop."""
        return self.has_timestamp

    @property
    def content_digest(self) -> str:
        return content_hash(self.headline, self.body or self.lead)

    @property
    def article_uid(self) -> str:
        """Stable identity: source, provider id or canonical URL, content.

        The content is in the key on purpose — an article revised in place at
        the publisher is a *new row* (section 5.1), and a uid that ignored the
        body would make the revision an idempotent no-op.
        """
        key = "|".join(
            [
                self.source,
                self.provider_id or self.canonical or normalise_text(self.headline),
                self.content_digest,
            ]
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @property
    def warnings(self) -> tuple[str, ...]:
        out = list(self.extra_warnings)
        if not self.has_timestamp:
            out.append(WARN_NO_PUBLISHER_TIMESTAMP)
        if self.tier_override is None:
            out.extend(tier_for_publisher(self.publisher)[1])
        return tuple(dict.fromkeys(out))

    def shingle_text(self) -> str:
        """Title plus lead — what MinHash is computed over (section 5.2)."""
        return normalise_text(f"{self.headline} {self.lead or self.body[:400]}")


def earliest_timestamp(articles: Iterable[Article]) -> datetime | None:
    """The minimum publisher timestamp across a set. ``None`` if none has one."""
    stamps = [a.published_at for a in articles if a.published_at is not None]
    return min(stamps) if stamps else None


def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
