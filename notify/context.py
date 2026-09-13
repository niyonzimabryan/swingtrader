"""Reading the context a proposal card's *page* shows, best-effort.

The email is the decision at a glance and needs only the proposal row. The page
is the full record Spec L §6.6 and AGENTS.md §6 ask for, and three of the things
it prints — the cohort answer behind the citation, the linked thesis with its
invalidators, and the stored bear case — live in tables the proposal path does
not otherwise read. This module reads them.

**Every function here fails soft and returns ``None``.** A card is a report about
work that already happened; a missing thesis, an unresolvable citation, or a
database that blinked must produce a card with one fewer section, never a lost
card and never an invented one. "Absent" and "unknown" are printable; a guess is
not (AGENTS.md §7's rule for a subagent is the same rule for a renderer).

**Nothing here computes a statistic.** Every value is read off a stored row —
the cohort answer's own ``warnings`` list, the row's own ``status`` and
``depth``, the thesis's own ``bear_case`` — and handed to the card unchanged.
Read-only throughout: no function in this module writes.
"""

from __future__ import annotations

from utils.logger import get_logger

log = get_logger("notify_context")

#: A dossier section or thesis older than this is *marked* stale on the card.
#: Mirrors `Settings.research_section_stale_days`, read from settings when one
#: is passed, so the card and the research tools agree on the same horizon.
DEFAULT_STALE_DAYS = 90


def _session_factory(factory=None):
    if factory is not None:
        return factory
    from database.db import get_session

    return get_session


def cohort_context(cohort_answer_id: str, *, session_factory=None) -> dict | None:
    """The stored cohort answer behind a citation, as the card prints it.

    ``require_citable=False``: this is a *display* path, and an answer that is
    `quick` or `insufficient` is exactly the one a reader most needs to see
    labelled. The sizing decision was made elsewhere, by
    ``portfolio/evidence.py``, against the citable rule — showing the answer
    here does not re-open it.
    """
    if not cohort_answer_id:
        return None
    try:
        from comparables import citations as cohort_citations

        with _session_factory(session_factory)() as session:
            resolved = cohort_citations.resolve(
                session, cohort_answer_id, require_citable=False
            )
            answer = dict(resolved.answer or {})
            return {
                "status": resolved.status or "unknown",
                "depth": resolved.depth or "unknown",
                # The answer's own count. `n_matured` is what the cohort engine
                # stored; this does not add the censored ones back in or pick a
                # friendlier field.
                "n_events": answer.get("n_matured"),
                "subject_qualifies": resolved.subject_qualifies,
                "subject_reason": resolved.subject_reason or "",
                "as_of_utc": resolved.as_of.isoformat() if resolved.as_of else "",
                # The answer's own list, printed verbatim. A warning summarised
                # is a warning weakened.
                "warnings": [str(warning) for warning in (answer.get("warnings") or [])],
            }
    except Exception as exc:
        log.info("card_cohort_context_unavailable", citation=str(cohort_answer_id), detail=str(exc))
        return None


def thesis_context(ticker: str, *, settings=None, session_factory=None) -> dict | None:
    """The active thesis for ``ticker``, its invalidators, and its bear case.

    Returns ``{"thesis": {...}, "invalidators": [...], "bear_case": {...}|None}``
    or ``None`` when there is no active thesis — which is a real and common
    state, not an error: a proposal may legitimately precede a written thesis,
    and the card saying nothing is more honest than the card implying one.

    Only an **active** thesis is shown. A draft is not yet a position's
    reasoning, and printing one on an approval card would give it a standing it
    has not earned (Spec M §7).
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    try:
        from research_workspace import store as research_store

        stale_days = int(getattr(settings, "research_section_stale_days", DEFAULT_STALE_DAYS) or DEFAULT_STALE_DAYS)

        with _session_factory(session_factory)() as session:
            active = research_store.theses_for(session, symbol, statuses=["active"])
            if not active:
                return None
            thesis = active[0]
            invalidators = research_store.invalidators_for(session, thesis.id)
            written = getattr(thesis, "activated_at", None) or getattr(thesis, "updated_at", None)
            stale = False
            if written is not None:
                from utils.timeutils import utcnow_naive

                stale = (utcnow_naive() - written).days > stale_days
            payload = {
                "thesis": {
                    "state": str(getattr(thesis, "status", "") or ""),
                    # The stored probability, not a word for it: "conviction" is
                    # the label, the number is the thesis's own.
                    "conviction": (
                        "unstated"
                        if getattr(thesis, "probability", None) is None
                        else f"p={thesis.probability}"
                    ),
                    "as_of_utc": written.isoformat() if written is not None else "",
                    "stale": stale,
                    "summary": str(getattr(thesis, "claim", "") or getattr(thesis, "title", "") or ""),
                },
                "invalidators": [
                    f"{inv.description} [{inv.status}]"
                    for inv in invalidators
                    if getattr(inv, "description", "")
                ],
                "bear_case": None,
            }
            bear = str(getattr(thesis, "bear_case", "") or "")
            if bear:
                payload["bear_case"] = {
                    "body": bear,
                    # Attributed, always. Spec M §7: the bear case is stored as
                    # the critic's and never quietly merged into the bull case.
                    "source": str(getattr(thesis, "bear_case_author", "") or "thesis-critic"),
                }
            return payload
    except Exception as exc:
        log.info("card_thesis_context_unavailable", ticker=symbol, detail=str(exc))
        return None


def proposal_context(proposal: dict, *, settings=None, session_factory=None) -> dict:
    """Everything the page adds to the proposal row. Missing parts are absent.

    Returns the keyword arguments ``notify.cards.proposal.build_payload`` takes
    for its optional sections, so a caller hands the result straight through.

    ``exposure`` is deliberately **not** here: the combined-book concentration
    and sector figures are computed by ``portfolio.proposals.read_context`` when
    the proposal is sized, and are not carried on the row. Re-deriving them here
    would be a second implementation of the same arithmetic that could disagree
    with the one that actually bound the size — exactly the failure the single
    stored payload exists to prevent. The renderer prints an ``exposure``
    section when it is given one; putting it on the row is its own change.
    """
    proposal = dict(proposal or {})
    evidence = dict(proposal.get("evidence") or {})
    context: dict = {}

    cohort = cohort_context(
        str(evidence.get("cohort_answer_id") or ""), session_factory=session_factory
    )
    if cohort is not None:
        context["cohort"] = cohort

    thesis = thesis_context(
        str(proposal.get("ticker") or ""), settings=settings, session_factory=session_factory
    )
    if thesis is not None:
        context["thesis"] = thesis["thesis"]
        context["invalidators"] = thesis["invalidators"]
        if thesis["bear_case"]:
            context["bear_case"] = thesis["bear_case"]

    return context
