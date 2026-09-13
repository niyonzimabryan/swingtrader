"""The page card: an unprotected position, a sync that failed closed, a discrepancy.

A page is the one message in this system that has to survive being read on a
phone, at a glance, in a hurry. So the card is deliberately thin: what happened,
which row it happened to, the recovery text **verbatim**, and the detail dict
underneath on the page for whoever opens it.

Verbatim matters. ``portfolio.paging`` and the execution service put the
recovery instruction in ``detail["recovery"]`` and it is the one string a human
acts on; summarising it in a renderer would be the same defect as rewriting a
number.

:func:`build_alert_payload` is the *routine* sibling, added by the headless
runtime. An order filled, a target hit, a regime change: the same block
renderer, the same visual system, a different kind
(:data:`~notify.channel.KIND_ALERT`) so a page and a fill stay distinguishable
in ``notifications_sent``. It is what ``bot.notifications.NotificationManager``
emits on when Telegram is off, and every number it prints is a pre-formatted
string the caller handed it — the renderer does no arithmetic here either.
"""

from __future__ import annotations

from notify.channel import KIND_ALERT, KIND_PAGE

#: Events that mean capital is exposed in a way nobody chose. They get the red
#: pill; everything else gets the amber one. A page is never neutral.
_CRITICAL = frozenset(
    {
        "position_unprotected",
        "protection_failed",
        "unknown_placement",
        "portfolio_mass_deletion_blocked",
        "reconciliation_required",
    }
)

#: Keys printed as the summary rows when present, in this order. Anything else
#: in `detail` lands in the full-detail table on the page.
_SUMMARY_KEYS = ("ticker", "proposal_id", "execution_id", "account", "as_of_utc")


def _tone(event: str) -> str:
    return "bad" if str(event or "") in _CRITICAL else "warn"


def build_payload(
    *,
    event: str,
    detail: dict | None = None,
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    stale: bool = False,
) -> dict:
    """The page document. ``event`` and ``detail`` are ``portfolio.paging``'s."""
    detail = dict(detail or {})
    recovery = str(detail.get("recovery") or "")
    summary = [
        {
            "label": key,
            "value": detail.get(key),
            "stale": bool(stale) if key == "as_of_utc" else False,
        }
        for key in _SUMMARY_KEYS
        if detail.get(key) is not None
    ]

    blocks: list[dict] = []
    if summary:
        blocks.append({"type": "rows", "rows": summary})
    if recovery:
        blocks.append({"type": "text", "title": "recovery", "body": recovery})

    rest = {
        key: value
        for key, value in sorted(detail.items())
        if key not in _SUMMARY_KEYS and key != "recovery"
    }
    if rest:
        blocks.append(
            {
                "type": "table",
                "title": "detail",
                "columns": ["field", "value"],
                "rows": [[key, value] for key, value in rest.items()],
                "page_only": True,
            }
        )

    subject_ticker = f" — {detail['ticker']}" if detail.get("ticker") else ""
    return {
        "version": 1,
        "kind": KIND_PAGE,
        "uid": uid,
        "ref": ref or str(event or ""),
        "created_at_utc": created_at_utc,
        "subject": f"[PAGE] {event}{subject_ticker}",
        "eyebrow": "page",
        "title": str(event or "page"),
        "headline": str(detail.get("ticker") or detail.get("account") or ""),
        "verdict": {"label": "action needed", "tone": _tone(event)},
        "link_label": "Open the page card",
        "blocks": blocks,
        "chart": None,
        "footer": [
            "SwingTrader paged. Nothing in this message can place, cancel, or modify "
            "an order — acting on it is a human step at the broker or in the bot.",
        ],
    }


def build_alert_payload(
    *,
    event: str,
    subject: str,
    title: str,
    headline: str = "",
    verdict: dict | None = None,
    rows=(),
    body: str = "",
    eyebrow: str = "alert",
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    footer=None,
) -> dict:
    """The document for a routine operational notice.

    ``rows`` are ``{"label", "value"}`` dicts (optionally ``note``, ``tone``,
    ``stale``) built by the caller from figures it already had; ``body`` is a
    closing paragraph. Both are printed as given — the same rule the page card
    follows, for the same reason.
    """
    blocks: list[dict] = []
    rows = [dict(row) for row in rows if row]
    if rows:
        blocks.append({"type": "rows", "rows": rows})
    if body:
        blocks.append({"type": "text", "body": body})

    return {
        "version": 1,
        "kind": KIND_ALERT,
        "uid": uid,
        "ref": ref or str(event or ""),
        "created_at_utc": created_at_utc,
        "subject": subject,
        "eyebrow": eyebrow,
        "title": title,
        "headline": headline,
        "verdict": dict(verdict or {}),
        "link_label": "Open the full card",
        "blocks": blocks,
        "chart": None,
        "footer": list(
            footer
            or [
                "SwingTrader — notification only. Nothing in this message can place, "
                "cancel, or modify an order.",
                "This message is not investment advice and this system is not a "
                "licensed advisor.",
            ]
        ),
    }
