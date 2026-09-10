"""The tracked-investor list — Spec O section 3.2, curated by the owner.

A manager CIK, why it is tracked, and what style it is. It is deliberately a
**file, not a table**: the list is a handful of rows Bryan maintains by hand,
it belongs in code review beside the rules that use it, and a table would need
a migration, an editor and a backup for data that fits on one screen.

It ships **empty**. Naming managers here would be inventing the owner's
watchlist for him, and a wrong name is worse than an empty list because it
silently scopes every "who is buying this" answer. Add entries as::

    TrackedInvestor(
        cik="0001067983",
        name="Berkshire Hathaway Inc",
        style="concentrated_value",
        why="…",
    )

13F is out of scope for Phase 4, so this list currently feeds the 13D/G reads
only: "did a manager I track cross 5% in this name, and when did that become
knowable".
"""

from __future__ import annotations

from dataclasses import dataclass

from filings import client as sec_client


@dataclass(frozen=True)
class TrackedInvestor:
    cik: str
    name: str
    style: str = ""
    why: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", sec_client.normalise_cik(self.cik))


#: Owner-maintained. Empty by design — see the module docstring.
TRACKED_INVESTORS: tuple[TrackedInvestor, ...] = ()


def by_cik(cik: str) -> TrackedInvestor | None:
    normalised = sec_client.normalise_cik(cik)
    for investor in TRACKED_INVESTORS:
        if investor.cik == normalised:
            return investor
    return None


def is_tracked(cik: str) -> bool:
    return by_cik(cik) is not None
