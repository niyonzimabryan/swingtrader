"""The scan-memo card.

What the Telegram scan-complete message says today — the run's counts and one
line per name with its score, classification and Opus recommendation — plus, on
the page, the memo body itself and a chart of the name when one was supplied.

The body is rendered as a ``quote`` block rather than as prose, and a memo built
from filing or news text is marked ``untrusted``: that text is attacker-writable
(AGENTS.md §5) and has to stay visibly distinct wherever a human reads it, an
email included.
"""

from __future__ import annotations

from notify.channel import KIND_SCAN_MEMO

#: Recommendation -> the tone the pill uses. `pass` is not a failure and does
#: not get the red one; `reduce_size` is the one that wants a second look.
_TONES = {
    "proceed": "ok",
    "reduce_size": "warn",
    "watchlist": "neutral",
    "pass": "neutral",
}


def _memo_rows(memos) -> list[list]:
    rows = []
    for memo in memos or []:
        memo = memo if isinstance(memo, dict) else {}
        score = memo.get("score")
        rows.append(
            [
                memo.get("ticker") or "?",
                "—" if score is None else f"{float(score):.2f}",
                memo.get("classification") or "—",
                memo.get("opus_recommendation") or "—",
                "auto (paper)" if memo.get("auto_executed") else "",
            ]
        )
    return rows


def build_scan_payload(
    *,
    scan_type: str,
    duration_text: str,
    total_scanned,
    escalated,
    memos_generated,
    memos=(),
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    as_of_utc: str = "",
    stale: bool = False,
) -> dict:
    """The run-level card: one scan, every memo it produced."""
    memos = list(memos or [])
    blocks: list[dict] = [
        {
            "type": "rows",
            "rows": [
                {"label": "duration", "value": duration_text or "—"},
                {"label": "tickers scanned", "value": total_scanned},
                {"label": "escalated", "value": escalated},
                {
                    "label": "memos generated",
                    "value": memos_generated,
                    "tone": "ok" if memos_generated else "neutral",
                },
                {"label": "scan completed", "value": as_of_utc or "—", "stale": bool(stale)},
            ],
        }
    ]
    if memos:
        blocks.append(
            {
                "type": "table",
                "title": "memos",
                "columns": ["ticker", "score", "classification", "recommendation", "note"],
                "rows": _memo_rows(memos),
                "note": "Scores and classifications are the scan's own; nothing here re-ranks them.",
            }
        )
    else:
        blocks.append(
            {
                "type": "text",
                "body": "No opportunity met the memo threshold on this scan.",
                "muted": True,
            }
        )

    return {
        "version": 1,
        "kind": KIND_SCAN_MEMO,
        "uid": uid,
        "ref": ref or f"scan:{scan_type}",
        "created_at_utc": created_at_utc,
        "subject": f"Scan complete ({scan_type}) — {memos_generated} memo(s)",
        "eyebrow": "scan complete",
        "title": str(scan_type or "scan"),
        "headline": f"{total_scanned} scanned · {escalated} escalated · {memos_generated} memos",
        "verdict": {
            "label": f"{memos_generated} memo(s)",
            "tone": "ok" if memos_generated else "neutral",
        },
        "link_label": "Open the scan card",
        "blocks": blocks,
        "chart": None,
        "footer": [
            "SwingTrader — a memo is an input to a decision, never a decision.",
            "This message is not investment advice and this system is not a licensed advisor.",
        ],
    }


def build_memo_payload(
    *,
    ticker: str,
    score=None,
    classification: str = "",
    recommendation: str = "",
    body: str = "",
    body_trust: str = "",
    body_source: str = "",
    levels: dict | None = None,
    chart: dict | None = None,
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
    as_of_utc: str = "",
    stale: bool = False,
) -> dict:
    """One name's memo, with its chart and its body."""
    blocks: list[dict] = [
        {
            "type": "rows",
            "rows": [
                {"label": "score", "value": "—" if score is None else f"{float(score):.2f}"},
                {"label": "classification", "value": classification or "—"},
                {
                    "label": "recommendation",
                    "value": recommendation or "—",
                    "tone": _TONES.get(str(recommendation or "").lower(), "neutral"),
                },
                {"label": "as of", "value": as_of_utc or "—", "stale": bool(stale)},
            ],
        }
    ]
    if chart and chart.get("bars"):
        blocks.append(
            {
                "type": "chart",
                "alt": f"{ticker} daily closes",
                "caption": chart.get("caption") or "",
            }
        )
    if levels:
        blocks.append(
            {
                "type": "rows",
                "title": "reference levels",
                "rows": [{"label": key, "value": value} for key, value in levels.items()],
                "note": "",
            }
        )
    if body:
        blocks.append(
            {
                "type": "quote",
                "title": "memo",
                "body": body,
                "source": body_source,
                "trust": body_trust,
                "page_only": True,
            }
        )

    return {
        "version": 1,
        "kind": KIND_SCAN_MEMO,
        "uid": uid,
        "ref": ref or f"memo:{ticker}",
        "created_at_utc": created_at_utc,
        "subject": f"{ticker} — {recommendation or classification or 'memo'}",
        "eyebrow": "scan memo",
        "title": str(ticker or ""),
        "headline": classification or "",
        "verdict": {
            "label": recommendation or "memo",
            "tone": _TONES.get(str(recommendation or "").lower(), "neutral"),
        },
        "link_label": "Open the memo card",
        "blocks": blocks,
        "chart": dict(chart) if chart else None,
        "footer": [
            "SwingTrader — a memo is an input to a decision, never a decision.",
            "This message is not investment advice and this system is not a licensed advisor.",
        ],
    }
