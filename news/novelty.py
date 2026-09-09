"""Novelty from structured facts — Spec O section 5.2.

> **Novelty score**: does this story contain information absent from the prior
> cluster, or is it a restatement? Computed by comparing extracted structured
> facts, **not by asking a model whether it feels new**.

So novelty here is a set operation, and the set is a small vocabulary of typed,
extractable things: money amounts, percentages, share counts, EPS figures,
ratings actions, and a fixed list of event keywords. The score is the fraction
of this story's structured facts that do not appear in the prior story for the
same symbols.

Its limitations, stated rather than papered over:

* a genuinely new development described **without a number** scores 0.0;
* a restatement that rounds a number differently scores above 0.0.

Both are the price of determinism, and both are visible in ``novelty_basis``,
which lists the facts on each side. A model-scored novelty would get those two
cases right and would be unreproducible, which is the wrong trade for something
that feeds a cohort. It is a covariate, not a signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from news.articles import normalise_text

#: Ordered, named extractors. Each yields canonical "type:value" strings, so
#: "$1.2 billion" and "$1,200,000,000" do not both need to appear to match.
MONEY = re.compile(r"\$\s*(\d+(?:[\d,]*\d)?(?:\.\d+)?)\s*(billion|bn|million|mm|thousand|k)?\b", re.I)
PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:%|percent)\b", re.I)
SHARES = re.compile(r"(\d+(?:[\d,]*\d)?(?:\.\d+)?)\s*(?:million|billion)?\s+shares\b", re.I)

MULTIPLIERS = {
    "billion": 1e9, "bn": 1e9,
    "million": 1e6, "mm": 1e6,
    "thousand": 1e3, "k": 1e3,
}

#: Event vocabulary that carries information without a number attached.
EVENT_KEYWORDS: tuple[str, ...] = (
    "acquisition", "merger", "buyback", "dividend", "guidance", "downgrade",
    "upgrade", "resigns", "resignation", "appointed", "recall", "lawsuit",
    "settlement", "bankruptcy", "delisting", "restatement", "offering",
    "split", "spin-off", "spinoff", "layoffs", "restructuring", "fda",
    "approval", "investigation", "subpoena", "default", "outage", "breach",
)


def _money_facts(text: str) -> set[str]:
    out = set()
    for amount, unit in MONEY.findall(text):
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            continue
        value *= MULTIPLIERS.get((unit or "").lower(), 1.0)
        # Canonical to 6 significant figures so "$1.2 billion" and
        # "$1,200,000,000" collapse to one fact.
        out.add(f"money:{value:.6g}")
    return out


def structured_facts(text: str) -> set[str]:
    """The typed things a story states. Order-free, so comparison is a set op."""
    body = normalise_text(text)
    facts = _money_facts(body)
    facts |= {f"percent:{float(v):.4g}" for v in PERCENT.findall(body)}
    for raw in SHARES.findall(body):
        try:
            facts.add(f"shares:{float(raw.replace(',', '')):.6g}")
        except ValueError:
            continue
    facts |= {f"event:{word}" for word in EVENT_KEYWORDS if word in body}
    return facts


@dataclass(frozen=True)
class Novelty:
    score: float
    new_facts: tuple[str, ...]
    prior_facts: tuple[str, ...]
    total_facts: int

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "new_facts": list(self.new_facts),
            "prior_facts": list(self.prior_facts),
            "total_facts": self.total_facts,
        }


def novelty(text: str, prior_texts: Sequence[str] = ()) -> Novelty:
    """Fraction of this story's facts absent from the prior stories.

    A story with no structured facts scores 0.0 with ``total_facts=0`` — which
    reads as "nothing to compare", not as "nothing new", and the basis says so.
    A story with no prior scores 1.0: the first telling of anything is new.
    """
    current = structured_facts(text)
    prior: set[str] = set()
    for earlier in prior_texts:
        prior |= structured_facts(earlier)

    if not current:
        return Novelty(0.0, (), tuple(sorted(prior)), 0)
    fresh = current - prior
    return Novelty(
        score=round(len(fresh) / len(current), 6),
        new_facts=tuple(sorted(fresh)),
        prior_facts=tuple(sorted(prior)),
        total_facts=len(current),
    )
