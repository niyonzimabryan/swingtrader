"""Source tiers, and the two independent decisions they drive.

A source answers two different questions, and conflating them produces either a
leaky repo or a mirror full of holes:

**Is the content trusted?** (Spec P §5.) Filing text, news bodies, and scraped
pages are written by people who are not us — anyone who can file with the SEC or
issue a press release can put text in front of this system. That text is *data*,
never instruction. Sections resting on it are marked ``content_trust:
"untrusted"`` wherever they are rendered, and ``research_write`` refuses a
section whose sources are **all** untrusted-tier unless a human says they wrote
it (:func:`refuses_all_untrusted`).

**May it reach the public repo?** (Spec K §3.3.) The repo is public and vendor
licences forbid redistribution — Tiingo's internal-use licence and Alpaca's bar
on publishing "any derived products or services" are the verified clauses. News
bodies and news-derived features are out for the same reason. So the mirror
withholds those tiers and carries a marker with the Postgres id in their place.

The two are orthogonal. An EDGAR filing is untrusted content that may be
committed; a vendor fundamentals series is untrusted content that may not. One
enum, two lookups, no inference from source names.

`web_scrape` is withheld from the mirror although Spec M §5 names only news and
vendor-data. The rationale in Spec K §3.3 is third-party copyrighted prose in a
public repository, which is exactly what an arbitrary scraped page is; the
spec's list is a floor, and widening it withholds more rather than less. Flagged
for ratification in the Phase 2 PR.
"""

from __future__ import annotations

#: Written by us, or computed by us. Trusted content.
HUMAN = "human"
MODEL = "model"
INTERNAL_ANALYSIS = "internal_analysis"

#: Written by someone else. Untrusted content (Spec P §5).
PRIMARY_REGULATOR = "primary_regulator"  # EDGAR. Anyone who can file can write it.
COMPANY_DISCLOSURE = "company_disclosure"  # IR page, issuer press release.
NEWS = "news"
VENDOR_DATA = "vendor_data"  # Tiingo, Polygon, Sharadar, FMP, yfinance.
WEB_SCRAPE = "web_scrape"

TIERS: tuple[str, ...] = (
    HUMAN,
    MODEL,
    INTERNAL_ANALYSIS,
    PRIMARY_REGULATOR,
    COMPANY_DISCLOSURE,
    NEWS,
    VENDOR_DATA,
    WEB_SCRAPE,
)

#: Written by us: our own prose, and our own computations.
SELF_AUTHORED_TIERS = frozenset({HUMAN, MODEL, INTERNAL_ANALYSIS})

#: Content this system did not write. Marked ``untrusted`` in every response and
#: wrapped in nonce delimiters wherever it is rendered (Spec P §5). A filing is
#: in here: anyone who can file with the SEC can put text in front of this
#: system, and that text is data, never instruction.
UNTRUSTED_TIERS = frozenset(
    {PRIMARY_REGULATOR, COMPANY_DISCLOSURE, NEWS, VENDOR_DATA, WEB_SCRAPE}
)

#: Third-party text and data that is **not** attributable to a named filer or
#: issuer. Two rules key on this set, for two different reasons that happen to
#: pick out the same tiers — keep them as two constants, because a later phase
#: may well want to move a tier in one and not the other:
#:
#: * :func:`refuses_all_untrusted` — a section resting *only* on this needs a
#:   human to claim authorship (Spec P §5).
#: * :data:`MIRROR_WITHHELD_TIERS` — this never reaches the public repo
#:   (Spec K §3.3, Spec M §5).
UNACCOUNTABLE_TIERS = frozenset({NEWS, VENDOR_DATA, WEB_SCRAPE})

#: Third-party text attributable to a named filer or issuer, on the record.
ACCOUNTABLE_PRIMARY_TIERS = frozenset({PRIMARY_REGULATOR, COMPANY_DISCLOSURE})

#: Never written to the public repo (Spec K §3.3, Spec M §5).
MIRROR_WITHHELD_TIERS = UNACCOUNTABLE_TIERS

WITHHELD_REASONS = {
    NEWS: "news-derived",
    VENDOR_DATA: "vendor-data",
    WEB_SCRAPE: "third-party page text",
}

TRUSTED = "trusted"
UNTRUSTED = "untrusted"


class UnknownTier(ValueError):
    """A source tier that is not one of :data:`TIERS`.

    Refused rather than defaulted. Defaulting an unrecognised tier to
    ``untrusted`` would be safe for the trust question and *unsafe* for the
    licensing one, and defaulting it the other way is worse.
    """


def validate_tier(tier: str) -> str:
    if tier not in TIERS:
        raise UnknownTier(f"unknown source tier {tier!r}; valid tiers are {list(TIERS)}")
    return tier


def normalise_sources(sources) -> list[dict]:
    """Canonicalise a source list, refusing anything unusable.

    A source is ``{"url", "tier", "title", "as_of"}``. ``url`` and ``tier`` are
    required: a claim whose origin cannot be reopened is not a sourced claim,
    and a tier is what both decisions above are made from.
    """
    out: list[dict] = []
    for raw in sources or []:
        if not isinstance(raw, dict):
            raise UnknownTier(f"a source must be an object, got {type(raw).__name__}")
        tier = validate_tier(str(raw.get("tier") or ""))
        url = str(raw.get("url") or "").strip()
        if not url:
            raise UnknownTier(f"source with tier {tier!r} has no url")
        out.append(
            {
                "url": url,
                "tier": tier,
                "title": str(raw.get("title") or ""),
                "as_of": str(raw.get("as_of") or ""),
            }
        )
    return out


def tiers_of(sources) -> set[str]:
    return {str(s.get("tier")) for s in (sources or [])}


def content_trust(sources) -> str:
    """``untrusted`` if *any* source is content someone else wrote.

    Any, not all: a section citing one filing and one internal computation may
    quote the filing, and the marking exists so a reader knows the prose may
    carry someone else's words.
    """
    return UNTRUSTED if tiers_of(sources) & UNTRUSTED_TIERS else TRUSTED


def refuses_all_untrusted(sources) -> bool:
    """Spec P §5: every source is unaccountable third-party text.

    Three readings of "all untrusted-tier" are possible and they are not close
    to equivalent, so the choice is stated rather than left in the code:

    * *Every non-self-authored source*, filings included. This is the literal
      reading of Spec P §5's prose, and it makes the ``company-researcher``
      subagent of Spec P §4 — whose entire brief is "build a dossier section
      from primary sources" — unable to write anything without a human flag.
      Rejected: it refuses the system's best sources.
    * *Every source is unaccountable third-party text* — news, vendor data, a
      scraped page. A section built only from those has no named filer or
      issuer standing behind any of it, which is exactly the laundering the
      rule exists to stop. **Adopted.**
    * Any single untrusted source. Rejected: it makes the flag routine, and a
      flag that is always set is not a flag.

    "All", not "any": a section that also rests on a filing, a number we
    computed, or a human's own knowledge is a synthesis. An **empty** source
    list is not "all untrusted" either — it is ``unsourced``, which Spec M §3
    permits and flags everywhere rather than refusing.

    Flagged for ratification in the Phase 2 PR.
    """
    tiers = tiers_of(sources)
    return bool(tiers) and tiers <= UNACCOUNTABLE_TIERS


def mirror_withheld_tiers(sources) -> list[str]:
    """The tiers in ``sources`` that may not be committed, sorted."""
    return sorted(tiers_of(sources) & MIRROR_WITHHELD_TIERS)


def is_mirror_withheld(sources) -> bool:
    """Withhold if *any* source is a withheld tier.

    Any, because a section citing both a 10-K and a news article may have taken
    its wording from either, and the file is public the moment it is committed.
    Git history does not forget (Spec K §3.3).
    """
    return bool(mirror_withheld_tiers(sources))


def withheld_reason(sources) -> str:
    """The reason string the mirror marker carries."""
    tiers = mirror_withheld_tiers(sources)
    return ", ".join(WITHHELD_REASONS.get(t, t) for t in tiers)
