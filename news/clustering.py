"""Near-duplicate coverage into one story — Spec O section 5.2.

> Cluster near-duplicate coverage into one story; a story republished by twenty
> outlets is one event, and counting it twenty times manufactures false
> momentum.

The stack is deterministic, in two passes:

1. **Exact canonical-URL match.** The same article reached through five
   tracking URLs is one article.
2. **MinHash/LSH over title-plus-lead shingles** (``datasketch``, MIT) for wire
   pickups — the case where twenty outlets rewrite the headline slightly around
   the same wire copy.

**LSH proposes; exact Jaccard decides.** ``MinHashLSH`` is a banded index and
its recall is probabilistic: a genuinely 0.57-similar pair is *missed* at a
0.45 index threshold with 128 permutations, which was reproducible on the
committed fixtures. So the index is queried at a deliberately loose threshold
to generate candidates, and every candidate pair is then confirmed by the exact
Jaccard of the shingle sets against the configured threshold. That makes the
decision exact and independent of the permutation count, and leaves LSH doing
the one thing it is good at — avoiding the quadratic comparison.

Embedding cosine is named in the spec as an *optional* third pass for
paraphrases and is deliberately not built: it would put a model in a path whose
whole selling point is that it is reproducible, and the wire-pickup case that
actually inflates counts is a near-duplicate, not a paraphrase.

**Determinism matters more than recall here.** ``datasketch``'s MinHash is
seeded explicitly, edges are collected in sorted order, and the cluster id is
the ``article_uid`` of the earliest-published member (ties broken by uid), so
the same input set always produces the same cluster ids. A cluster id that
moved between runs would make ``news_clusters`` unjoinable to anything.

**The cluster's ``known_at_utc`` is the minimum publisher timestamp across its
members, and it applies to the story event only.** A fact extracted from one
member carries *that member's* timestamp (section 5.1) — see
``news.consensus_from_news``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from datasketch import MinHash, MinHashLSH

from news.articles import Article

#: Fixed seed. MinHash permutations are pseudo-random; an unseeded hash makes
#: cluster membership differ between processes, which is not a tuning question
#: but a correctness one.
MINHASH_SEED = 1

#: The Jaccard threshold, and where it comes from. Measured on the committed
#: fixtures, over 5-word shingles of title-plus-lead:
#:
#: * a wire release and the reaction piece that reproduces its lead and adds a
#:   sentence — the same story — score **0.56**;
#: * two outlets covering the same preview with different consensus figures —
#:   also the same story — score **0.57**;
#: * a morning preview and that evening's results piece — genuinely different
#:   stories — score **0.08**.
#:
#: 0.5 sits in the wide gap between those. 0.6, the first value tried, split
#: the two same-story pairs, which is the failure that manufactures momentum:
#: one event counted twice. The measurement is in the fixtures, so it can be
#: rerun rather than believed.

DEFAULT_PERMUTATIONS = 128
DEFAULT_THRESHOLD = 0.5
DEFAULT_SHINGLE_SIZE = 5

#: How far below the decision threshold the LSH index is queried. Wide enough
#: that banding's recall loss cannot drop a true pair; every candidate it
#: proposes is then confirmed exactly, so a loose index costs comparisons, not
#: correctness.
CANDIDATE_SLACK = 0.25
MIN_CANDIDATE_THRESHOLD = 0.1


def shingles(text: str, size: int = DEFAULT_SHINGLE_SIZE) -> list[str]:
    """Overlapping word n-grams. Short texts yield one shingle, not none."""
    words = (text or "").split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    return [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]


def jaccard(left: set, right: set) -> float:
    """Exact Jaccard of two shingle sets. Two empty sets are identical."""
    if not left and not right:
        return 1.0
    union_size = len(left | right)
    return (len(left & right) / union_size) if union_size else 0.0


def minhash(text: str, *, permutations: int, shingle_size: int) -> MinHash:
    sketch = MinHash(num_perm=permutations, seed=MINHASH_SEED)
    for shingle in shingles(text, shingle_size):
        sketch.update(shingle.encode("utf-8"))
    return sketch


class _Union:
    """Union-find, keyed by article uid."""

    def __init__(self, keys: Iterable[str]):
        self._parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self._parent
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            # Lexicographic, so the merge order cannot depend on input order.
            low, high = sorted((a, b))
            self._parent[high] = low

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in sorted(self._parent):
            out.setdefault(self.find(key), []).append(key)
        return out


@dataclass(frozen=True)
class Cluster:
    """One story. ``known_at_utc`` is the story's, never a fact's."""

    cluster_id: str
    members: tuple[Article, ...]
    known_at_utc: datetime | None
    headline: str
    symbols: tuple[str, ...]
    replay_eligible: bool

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def member_uids(self) -> tuple[str, ...]:
        return tuple(a.article_uid for a in self.members)


def _sort_key(article: Article):
    """Earliest published first; undated last; uid breaks every tie."""
    stamp = article.published_at
    return (stamp is None, stamp or datetime.max, article.article_uid)


def cluster_articles(
    articles: Sequence[Article],
    *,
    permutations: int = DEFAULT_PERMUTATIONS,
    threshold: float = DEFAULT_THRESHOLD,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
) -> list[Cluster]:
    """Group articles into stories. Deterministic for a given input set."""
    unique: dict[str, Article] = {}
    for article in sorted(articles, key=_sort_key):
        unique.setdefault(article.article_uid, article)
    if not unique:
        return []

    union = _Union(unique)

    by_canonical: dict[str, list[str]] = {}
    for uid, article in sorted(unique.items()):
        canonical = article.canonical
        if canonical:
            by_canonical.setdefault(canonical, []).append(uid)
    for uids in by_canonical.values():
        for uid in uids[1:]:
            union.union(uids[0], uid)

    lsh = MinHashLSH(
        threshold=max(MIN_CANDIDATE_THRESHOLD, threshold - CANDIDATE_SLACK),
        num_perm=permutations,
    )
    shingle_sets: dict[str, set] = {}
    for uid, article in sorted(unique.items()):
        text = article.shingle_text()
        shingle_sets[uid] = set(shingles(text, shingle_size))
        sketch = minhash(text, permutations=permutations, shingle_size=shingle_size)
        for neighbour in sorted(lsh.query(sketch)):
            if jaccard(shingle_sets[neighbour], shingle_sets[uid]) >= threshold:
                union.union(neighbour, uid)
        lsh.insert(uid, sketch)

    clusters: list[Cluster] = []
    for uids in union.groups().values():
        members = tuple(sorted((unique[uid] for uid in uids), key=_sort_key))
        stamps = [a.published_at for a in members if a.published_at is not None]
        symbols = sorted({s for a in members for s in a.symbols})
        clusters.append(
            Cluster(
                cluster_id=members[0].article_uid,
                members=members,
                known_at_utc=min(stamps) if stamps else None,
                headline=members[0].headline,
                symbols=tuple(symbols),
                # A story is replay-eligible when *some* member can be dated;
                # the story's known_at is that earliest defensible timestamp.
                replay_eligible=bool(stamps),
            )
        )
    clusters.sort(key=lambda c: (c.known_at_utc is None, c.known_at_utc or datetime.max, c.cluster_id))
    return clusters
