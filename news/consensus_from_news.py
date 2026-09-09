"""``consensus_eps_news`` — a deterministic extractor, not a model.

Spec N section 4.0 (owner's suggestion, 2026-09-06):

> Earnings previews and reaction pieces routinely state the number — "analysts
> expect EPS of $1.23", "beat the $1.18 consensus by five cents" — and a
> Benzinga article via Alpaca carries a publisher timestamp, so the figure is
> point-in-time by construction.

Two design commitments, both load-bearing:

**A regex grammar, never a model.** A model reading a number out of a sentence
is parsing rather than producing a statistic, so it would not violate the
letter of Spec N section 9 — but it is not reproducible, and a consensus figure
that changes when the model is upgraded silently relabels every cohort built
on it. The patterns are listed, ordered, tested, and each match records the
pattern that produced it.

**``known_at_utc`` is the article's, never the cluster's.** Spec O section 5.1:
a number that first appears in a 16:45 reaction piece must not inherit the
07:00 preview's timestamp, or it becomes available before it existed.
``test_fact_known_at_is_article_not_cluster`` is exactly that case.

**Disagreement is recorded, not resolved away.** Zacks, FactSet and Refinitiv
consensus differ and articles cite whichever their author uses. When articles
disagree, the observation carries every cited value, the count behind each, and
the spread — and the headline value is chosen by a stated deterministic rule
(most-cited wins; the earliest-published article breaks a tie), not by picking
the first match seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from news.articles import Article

FACT_TYPE_CONSENSUS_EPS = "consensus_eps_news"

WARN_CONSENSUS_DISAGREEMENT = "consensus_sources_disagree"
WARN_CONSENSUS_SINGLE_SOURCE = "consensus_single_article"

#: Money amounts in the range an EPS figure plausibly occupies. A "$4.1B
#: revenue" in the same sentence must not be read as EPS, so the patterns
#: anchor on EPS vocabulary and the value is range-checked afterwards.
MIN_PLAUSIBLE_EPS = -100.0
MAX_PLAUSIBLE_EPS = 100.0

_NUM = r"\(?\$\s*(?P<value>-?\d+(?:\.\d+)?)\)?"

#: Ordered, named patterns. Each is anchored on explicit consensus vocabulary
#: *and* on EPS vocabulary, because "analysts expect $4.1 billion" is a revenue
#: estimate and reading it as EPS would be worse than extracting nothing.
PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "analysts_expect_eps_of",
        re.compile(
            r"(?:analysts?|the street|wall street)\s+(?:are\s+)?"
            r"(?:expect(?:ed|ing)?|estimat(?:e|ed|ing)|forecast(?:ed|ing)?|"
            r"anticipat(?:e|ed))\s+"
            r"(?:adjusted\s+|non-?gaap\s+)?(?:eps|earnings per share)\s+of\s+" + _NUM,
            re.IGNORECASE,
        ),
    ),
    (
        "consensus_estimate_of",
        re.compile(
            r"(?:consensus|street)\s+(?:eps\s+|earnings(?:\s+per\s+share)?\s+)?"
            r"(?:estimate|forecast|view|expectation)\s+(?:of|was|is|at)\s+" + _NUM,
            re.IGNORECASE,
        ),
    ),
    (
        "the_value_consensus",
        re.compile(
            r"(?:the\s+)?" + _NUM + r"\s+"
            r"(?:analyst\s+|street\s+)?(?:consensus|estimate|expectation)"
            r"(?:\s+for\s+(?:eps|earnings per share))?",
            re.IGNORECASE,
        ),
    ),
    (
        "eps_estimate_of",
        re.compile(
            r"(?:eps|earnings per share)\s+"
            r"(?:consensus|estimate|forecast|expectation)s?\s+"
            r"(?:of|was|is|at|stood at)\s+" + _NUM,
            re.IGNORECASE,
        ),
    ),
    (
        "versus_expected",
        re.compile(
            r"(?:versus|vs\.?|against|compared (?:to|with))\s+(?:the\s+)?" + _NUM
            + r"\s+(?:analysts?\s+)?(?:expected|estimate[sd]?|consensus|forecast)",
            re.IGNORECASE,
        ),
    ),
    (
        "beat_missed_the_estimate",
        re.compile(
            r"(?:beat|missed|topped|matched|fell short of)\s+"
            r"(?:the\s+)?(?:street'?s?\s+|analysts?'?\s+|consensus\s+)?"
            r"(?:eps\s+|earnings(?:\s+per\s+share)?\s+)?"
            r"(?:estimate|forecast|consensus|expectation)s?\s+of\s+" + _NUM,
            re.IGNORECASE,
        ),
    ),
    (
        "beat_the_value_consensus",
        re.compile(
            r"(?:beat|missed|topped|matched)\s+(?:the\s+)?" + _NUM
            + r"\s+(?:consensus|estimate|expectation)",
            re.IGNORECASE,
        ),
    ),
)

#: Vocabulary that means the number is a *revenue* figure. A sentence carrying
#: it is skipped entirely rather than partially trusted.
REVENUE_CONTEXT = re.compile(
    r"\b(?:revenue|sales|billion|bn\b|million|mm\b|top line|topline)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class ConsensusMention:
    """One consensus figure, and the article and pattern it came from."""

    value: float
    pattern: str
    matched_text: str
    article_uid: str
    published_at: datetime | None
    publisher: str
    tier: str
    source_url: str

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "pattern": self.pattern,
            "matched_text": self.matched_text,
            "article_uid": self.article_uid,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "publisher": self.publisher,
            "tier": self.tier,
        }


def _sentences(text: str) -> list[str]:
    """Split on sentence enders, keeping it dumb and therefore reproducible."""
    return [part for part in re.split(r"(?<=[.!?])\s+", text or "") if part.strip()]


def extract_from_text(text: str) -> list[tuple[float, str, str]]:
    """``[(value, pattern name, matched text)]`` in a fixed order.

    Every pattern is tried against every sentence, and the results are sorted
    by (pattern index, position) so the same text always yields the same list.
    A sentence carrying revenue vocabulary is skipped: "$4.1 billion consensus"
    is not an EPS figure, and reading it as one puts a four-billion-dollar EPS
    estimate into a cohort.
    """
    found: list[tuple[int, int, float, str, str]] = []
    for sentence in _sentences(text or ""):
        if REVENUE_CONTEXT.search(sentence):
            continue
        for index, (name, pattern) in enumerate(PATTERNS):
            for match in pattern.finditer(sentence):
                try:
                    value = float(match.group("value"))
                except (TypeError, ValueError):
                    continue
                if not (MIN_PLAUSIBLE_EPS <= value <= MAX_PLAUSIBLE_EPS):
                    continue
                found.append((index, match.start(), value, name, match.group(0).strip()))
    found.sort()
    seen: set[tuple[float, str]] = set()
    out: list[tuple[float, str, str]] = []
    for _index, _pos, value, name, text_match in found:
        key = (value, text_match.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((value, name, text_match))
    return out


def extract_from_article(article: Article) -> list[ConsensusMention]:
    """Every consensus figure this one article states, with its own timestamp."""
    body = " ".join(part for part in (article.headline, article.lead, article.body) if part)
    return [
        ConsensusMention(
            value=value,
            pattern=name,
            matched_text=matched,
            article_uid=article.article_uid,
            published_at=article.published_at,
            publisher=article.publisher,
            tier=article.tier,
            source_url=article.url,
        )
        for value, name, matched in extract_from_text(body)
    ]


@dataclass(frozen=True)
class ConsensusReading:
    """What a set of articles collectively says the consensus was.

    ``known_at_utc`` is **the timestamp of the article the chosen value came
    from** — Spec O section 5.1 — not the earliest article in the story.
    """

    value: float
    known_at_utc: datetime | None
    source_article_uid: str
    source_url: str
    mentions: tuple[ConsensusMention, ...]
    distinct_values: tuple[float, ...]
    agreeing_articles: int
    total_articles: int
    spread: float
    warnings: tuple[str, ...]

    @property
    def disagreement(self) -> bool:
        return len(self.distinct_values) > 1

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "known_at_utc": self.known_at_utc.isoformat() if self.known_at_utc else None,
            "source_article_uid": self.source_article_uid,
            "distinct_values": list(self.distinct_values),
            "agreeing_articles": self.agreeing_articles,
            "total_articles": self.total_articles,
            "spread": self.spread,
            "disagreement": self.disagreement,
            "mentions": [m.as_dict() for m in self.mentions],
        }


def _mention_order(mention: ConsensusMention):
    return (
        mention.published_at is None,
        mention.published_at or datetime.max,
        mention.article_uid,
    )


def read_consensus(articles: Sequence[Article]) -> ConsensusReading | None:
    """Collapse a set of articles to one consensus reading, or ``None``.

    The selection rule, stated so it can be argued with rather than guessed at:

    1. Only articles carrying a publisher timestamp count toward the value —
       an undated article cannot date a fact (section 5.1).
    2. The value cited by the **most distinct articles** wins.
    3. A tie goes to the value in the **earliest-published** article, because
       that is the one that was available longest.

    When articles disagree, every value is kept in ``distinct_values`` with the
    spread, and the reading is flagged. Averaging them would invent a number no
    source published.
    """
    mentions: list[ConsensusMention] = []
    for article in sorted(articles, key=lambda a: (a.published_at is None, a.published_at or datetime.max, a.article_uid)):
        mentions.extend(extract_from_article(article))
    dated = [m for m in mentions if m.published_at is not None]
    if not dated:
        return None

    by_value: dict[float, list[ConsensusMention]] = {}
    for mention in sorted(dated, key=_mention_order):
        by_value.setdefault(mention.value, []).append(mention)

    def rank(item):
        value, group = item
        articles_citing = len({m.article_uid for m in group})
        earliest = min(_mention_order(m) for m in group)
        return (-articles_citing, earliest, value)

    chosen_value, chosen_group = sorted(by_value.items(), key=rank)[0]
    chosen = sorted(chosen_group, key=_mention_order)[0]

    values = tuple(sorted(by_value))
    spread = (max(values) - min(values)) if len(values) > 1 else 0.0
    warnings: list[str] = []
    if len(values) > 1:
        warnings.append(WARN_CONSENSUS_DISAGREEMENT)
    total_articles = len({m.article_uid for m in dated})
    if total_articles < 2:
        warnings.append(WARN_CONSENSUS_SINGLE_SOURCE)

    return ConsensusReading(
        value=chosen_value,
        known_at_utc=chosen.published_at,
        source_article_uid=chosen.article_uid,
        source_url=chosen.source_url,
        mentions=tuple(sorted(dated, key=_mention_order)),
        distinct_values=values,
        agreeing_articles=len({m.article_uid for m in chosen_group}),
        total_articles=total_articles,
        spread=round(spread, 6),
        warnings=tuple(warnings),
    )
