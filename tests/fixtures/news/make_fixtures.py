"""Rebuild the synthetic news fixtures in this directory.

Run from the repository root::

    python tests/fixtures/news/make_fixtures.py

See README.md here for why these are synthetic rather than recorded, and for
``scripts/record_news_fixtures.py``, which replaces them with real Alpaca
responses on a machine that can reach ``data.alpaca.markets``.

Each file is an Alpaca-shaped ``{"news": [...], "next_page_token": null}``
payload: ``id``, ``headline``, ``summary``, ``content``, ``source``, ``url``,
``symbols``, ``created_at``, ``updated_at``. The company is fictional
(``FCTX``, Fictional Example Corp) and so is every number in it.

Four cases are covered, one per file:

``wire_pickup``
    One wire story republished by twenty outlets, with slightly rewritten
    headlines and tracking parameters on the URLs. Must cluster to one story.
``earnings_preview_and_reaction``
    A 07:00 preview with no consensus figure and a 16:45 reaction piece that
    states one. The extracted consensus must carry **16:45**, not 07:00.
``consensus_disagreement``
    Two established publishers citing different consensus figures for the same
    print. Both must be recorded, with the spread.
``tiering``
    One article per source tier, including an undated one, for the eligibility
    matrix.
"""
import json
import os

OUT = os.path.dirname(os.path.abspath(__file__))

SYMBOL = "FCTX"
COMPANY = "Fictional Example Corp"


def article(uid, headline, publisher, created_at, *, summary="", content="",
            url=None, symbols=(SYMBOL,), updated_at=None):
    return {
        "id": uid,
        "headline": headline,
        "summary": summary,
        "content": content or summary,
        "author": "",
        "source": publisher,
        "url": url or f"https://example.invalid/{uid}",
        "symbols": list(symbols),
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "images": [],
    }


def write(name, articles):
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"news": articles, "next_page_token": None}, handle, indent=1)
        handle.write("\n")
    print(f"  wrote {name}.json ({len(articles)} articles)")


# --- one wire story, twenty outlets -----------------------------------------

WIRE_LEAD = (
    f"{COMPANY} said on Tuesday it will acquire privately held Placeholder "
    "Systems for $1.2 billion in cash, expanding its industrial software unit. "
    "The deal is expected to close in the fourth quarter."
)

OUTLETS = [
    ("Business Wire", "2026-05-12T11:00:00Z", f"{COMPANY} to Acquire Placeholder Systems for $1.2 Billion"),
    ("Reuters", "2026-05-12T11:06:00Z", f"{COMPANY} to buy Placeholder Systems for $1.2 billion"),
    ("Bloomberg", "2026-05-12T11:07:00Z", f"{COMPANY} agrees to acquire Placeholder Systems in $1.2 billion deal"),
    ("Dow Jones", "2026-05-12T11:09:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("CNBC", "2026-05-12T11:12:00Z", f"{COMPANY} buys Placeholder Systems for $1.2 billion"),
    ("MarketWatch", "2026-05-12T11:15:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion cash"),
    ("Benzinga", "2026-05-12T11:18:00Z", f"{COMPANY} announces $1.2 billion acquisition of Placeholder Systems"),
    ("Barron's", "2026-05-12T11:21:00Z", f"{COMPANY} will acquire Placeholder Systems for $1.2 billion"),
    ("Financial Times", "2026-05-12T11:24:00Z", f"{COMPANY} to acquire Placeholder Systems in $1.2bn deal"),
    ("Yahoo Finance", "2026-05-12T11:31:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("Seeking Alpha", "2026-05-12T11:34:00Z", f"{COMPANY} to buy Placeholder Systems for $1.2 billion"),
    ("Zacks", "2026-05-12T11:38:00Z", f"{COMPANY} acquires Placeholder Systems for $1.2 billion"),
    ("InvestorPlace", "2026-05-12T11:41:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("The Motley Fool", "2026-05-12T11:44:00Z", f"{COMPANY} is buying Placeholder Systems for $1.2 billion"),
    ("MSN", "2026-05-12T11:47:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("TipRanks", "2026-05-12T11:52:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("StreetInsider", "2026-05-12T11:55:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("GuruFocus", "2026-05-12T11:58:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("Investing.com", "2026-05-12T12:02:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
    ("Insider Monkey", "2026-05-12T12:07:00Z", f"{COMPANY} to acquire Placeholder Systems for $1.2 billion"),
]

wire = [
    article(
        f"wire-{index:02d}",
        headline,
        publisher,
        created,
        summary=WIRE_LEAD,
        url=(
            f"https://example.invalid/fctx-placeholder-deal"
            f"?utm_source={publisher.lower().replace(' ', '-')}&utm_medium=syndication"
        ),
    )
    for index, (publisher, created, headline) in enumerate(OUTLETS)
]
write("wire_pickup", wire)

# --- the preview / flash / reaction sequence --------------------------------
#
# The 16:30 wire release states the actual EPS and no consensus — companies do
# not publish the number they are being measured against. The 16:45 reaction
# piece states it. Those two are one story and cluster together; the 07:00
# preview is a different story and stays separate.
#
# So the story's known_at is 16:30 and the consensus figure's known_at must be
# 16:45. Stamping the figure with the cluster's earliest timestamp would make
# it available fifteen minutes before it existed — and in the preview case the
# spec names, nine hours before.

RESULTS_LEAD = (
    f"{COMPANY} reported adjusted earnings per share of $1.34 for the third "
    "quarter, with the industrial software unit expanding operating margin for "
    "a fourth consecutive period."
)

preview_reaction = [
    article(
        "preview-01",
        f"What to watch when {COMPANY} reports third-quarter results",
        "Reuters",
        "2026-07-28T07:00:00Z",
        summary=(
            f"{COMPANY} reports third-quarter results after the close on "
            "Tuesday. Investors will focus on commentary about the pending "
            "Placeholder Systems integration and on segment disclosure changes "
            "flagged at the last investor day."
        ),
    ),
    article(
        "flash-01",
        f"{COMPANY} Reports Third-Quarter Results",
        "Business Wire",
        "2026-07-28T16:30:00Z",
        summary=RESULTS_LEAD,
    ),
    article(
        "reaction-01",
        f"{COMPANY} Reports Third-Quarter Results, Tops Estimates",
        "Bloomberg",
        "2026-07-28T16:45:00Z",
        summary=(
            RESULTS_LEAD + " The company beat the $1.23 consensus estimate."
        ),
    ),
]
write("earnings_preview_and_reaction", preview_reaction)

# --- two established publishers, two different consensus figures ------------
#
# One preview story, covered twice. Zacks, FactSet and Refinitiv consensus
# differ and authors cite whichever their desk uses, so the two pieces carry
# different numbers for the same print. Averaging them would invent a figure
# nobody published; the reading records both, with the spread.

PREVIEW_LEAD = (
    f"{COMPANY} reports third-quarter results after the close on Tuesday, the "
    "first period to include a full quarter of Placeholder Systems."
)

disagreement = [
    article(
        "consensus-a",
        f"{COMPANY} third-quarter preview: what the Street expects",
        "Reuters",
        "2026-07-27T12:00:00Z",
        summary=PREVIEW_LEAD + " Analysts expect EPS of $1.23 for the quarter.",
    ),
    article(
        "consensus-b",
        f"{COMPANY} third-quarter preview: what the Street expects",
        "CNBC",
        "2026-07-27T13:30:00Z",
        summary=PREVIEW_LEAD + " The consensus estimate of $1.18 excludes two "
                               "smaller contributors from the panel.",
    ),
]
write("consensus_disagreement", disagreement)

# --- one article per tier, including an undated one -------------------------

tiering = [
    article(
        "tier-primary",
        f"{COMPANY} Announces Leadership Transition",
        "Business Wire",
        "2026-06-02T12:30:00Z",
        summary=f"{COMPANY} today announced that its chief financial officer will retire.",
    ),
    article(
        "tier-established",
        f"{COMPANY} finance chief to retire",
        "Reuters",
        "2026-06-02T12:41:00Z",
        summary=(
            f"{COMPANY}'s chief financial officer will retire at the end of the "
            "year, the company said. Analysts expect EPS of $1.41 for the fourth "
            "quarter."
        ),
    ),
    article(
        "tier-aggregator",
        f"{COMPANY} CFO exit: what it means for the stock",
        "Seeking Alpha",
        "2026-06-02T14:02:00Z",
        summary=(
            f"With the CFO leaving, the consensus estimate of $1.41 for the "
            "fourth quarter looks harder to hit."
        ),
    ),
    article(
        "tier-undated",
        f"{COMPANY} shakeup continues",
        "Unknown Blog",
        None,
        summary=(
            f"Sources say more departures are coming at {COMPANY}. Analysts "
            "expect EPS of $1.41 for the fourth quarter."
        ),
    ),
]
# Alpaca always sends created_at; the undated case is what a source that omits
# it looks like once the adapter has run, so the fixture stores it as null and
# the loader turns it into an article with published_at=None.
tiering[-1]["created_at"] = None
tiering[-1]["updated_at"] = None
write("tiering", tiering)

if __name__ == "__main__":
    print(f"wrote news fixtures to {OUT}")
