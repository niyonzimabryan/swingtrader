"""The daily digest and the weekly report, as one card kind.

Both are already built elsewhere — ``bot/daily_digest.py`` and
``bot/weekly_report.py`` assemble them from the ledger and the broker and render
MarkdownV2 for Telegram. This card does not rebuild them and must not: it takes
the sections they already produced and lays them out.

``from_sections`` is the structured entry point, for a caller that has the
numbers. ``from_markdown`` is the pragmatic one, for a caller that has only the
Telegram string: it un-escapes MarkdownV2 and splits on the digest's own header
convention, so the email is legible without either report being rewritten first.
That is a deliberately modest conversion — it does not parse emphasis, and it
prints what it could not classify as plain text rather than dropping it.
"""

from __future__ import annotations

import re

from notify.channel import KIND_DIGEST

#: MarkdownV2 escapes every one of these with a backslash. Undoing that is the
#: whole of the conversion; nothing here interprets `*` or `_` as markup,
#: because a digest line like `AAPL: -2.1%` must survive unchanged.
_UNESCAPE = re.compile(r"\\([_*\[\]()~`>#+=|{}.!\-])")

#: A section header in either report: `*📊 SCOREBOARD*` or `*Today*`.
_HEADER = re.compile(r"^\s*\*(?P<title>[^*]+)\*\s*$")


def unescape_markdown_v2(text: str) -> str:
    """Drop MarkdownV2 escape backslashes. Leaves everything else alone."""
    return _UNESCAPE.sub(r"\1", str(text or ""))


def sections_from_markdown(text: str) -> list[tuple[str, list[str]]]:
    """``[(title, lines), ...]``. Lines before the first header get ``""``."""
    sections: list[tuple[str, list[str]]] = []
    title = ""
    buffer: list[str] = []
    for raw in unescape_markdown_v2(text).split("\n"):
        match = _HEADER.match(raw)
        if match:
            if buffer or title:
                sections.append((title, buffer))
            title = match.group("title").strip()
            buffer = []
            continue
        buffer.append(raw.rstrip())
    if buffer or title:
        sections.append((title, buffer))
    return [
        (title, [line for line in lines if line.strip()])
        for title, lines in sections
        if title or any(line.strip() for line in lines)
    ]


def _blocks(sections) -> list[dict]:
    blocks: list[dict] = []
    for title, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        blocks.append({"type": "text", "title": title, "body": body, "mono": True})
    return blocks


def build_payload(
    *,
    title: str,
    sections,
    subject: str = "",
    headline: str = "",
    eyebrow: str = "digest",
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    as_of_utc: str = "",
    stale: bool = False,
    footer=None,
) -> dict:
    """``sections`` is an iterable of ``(title, lines)``."""
    blocks: list[dict] = []
    if as_of_utc:
        blocks.append(
            {
                "type": "rows",
                "rows": [{"label": "as of", "value": as_of_utc, "stale": bool(stale)}],
            }
        )
    blocks.extend(_blocks(sections))
    return {
        "version": 1,
        "kind": KIND_DIGEST,
        "uid": uid,
        "ref": ref or f"digest:{title}",
        "created_at_utc": created_at_utc,
        "subject": subject or title,
        "eyebrow": eyebrow,
        "title": title,
        "headline": headline,
        "verdict": {},
        "link_label": "Open the full digest",
        "blocks": blocks,
        "chart": None,
        "footer": list(footer or [
            "SwingTrader — summary only. Nothing here places, cancels, or modifies an order.",
            "This message is not investment advice and this system is not a licensed advisor.",
        ]),
    }


def from_markdown(
    *,
    title: str,
    markdown: str,
    subject: str = "",
    headline: str = "",
    eyebrow: str = "digest",
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    as_of_utc: str = "",
    stale: bool = False,
) -> dict:
    """Build the card from a report's existing MarkdownV2 string."""
    return build_payload(
        title=title,
        sections=sections_from_markdown(markdown),
        subject=subject,
        headline=headline,
        eyebrow=eyebrow,
        uid=uid,
        ref=ref,
        created_at_utc=created_at_utc,
        as_of_utc=as_of_utc,
        stale=stale,
    )
