"""One visual system, two renderings: the email and the page.

**The document model.** Every card kind — a proposal, a scan memo, a scorecard,
a page, a digest — is built into the same JSON shape, a list of typed blocks,
and both renderings walk that list. One renderer means one visual system and one
set of golden tests, and it means the page cannot quietly print a different
number from the email: they read the same payload, which is the one stored in
``cards.payload_json``.

A block is a dict with a ``type``:

``rows``     label/value pairs — the workhorse. Each row may carry a ``note``,
             a ``tone``, and ``stale: true``.
``table``    ``columns`` and ``rows``; scrolls horizontally on a narrow screen.
``text``     a paragraph, optionally ``mono``.
``list``     bullets.
``quote``    a block quote with a ``source``. ``trust: "untrusted"`` renders it
             visibly differently, because filing and news text is
             attacker-writable and must stay distinguishable wherever a human
             reads it (AGENTS.md §5).
``chart``    the PNG, by URL. Never base64: Gmail proxies remote images and a
             data URI is stripped.

Any block may set ``page_only: true``. The email is the summary and the link;
the page is the full record.

**Numbers.** Nothing here computes one. ``value`` fields arrive pre-formatted or
as scalars that are printed with ``str``/``format`` and no arithmetic — the
renderer has no ``+``, ``*``, ``round`` or ``%`` on a reported figure anywhere
in it, which is AGENTS.md §1.2 held by construction rather than by care.
``stale`` travels on the row itself so the flag is printed *next to* the number
in both renderings, not once in a footer (§1.3).

**Email constraints.** Table-based layout, inline CSS on every element that
needs it, no JavaScript, no inline SVG (Gmail strips it), and a light palette as
the baseline so a client that honours nothing still renders correctly. The
``@media (prefers-color-scheme: dark)`` block in ``<style>`` is the enhancement
for the clients that do honour it (Apple Mail, iOS Mail, recent Outlook);
Gmail's web client ignores it and gets the light card, which is the safe
default rather than a broken one.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

#: The one palette. Light is the baseline; dark is the `prefers-color-scheme`
#: override, so every colour here is defined once and named by role.
LIGHT = {
    "page": "#f4f5f7",
    "ground": "#ffffff",
    "ink": "#11151c",
    "muted": "#5c6673",
    "faint": "#8b95a3",
    "hairline": "#e2e6ec",
    "accent": "#1f4fd8",
    "ok": "#0f7a4d",
    "warn": "#9a6200",
    "bad": "#b3261e",
    "ok_bg": "#e8f5ee",
    "warn_bg": "#fdf3e2",
    "bad_bg": "#fdeceb",
    "neutral_bg": "#eef1f5",
    "quote_bg": "#f7f8fa",
    "untrusted_bg": "#fff8e6",
    "untrusted_edge": "#d9a300",
}

DARK = {
    "page": "#0d1015",
    "ground": "#161a21",
    "ink": "#e7ecf3",
    "muted": "#9aa5b3",
    "faint": "#78828f",
    "hairline": "#28303c",
    "accent": "#7ea2ff",
    "ok": "#5ecf9a",
    "warn": "#e8b35c",
    "bad": "#ff8a80",
    "ok_bg": "#12261d",
    "warn_bg": "#2a2213",
    "bad_bg": "#2b1717",
    "neutral_bg": "#1d232c",
    "quote_bg": "#1a1f27",
    "untrusted_bg": "#2a2415",
    "untrusted_edge": "#a37d00",
}

FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', "
    "Arial, sans-serif"
)
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

#: Tone -> (text colour key, background key). `neutral` is the default.
TONES = {
    "ok": ("ok", "ok_bg"),
    "warn": ("warn", "warn_bg"),
    "bad": ("bad", "bad_bg"),
    "neutral": ("muted", "neutral_bg"),
}

#: The marker printed beside a number whose source said it was stale. The same
#: string in the email, on the page, and in the plain-text part, so grepping a
#: mailbox for it finds every one of them.
STALE_MARK = "STALE"


@dataclass(frozen=True)
class RenderedCard:
    """What a card renders to. ``html_page`` is the full record; the rest is the email."""

    subject: str
    html_email: str
    html_page: str
    text: str


# --------------------------------------------------------------------------- #
# Escaping and formatting — no arithmetic anywhere below this line
# --------------------------------------------------------------------------- #


def esc(value) -> str:
    """HTML-escape, including quotes. Every interpolation in this module uses it."""
    return html.escape("" if value is None else str(value), quote=True)


def fmt(value, *, dash: str = "—") -> str:
    """Print a value. Never rounds, never scales, never selects.

    A float is printed with ``repr``-equivalent fidelity via ``str`` unless the
    payload already carries a formatted string, which is the normal case: the
    builders format, because they are the ones that know whether a figure is
    dollars, a fraction or a count. This is the last-resort printer.
    """
    if value is None or value == "":
        return dash
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# --------------------------------------------------------------------------- #
# The stylesheet
# --------------------------------------------------------------------------- #


def _vars(palette: dict) -> str:
    return "\n".join(f"      --{key}: {value};" for key, value in sorted(palette.items()))


def stylesheet(*, page: bool) -> str:
    """The ``<style>`` block. Dark-mode overrides only; light is inline too.

    On the **page** this is the whole stylesheet and CSS variables carry it. In
    the **email** the light palette is additionally inlined on every element,
    because a client that drops ``<style>`` (Gmail's clipped view, some webmail)
    must still get a correctly coloured card — the classes here only ever
    *override* toward dark.
    """
    root = ":root" if page else ":root"
    blocks = [
        f"{root} {{\n{_vars(LIGHT)}\n    }}",
        "    @media (prefers-color-scheme: dark) {\n"
        f"      :root {{\n{_vars(DARK)}\n      }}\n"
        "      body, .st-page, .st-wrap { background: var(--page) !important; }\n"
        "      .st-card { background: var(--ground) !important; }\n"
        "      .st-ink, .st-ink a { color: var(--ink) !important; }\n"
        "      .st-muted { color: var(--muted) !important; }\n"
        "      .st-faint { color: var(--faint) !important; }\n"
        "      .st-rule { border-color: var(--hairline) !important; }\n"
        "      .st-hair { background: var(--hairline) !important; }\n"
        "      .st-accent, .st-accent a { color: var(--accent) !important; }\n"
        "      .st-quote { background: var(--quote_bg) !important; }\n"
        "      .st-untrusted { background: var(--untrusted_bg) !important;"
        " border-color: var(--untrusted_edge) !important; }\n"
        "      .st-tone-ok { color: var(--ok) !important; background: var(--ok_bg) !important; }\n"
        "      .st-tone-warn { color: var(--warn) !important; background: var(--warn_bg) !important; }\n"
        "      .st-tone-bad { color: var(--bad) !important; background: var(--bad_bg) !important; }\n"
        "      .st-tone-neutral { color: var(--muted) !important;"
        " background: var(--neutral_bg) !important; }\n"
        "      .st-btn { background: var(--accent) !important; color: #0d1015 !important; }\n"
        "    }",
        "    .st-wrap { width: 100%; }\n"
        "    .st-card { max-width: 640px; margin: 0 auto; }\n"
        "    img { max-width: 100%; border: 0; }\n"
        "    .st-scroll { overflow-x: auto; }\n"
        "    @media (max-width: 480px) {\n"
        "      .st-pad { padding-left: 18px !important; padding-right: 18px !important; }\n"
        "      .st-ticker { font-size: 26px !important; }\n"
        "    }",
    ]
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# Block renderers
# --------------------------------------------------------------------------- #


def _tone_style(tone: str) -> str:
    ink_key, bg_key = TONES.get(str(tone or "neutral"), TONES["neutral"])
    return (
        f"color:{LIGHT[ink_key]};background:{LIGHT[bg_key]};"
        "display:inline-block;padding:3px 9px;border-radius:999px;"
        "font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;"
    )


def pill(label: str, tone: str = "neutral") -> str:
    cls = f"st-tone-{str(tone or 'neutral') if str(tone or 'neutral') in TONES else 'neutral'}"
    return f'<span class="{cls}" style="{_tone_style(tone)}">{esc(label)}</span>'


def _stale_mark() -> str:
    return (
        f'<span class="st-tone-warn" style="{_tone_style("warn")}margin-left:6px;">'
        f"{STALE_MARK}</span>"
    )


def _section_title(title: str) -> str:
    if not title:
        return ""
    return (
        f'<tr><td class="st-pad st-faint" style="padding:22px 28px 6px 28px;'
        f'font:600 11px/1.4 {FONT};letter-spacing:.12em;text-transform:uppercase;'
        f'color:{LIGHT["faint"]};">{esc(title)}</td></tr>'
    )


def _rows_block(block: dict) -> str:
    cells = []
    for row in block.get("rows") or []:
        label = esc(row.get("label", ""))
        value = esc(fmt(row.get("value")))
        note = row.get("note") or ""
        tone = row.get("tone") or ""
        value_html = (
            pill(fmt(row.get("value")), tone)
            if tone
            else f'<span class="st-ink" style="color:{LIGHT["ink"]};">{value}</span>'
        )
        if row.get("stale"):
            value_html += _stale_mark()
        note_html = (
            f'<div class="st-muted" style="font:400 12px/1.5 {FONT};color:{LIGHT["muted"]};'
            f'margin-top:3px;">{esc(note)}</div>'
            if note
            else ""
        )
        cells.append(
            f'<tr><td class="st-muted" style="padding:7px 14px 7px 0;font:400 13px/1.5 {FONT};'
            f'color:{LIGHT["muted"]};vertical-align:top;white-space:nowrap;">{label}</td>'
            f'<td style="padding:7px 0;font:600 14px/1.5 {MONO};text-align:right;'
            f'vertical-align:top;">{value_html}{note_html}</td></tr>'
        )
    if not cells:
        return ""
    return (
        f'<tr><td class="st-pad" style="padding:2px 28px 4px 28px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(cells)
        + "</table></td></tr>"
    )


def _table_block(block: dict) -> str:
    columns = list(block.get("columns") or [])
    body = list(block.get("rows") or [])
    if not columns and not body:
        return ""
    head = "".join(
        f'<th class="st-faint st-rule" style="padding:6px 10px;font:600 11px/1.4 {FONT};'
        f'text-align:left;color:{LIGHT["faint"]};letter-spacing:.06em;text-transform:uppercase;'
        f'border-bottom:1px solid {LIGHT["hairline"]};white-space:nowrap;">{esc(column)}</th>'
        for column in columns
    )
    lines = []
    for entry in body:
        cells = "".join(
            f'<td class="st-ink st-rule" style="padding:7px 10px;font:400 13px/1.5 {MONO};'
            f'color:{LIGHT["ink"]};border-bottom:1px solid {LIGHT["hairline"]};'
            f'white-space:nowrap;">{esc(fmt(cell))}</td>'
            for cell in (entry or [])
        )
        lines.append(f"<tr>{cells}</tr>")
    note = block.get("note") or ""
    note_html = (
        f'<div class="st-muted" style="font:400 12px/1.5 {FONT};color:{LIGHT["muted"]};'
        f'margin-top:8px;">{esc(note)}</div>'
        if note
        else ""
    )
    return (
        f'<tr><td class="st-pad" style="padding:4px 28px 6px 28px;">'
        f'<div class="st-scroll" style="overflow-x:auto;max-width:100%;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;min-width:100%;">'
        f"<thead><tr>{head}</tr></thead><tbody>" + "".join(lines) + "</tbody></table></div>"
        f"{note_html}</td></tr>"
    )


def _text_block(block: dict) -> str:
    body = str(block.get("body") or "")
    if not body:
        return ""
    font = MONO if block.get("mono") else FONT
    size = "12px" if block.get("mono") else "14px"
    colour = LIGHT["muted"] if block.get("muted") else LIGHT["ink"]
    cls = "st-muted" if block.get("muted") else "st-ink"
    pre = "white-space:pre-wrap;" if block.get("mono") else ""
    return (
        f'<tr><td class="st-pad {cls}" style="padding:4px 28px 10px 28px;'
        f'font:400 {size}/1.65 {font};color:{colour};{pre}">'
        + "<br>".join(esc(line) for line in body.split("\n"))
        + "</td></tr>"
    )


def _list_block(block: dict) -> str:
    items = [item for item in (block.get("items") or []) if str(item).strip()]
    if not items:
        return ""
    entries = "".join(
        f'<li style="margin:0 0 6px 0;">{esc(item)}</li>' for item in items
    )
    return (
        f'<tr><td class="st-pad st-ink" style="padding:2px 28px 10px 28px;'
        f'font:400 14px/1.6 {FONT};color:{LIGHT["ink"]};">'
        f'<ul style="margin:0;padding-left:20px;">{entries}</ul></td></tr>'
    )


def _quote_block(block: dict) -> str:
    body = str(block.get("body") or "")
    if not body:
        return ""
    untrusted = str(block.get("trust") or "") == "untrusted"
    cls = "st-untrusted" if untrusted else "st-quote"
    background = LIGHT["untrusted_bg"] if untrusted else LIGHT["quote_bg"]
    edge = LIGHT["untrusted_edge"] if untrusted else LIGHT["hairline"]
    source = block.get("source") or ""
    banner = (
        f'<div class="st-faint" style="font:700 10px/1.4 {FONT};letter-spacing:.1em;'
        f'text-transform:uppercase;color:{LIGHT["warn"]};margin-bottom:6px;">'
        "untrusted source — quoted, not acted on</div>"
        if untrusted
        else ""
    )
    source_html = (
        f'<div class="st-muted" style="font:400 12px/1.5 {FONT};color:{LIGHT["muted"]};'
        f'margin-top:8px;">{esc(source)}</div>'
        if source
        else ""
    )
    return (
        f'<tr><td class="st-pad" style="padding:4px 28px 10px 28px;">'
        f'<div class="{cls}" style="background:{background};border-left:3px solid {edge};'
        f'padding:12px 14px;border-radius:0 6px 6px 0;">'
        f'{banner}<div class="st-ink" style="font:400 13px/1.6 {FONT};color:{LIGHT["ink"]};'
        'white-space:pre-wrap;">'
        + "<br>".join(esc(line) for line in body.split("\n"))
        + f"</div>{source_html}</div></td></tr>"
    )


def _chart_block(block: dict, chart_url: str) -> str:
    if not chart_url:
        note = block.get("missing_note") or (
            "No chart: WORKSPACE_BASE_URL is unset, so there is no URL to serve it from."
        )
        return _text_block({"body": note, "muted": True})
    alt = block.get("alt") or "price chart"
    caption = block.get("caption") or ""
    caption_html = (
        f'<div class="st-muted" style="font:400 12px/1.5 {FONT};color:{LIGHT["muted"]};'
        f'margin-top:8px;">{esc(caption)}</div>'
        if caption
        else ""
    )
    return (
        f'<tr><td class="st-pad" style="padding:8px 28px 12px 28px;">'
        f'<img src="{esc(chart_url)}" alt="{esc(alt)}" width="584" '
        f'style="display:block;width:100%;max-width:584px;height:auto;border-radius:8px;">'
        f"{caption_html}</td></tr>"
    )


def _divider() -> str:
    return (
        f'<tr><td class="st-pad" style="padding:8px 28px;">'
        f'<div class="st-hair" style="height:1px;background:{LIGHT["hairline"]};'
        'line-height:1px;font-size:0;">&nbsp;</div></td></tr>'
    )


def render_block(block: dict, *, chart_url: str = "") -> str:
    kind = str(block.get("type") or "")
    title = _section_title(block.get("title", ""))
    if kind == "rows":
        body = _rows_block(block)
    elif kind == "table":
        body = _table_block(block)
    elif kind == "text":
        body = _text_block(block)
    elif kind == "list":
        body = _list_block(block)
    elif kind == "quote":
        body = _quote_block(block)
    elif kind == "chart":
        body = _chart_block(block, chart_url)
    elif kind == "divider":
        return _divider()
    else:
        body = ""
    return f"{title}{body}" if body else ""


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #


def _header(payload: dict) -> str:
    title = payload.get("title") or ""
    headline = payload.get("headline") or ""
    verdict = payload.get("verdict") or {}
    eyebrow = payload.get("eyebrow") or ""
    verdict_html = (
        pill(verdict.get("label", ""), verdict.get("tone", "neutral"))
        if verdict.get("label")
        else ""
    )
    eyebrow_html = (
        f'<div class="st-faint" style="font:600 11px/1.4 {FONT};letter-spacing:.14em;'
        f'text-transform:uppercase;color:{LIGHT["faint"]};margin-bottom:10px;">'
        f"{esc(eyebrow)}</div>"
        if eyebrow
        else ""
    )
    headline_html = (
        f'<div class="st-muted" style="font:400 14px/1.5 {FONT};color:{LIGHT["muted"]};'
        f'margin-top:6px;">{esc(headline)}</div>'
        if headline
        else ""
    )
    return (
        f'<tr><td class="st-pad" style="padding:28px 28px 6px 28px;">{eyebrow_html}'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        f'<tr><td class="st-ticker st-ink" style="font:700 30px/1.15 {FONT};'
        f'letter-spacing:-.01em;color:{LIGHT["ink"]};vertical-align:middle;">{esc(title)}</td>'
        f'<td style="text-align:right;vertical-align:middle;">{verdict_html}</td></tr>'
        f"</table>{headline_html}</td></tr>"
    )


def _link_button(url: str, label: str) -> str:
    if not url:
        return ""
    return (
        f'<tr><td class="st-pad" style="padding:14px 28px 4px 28px;">'
        f'<a class="st-btn" href="{esc(url)}" '
        f'style="display:inline-block;background:{LIGHT["accent"]};color:#ffffff;'
        f'font:600 14px/1 {FONT};padding:12px 20px;border-radius:8px;'
        f'text-decoration:none;">{esc(label)}</a></td></tr>'
    )


def _footer(payload: dict, *, card_url: str) -> str:
    lines = list(payload.get("footer") or [])
    if card_url:
        lines.append(f"Full card: {card_url}")
    if not lines:
        return ""
    body = "<br>".join(esc(line) for line in lines)
    return (
        f'<tr><td class="st-pad st-faint" style="padding:18px 28px 28px 28px;'
        f'font:400 12px/1.6 {FONT};color:{LIGHT["faint"]};border-top:1px solid '
        f'{LIGHT["hairline"]};">{body}</td></tr>'
    )


def _shell(inner: str, *, page: bool, title: str) -> str:
    if page:
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex, nofollow">\n'
            f"<title>{esc(title)}</title>\n<style>\n    {stylesheet(page=True)}\n"
            f"    body {{ margin:0; background: {LIGHT['page']}; }}\n"
            "</style>\n</head>\n"
            f'<body class="st-page" style="margin:0;background:{LIGHT["page"]};">\n'
            f'<table role="presentation" class="st-wrap" cellpadding="0" cellspacing="0" '
            f'border="0" width="100%" style="background:{LIGHT["page"]};">'
            f'<tr><td style="padding:28px 12px;">{inner}</td></tr></table>\n'
            "</body>\n</html>\n"
        )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="supported-color-schemes" content="light dark">\n'
        f"<title>{esc(title)}</title>\n<style>\n    {stylesheet(page=False)}\n</style>\n"
        "</head>\n"
        f'<body class="st-page" style="margin:0;padding:0;background:{LIGHT["page"]};">\n'
        f'<table role="presentation" class="st-wrap" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" style="background:{LIGHT["page"]};">'
        f'<tr><td style="padding:20px 10px;">{inner}</td></tr></table>\n'
        "</body>\n</html>\n"
    )


def _card_table(rows_html: str) -> str:
    return (
        f'<table role="presentation" class="st-card" cellpadding="0" cellspacing="0" '
        # `table-layout:fixed` is load-bearing, not cosmetic: without it a wide
        # table (the scorecard's arm rows) auto-sizes its containing cell and
        # drags the whole card past 640px, so the *page* scrolls sideways
        # instead of the one table that is too wide. Fixed layout pins the
        # column, and the `.st-scroll` wrapper inside it takes the overflow.
        f'border="0" width="100%" style="table-layout:fixed;max-width:640px;'
        f'margin:0 auto;background:{LIGHT["ground"]};border-radius:14px;'
        f'border:1px solid {LIGHT["hairline"]};">' + rows_html + "</table>"
    )


def render_html(
    payload: dict,
    *,
    page: bool,
    chart_url: str = "",
    card_url: str = "",
) -> str:
    """Render the document. ``page_only`` blocks are dropped from the email."""
    blocks = payload.get("blocks") or []
    parts = [_header(payload)]
    for block in blocks:
        if not page and block.get("page_only"):
            continue
        parts.append(render_block(block, chart_url=chart_url))
    if not page:
        parts.append(_link_button(card_url, payload.get("link_label") or "Open the full card"))
    parts.append(_footer(payload, card_url="" if page else card_url))
    return _shell(_card_table("".join(parts)), page=page, title=payload.get("subject") or payload.get("title") or "card")


# --------------------------------------------------------------------------- #
# The plain-text rendering
# --------------------------------------------------------------------------- #


def _text_rows(block: dict) -> list[str]:
    lines = []
    for row in block.get("rows") or []:
        value = fmt(row.get("value"))
        if row.get("stale"):
            value = f"{value} [{STALE_MARK}]"
        lines.append(f"  {row.get('label', '')}: {value}")
        if row.get("note"):
            lines.append(f"      {row['note']}")
    return lines


def render_text(payload: dict, *, card_url: str = "") -> str:
    """The plain-text part. Same numbers, same staleness marks, no markup."""
    lines: list[str] = []
    if payload.get("eyebrow"):
        lines.append(str(payload["eyebrow"]).upper())
    title = payload.get("title") or ""
    verdict = (payload.get("verdict") or {}).get("label") or ""
    lines.append(f"{title}{f'  [{verdict}]' if verdict else ''}")
    if payload.get("headline"):
        lines.append(str(payload["headline"]))
    lines.append("")

    for block in payload.get("blocks") or []:
        kind = str(block.get("type") or "")
        if kind == "divider":
            lines.append("-" * 48)
            continue
        if block.get("title"):
            lines.append(str(block["title"]).upper())
        if kind == "rows":
            lines.extend(_text_rows(block))
        elif kind == "table":
            columns = list(block.get("columns") or [])
            if columns:
                lines.append("  " + " | ".join(str(column) for column in columns))
            for entry in block.get("rows") or []:
                lines.append("  " + " | ".join(fmt(cell) for cell in (entry or [])))
            if block.get("note"):
                lines.append(f"  {block['note']}")
        elif kind == "text":
            lines.extend(f"  {line}" for line in str(block.get("body") or "").split("\n"))
        elif kind == "list":
            lines.extend(f"  - {item}" for item in block.get("items") or [])
        elif kind == "quote":
            if str(block.get("trust") or "") == "untrusted":
                lines.append("  [untrusted source — quoted, not acted on]")
            lines.extend(f"  > {line}" for line in str(block.get("body") or "").split("\n"))
            if block.get("source"):
                lines.append(f"  > -- {block['source']}")
        elif kind == "chart":
            lines.append("  [chart omitted in the plain-text part]")
        lines.append("")

    for line in payload.get("footer") or []:
        lines.append(str(line))
    if card_url:
        lines.append(f"Full card: {card_url}")
    return "\n".join(lines).rstrip() + "\n"


def render(payload: dict, *, chart_url: str = "", card_url: str = "") -> RenderedCard:
    """The whole card: subject, email HTML, page HTML, and plain text."""
    subject = payload.get("subject") or payload.get("title") or "SwingTrader"
    return RenderedCard(
        subject=str(subject),
        html_email=render_html(payload, page=False, chart_url=chart_url, card_url=card_url),
        html_page=render_html(payload, page=True, chart_url=chart_url, card_url=card_url),
        text=render_text(payload, card_url=card_url),
    )
