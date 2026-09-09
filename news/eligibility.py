"""The Spec O section 5.3 eligibility matrix, enforced.

| Use | Requires | Provenance class |
|---|---|---|
| **Date an event** (Spec N event clock) | publisher timestamp + primary tier | ``vendor_pit`` |
| **Qualify a cohort** as a structured fact | publisher timestamp + tier >= established + deterministic extraction | ``vendor_pit`` |
| **Covariate or novelty context** | publisher timestamp | ``vendor_pit`` |
| **Dossier evidence, invalidator trigger** (Spec M) | any tier | — |
| **Spec Q promotion evidence** | **never** | — |

The last row is not a threshold anybody can meet. It is the reason this is a
function and not a comment: "news may never support a promotion" is only true
if there is one place that says no, and it says no to every argument.

An article missing a timestamp qualifies for nothing above the last row.
"""

from __future__ import annotations

from dataclasses import dataclass

from news.articles import (
    TIER_ESTABLISHED,
    TIER_PRIMARY,
    Article,
    tier_at_least,
)

USE_DATE_EVENT = "date_event"
USE_QUALIFY_COHORT = "qualify_cohort"
USE_COVARIATE = "covariate"
USE_DOSSIER = "dossier"
USE_PROMOTION = "promotion"

USES: tuple[str, ...] = (
    USE_DATE_EVENT,
    USE_QUALIFY_COHORT,
    USE_COVARIATE,
    USE_DOSSIER,
    USE_PROMOTION,
)

PROVENANCE_VENDOR_PIT = "vendor_pit"


class UnknownUse(ValueError):
    """A use that is not a row of the matrix. There is no default."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    use: str
    reason: str
    provenance_class: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def eligible_for(
    article: Article, use: str, *, deterministic_extraction: bool = True
) -> Decision:
    """Whether ``article`` may be used for ``use``. No use defaults to allowed."""
    if use not in USES:
        raise UnknownUse(
            f"{use!r} is not a row of the Spec O section 5.3 matrix "
            f"({', '.join(USES)}). A use not in the matrix has no rule, and a "
            "missing rule must not read as permission."
        )

    if use == USE_PROMOTION:
        return Decision(
            False,
            use,
            "Spec O section 5.3: news is never evidence for a Spec Q promotion. "
            "A promotion decides whether a strategy trades real money; the "
            "evidence for it is measured outcomes, not coverage.",
        )

    if use == USE_DOSSIER:
        return Decision(
            True,
            use,
            "Any tier, rendered with its tier (Spec M). A dossier shows a human "
            "the evidence and its provenance; it does not compute with it.",
        )

    if not article.has_timestamp:
        return Decision(
            False,
            use,
            "no publisher timestamp: an article whose publication time cannot "
            "be established qualifies for nothing but dossier evidence "
            "(Spec O section 5.1), and is stored replay_eligible=false.",
        )

    if use == USE_COVARIATE:
        return Decision(True, use, "publisher timestamp present", PROVENANCE_VENDOR_PIT)

    if use == USE_DATE_EVENT:
        if not tier_at_least(article.tier, TIER_PRIMARY):
            return Decision(
                False,
                use,
                f"tier {article.tier!r} is below 'primary'. Dating an event needs "
                "the company release or the filing itself: a wire pickup's "
                "timestamp is when the wire ran it, which is after the event.",
            )
        return Decision(True, use, "primary tier with a timestamp", PROVENANCE_VENDOR_PIT)

    # USE_QUALIFY_COHORT
    if not tier_at_least(article.tier, TIER_ESTABLISHED):
        return Decision(
            False,
            use,
            f"tier {article.tier!r} is below 'established'. An aggregator "
            "republishes someone else's reporting; qualifying a cohort on it "
            "puts the aggregator's editorial choices into the sample.",
        )
    if not deterministic_extraction:
        return Decision(
            False,
            use,
            "the fact was not produced by a deterministic extractor (Spec N "
            "section 4.0). A cohort must be reproducible from stored inputs.",
        )
    return Decision(
        True, use, "established tier, timestamped, deterministic extraction",
        PROVENANCE_VENDOR_PIT,
    )
