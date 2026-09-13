"""The approval card for a ``propose_order`` row.

The email is the decision at a glance: the ticker, the status, entry/stop, the
size, the budget it draws from, and the chart. The page is the full record that
Spec L §6.6 and AGENTS.md §6 ask for — every cap with its limit and whether it
**bound**, the evidence with a negative lower bound printed as one, the cohort
answer's warnings verbatim, the linked thesis and its invalidators, the exposure
impact, the stored bear case, and the provenance and staleness of every number.

Everything on both comes out of :func:`portfolio.proposals.proposal_payload` and
the optional context the caller hands in. **Nothing is computed here**: the
multiplier, the effective fraction and every cap were decided by
``portfolio/sizing.py`` and ``portfolio/evidence.py`` and are printed as they
were stored (AGENTS.md §1.2).

The approvable/refused distinction is load-bearing and is stated three times on
purpose — in the status pill, in the summary, and in the closing note — because
a refusal that reads like an approval is the one rendering mistake in this
system that could cost money.
"""

from __future__ import annotations

from notify.channel import KIND_PROPOSAL

#: Statuses, mirrored from `portfolio.proposals` rather than imported: this
#: package deliberately imports nothing from `portfolio`, so that the workspace
#: import closure through `notify` stays as small as the test asserts.
PROPOSED = "proposed"
RISK_REJECTED = "risk_rejected"


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fraction(value, digits: int = 5) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _signed(value, digits: int = 4) -> str:
    """A lower bound that is negative prints as negative. That is the point."""
    try:
        return f"{float(value):+.{digits}f}"
    except (TypeError, ValueError):
        return "unknown"


def _cap_rows(caps: dict) -> list[list]:
    rows = []
    for name, record in sorted((caps or {}).items()):
        record = record if isinstance(record, dict) else {}
        limit = record.get("limit_notional", record.get("limit"))
        rows.append(
            [
                name,
                "BOUND" if record.get("bound") else "ok",
                "—" if limit is None else limit,
                "—" if record.get("shares_allowed") is None else record.get("shares_allowed"),
            ]
        )
    return rows


def _bound_caps(caps: dict) -> list[str]:
    return sorted(
        name
        for name, record in (caps or {}).items()
        if isinstance(record, dict) and record.get("bound")
    )


def build_payload(
    *,
    proposal: dict,
    approvable: bool,
    uid: str = "",
    created_at_utc: str = "",
    chart: dict | None = None,
    thesis: dict | None = None,
    invalidators=(),
    bear_case: dict | None = None,
    cohort: dict | None = None,
    exposure: dict | None = None,
    ledger_stale: bool = False,
) -> dict:
    """The stored document for one proposal card.

    ``proposal`` is ``portfolio.proposals.proposal_payload(row)`` verbatim.
    Every other argument is optional context the caller could read cheaply; a
    missing one is simply a section the card does not print, never a section it
    fills in with a guess.
    """
    proposal = dict(proposal or {})
    evidence = dict(proposal.get("evidence") or {})
    caps = dict(proposal.get("caps") or {})
    status = str(proposal.get("status") or "")
    refused = status == RISK_REJECTED
    ticker = str(proposal.get("ticker") or "")
    budget = str(proposal.get("budget") or "")

    verdict = (
        {"label": f"refused · {proposal.get('rejection_code') or 'risk'}", "tone": "bad"}
        if refused
        else ({"label": "awaiting approval", "tone": "ok"} if approvable else {"label": status, "tone": "warn"})
    )

    blocks: list[dict] = []

    # -- the decision at a glance ------------------------------------------ #
    blocks.append(
        {
            "type": "rows",
            "rows": [
                {"label": "entry", "value": _money(proposal.get("entry"))},
                {"label": "stop", "value": _money(proposal.get("stop"))},
                {
                    "label": "size",
                    "value": f"{proposal.get('quantity', 0)} shares",
                    "note": f"{_money(proposal.get('notional'))} notional · "
                    f"{_money(proposal.get('risk_dollars'))} at risk",
                },
                {
                    "label": "budget",
                    "value": budget or "—",
                    "tone": "ok" if budget == "evidenced" else "warn",
                    "note": (
                        "Evidenced: a qualifying cohort answer for this ticker sized it."
                        if budget == "evidenced"
                        else "Discretionary: judgment, drawn from the separate budget "
                        "with its own per-trade cap and daily notional."
                    ),
                },
                {
                    "label": "expected hold",
                    "value": (
                        f"{proposal['expected_hold_sessions']} sessions"
                        if proposal.get("expected_hold_sessions")
                        else "—"
                    ),
                },
                # The ledger's as-of and its stale flag belong *here*, in the
                # email, and not only in the page-only provenance block below.
                # Every figure above was sized against this read of the book, so
                # AGENTS.md §1.3 — repeat the staleness wherever you repeat the
                # number — applies to the summary as much as to the full record.
                # It is printed twice on the page for the same reason.
                {
                    "label": "ledger as of",
                    "value": proposal.get("ledger_as_of_utc") or "never",
                    "stale": bool(ledger_stale),
                },
            ],
        }
    )

    if chart and chart.get("bars"):
        blocks.append(
            {
                "type": "chart",
                "alt": f"{ticker} daily closes with entry, stop and target",
                "caption": chart.get("caption") or "",
            }
        )

    # -- the risk math ------------------------------------------------------ #
    blocks.append(
        {
            "type": "rows",
            "title": "risk math",
            "rows": [
                {"label": "risk_fraction requested", "value": _fraction(proposal.get("risk_fraction"))},
                {
                    "label": "m (multiplier)",
                    "value": "n/a" if evidence.get("m") is None else _fraction(evidence.get("m"), 4),
                },
                {
                    "label": "risk_fraction effective",
                    "value": _fraction(proposal.get("risk_fraction_effective")),
                },
                {"label": "LB (lower 90% bound)", "value": _signed(evidence.get("lower_bound"))},
                {"label": "PE (point estimate)", "value": _signed(evidence.get("point_estimate"))},
                {
                    "label": "horizon",
                    "value": evidence.get("horizon_sessions") or "n/a",
                    "note": "trading sessions",
                },
                {"label": "gate mode", "value": evidence.get("gate_mode") or "—"},
                {
                    "label": "caps that bound the size",
                    "value": ", ".join(_bound_caps(caps)) or "none",
                    "tone": "warn" if _bound_caps(caps) else "",
                },
            ],
        }
    )
    if evidence.get("reason"):
        blocks.append({"type": "text", "body": str(evidence["reason"]), "muted": True})

    if caps:
        blocks.append(
            {
                "type": "table",
                "title": "every cap, bound or not",
                "columns": ["cap", "state", "limit", "shares allowed"],
                "rows": _cap_rows(caps),
                "note": "Size is the minimum over every cap. `BOUND` is the one that decided it.",
                "page_only": True,
            }
        )

    # -- the cohort evidence ------------------------------------------------ #
    if cohort:
        cohort_rows = [
            {"label": "cohort_answer_id", "value": evidence.get("cohort_answer_id") or "none"},
            {"label": "status", "value": cohort.get("status") or "—"},
            {"label": "depth", "value": cohort.get("depth") or "—"},
            {"label": "n", "value": cohort.get("n_events") if cohort.get("n_events") is not None else "—"},
            {
                "label": "subject qualified",
                "value": cohort.get("subject_qualifies"),
                "note": str(cohort.get("subject_reason") or ""),
            },
            {
                "label": "computed",
                "value": cohort.get("as_of_utc") or "—",
                "stale": bool(cohort.get("stale")),
            },
        ]
        blocks.append({"type": "rows", "title": "cohort evidence", "rows": cohort_rows, "page_only": True})
        warnings = [str(warning) for warning in (cohort.get("warnings") or [])]
        if warnings:
            blocks.append(
                {
                    "type": "list",
                    "title": "cohort warnings (verbatim)",
                    "items": warnings,
                    "page_only": True,
                }
            )
    elif evidence.get("cohort_answer_id"):
        blocks.append(
            {
                "type": "rows",
                "title": "cohort evidence",
                "rows": [{"label": "cohort_answer_id", "value": evidence["cohort_answer_id"]}],
                "page_only": True,
            }
        )

    # -- the thesis and its invalidators ------------------------------------ #
    if thesis:
        blocks.append(
            {
                "type": "rows",
                "title": "linked thesis",
                "rows": [
                    {"label": "state", "value": thesis.get("state") or "—"},
                    {"label": "conviction", "value": thesis.get("conviction") or "—"},
                    {
                        "label": "written",
                        "value": thesis.get("as_of_utc") or "—",
                        "stale": bool(thesis.get("stale")),
                    },
                ],
                "page_only": True,
            }
        )
        if thesis.get("summary"):
            blocks.append({"type": "text", "body": str(thesis["summary"]), "page_only": True})
    invalidator_items = [str(item) for item in (invalidators or [])]
    if invalidator_items:
        blocks.append(
            {
                "type": "list",
                "title": "invalidators",
                "items": invalidator_items,
                "page_only": True,
            }
        )

    if bear_case:
        blocks.append(
            {
                "type": "quote",
                "title": "bear case (thesis-critic, stored)",
                "body": str(bear_case.get("body") or ""),
                "source": str(bear_case.get("source") or "thesis-critic"),
                "page_only": True,
            }
        )

    # -- exposure impact ---------------------------------------------------- #
    if exposure:
        blocks.append(
            {
                "type": "rows",
                "title": "exposure impact",
                "rows": [
                    {"label": key, "value": value}
                    for key, value in (exposure.get("rows") or {}).items()
                ],
                "page_only": True,
            }
        )
        if exposure.get("note"):
            blocks.append({"type": "text", "body": str(exposure["note"]), "muted": True, "page_only": True})

    # -- provenance --------------------------------------------------------- #
    blocks.append(
        {
            "type": "rows",
            "title": "provenance",
            "rows": [
                {
                    "label": "ledger as of",
                    "value": proposal.get("ledger_as_of_utc") or "never",
                    "stale": bool(ledger_stale),
                    "note": "Every figure above was sized against this read of the book.",
                },
                {"label": "equity at proposal", "value": _money(proposal.get("equity_at_proposal"))},
                {"label": "account", "value": proposal.get("account") or "unknown"},
                {"label": "execution mode", "value": proposal.get("execution_mode") or "—"},
                {"label": "context hash", "value": str(proposal.get("portfolio_context_hash") or "")[:16]},
                {"label": "proposal uid", "value": proposal.get("proposal_uid") or ""},
                {"label": "approval expires", "value": proposal.get("approval_expires_at") or "n/a"},
            ],
            "page_only": True,
        }
    )

    if refused:
        blocks.append(
            {
                "type": "text",
                "title": "refused",
                "body": (
                    f"{proposal.get('rejection_code') or ''}\n"
                    f"{proposal.get('rejection_reason') or ''}\n\n"
                    "Nothing to approve. Risk is re-evaluated from fresh state at approval "
                    "time in any case, so a refusal here is not a reason to work around "
                    "this card."
                ).strip(),
            }
        )
    else:
        blocks.append(
            {
                "type": "text",
                "body": (
                    "Approving places a live entry and then a `gtc` `stop_market` protective "
                    "exit. Approval happens on Telegram, not here: this card is a record, and "
                    "nothing on this page or in this email can approve, modify, or place an "
                    "order. Risk is re-computed from fresh state first; the approval is "
                    "single-use, expiring, and bound to you."
                ),
                "muted": True,
            }
        )

    subject_status = "REFUSED" if refused else "approval needed"
    return {
        "version": 1,
        "kind": KIND_PROPOSAL,
        "uid": uid,
        "ref": str(proposal.get("proposal_uid") or ""),
        "created_at_utc": created_at_utc,
        "subject": f"[{subject_status}] {ticker} — proposal {proposal.get('proposal_id')}",
        "eyebrow": f"proposal {proposal.get('proposal_id')} · {proposal.get('side') or 'long'}",
        "title": ticker,
        "headline": (
            f"{_money(proposal.get('entry'))} entry, {_money(proposal.get('stop'))} stop, "
            f"{proposal.get('quantity', 0)} shares — {budget or 'unlabelled'} budget"
        ),
        "verdict": verdict,
        "link_label": "Open the full card",
        "blocks": blocks,
        "chart": dict(chart) if chart else None,
        "footer": [
            "SwingTrader — an agent proposes, the owner approves out of band, code executes.",
            "This message is not investment advice and this system is not a licensed advisor.",
        ],
    }
