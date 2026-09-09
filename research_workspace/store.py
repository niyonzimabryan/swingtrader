"""Every read and write of the research tables (Spec M §3, §4).

The rules that make this workspace worth having are enforced here, in code,
with readable refusals — the database CHECK constraints in
``migrations/versions/0004_research_workspace.py`` are the backstop under them,
not the primary gate, because a constraint violation is not a sentence an agent
can act on.

The four that matter:

* **Sections are append-only.** :func:`write_section` inserts a revision and
  points it at the row it supersedes. Nothing is updated in place, so "what did
  we believe in July" is a query.
* **A thesis cannot reach ``active``** without a bear case, a stated
  probability, a ``resolution_at``, and at least one machine-checkable
  invalidator (:func:`activate`).
* **An invalidator added after the position opens is flagged ``post_hoc``**,
  kept, and excluded from the honesty metrics — never silently accepted as
  though it had been set in advance.
* **A cited cohort answer must be citable.** :func:`journal_append` runs it
  through ``comparables.report.assert_citable`` and additionally requires
  ``status="ok"``: a `quick` answer, an `insufficient` one, and an
  `inconclusive` one are all evidence of something, but none of them is
  evidence *for* a position.

Nothing in this module writes an order, a proposal, or a position.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from database.models import (
    DECISION_BUDGETS,
    DECISION_KINDS,
    DecisionJournalEntry,
    Dossier,
    DossierSection,
    ResearchQuestion,
    Thesis,
    ThesisInvalidator,
)
from research_workspace import invalidators as invalidator_module
from research_workspace import trust
from research_workspace.errors import ResearchRefused
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("research_store")

HUMAN = "human"
MODEL = "model"

#: The section keys Spec M §3 names as structured metadata. Others are allowed:
#: this is the vocabulary the mirror orders by, not a whitelist.
CANONICAL_SECTION_KEYS: tuple[str, ...] = (
    "business_model",
    "revenue_drivers",
    "key_customers",
    "competitive_position",
    "capital_structure",
    "risks",
    "notes",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-")[:120] or "thesis"


def _canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(payload) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _author_kind(author: str, human_authored: bool) -> str:
    """``human`` or ``model``, so model-written prose is visibly distinct (§7)."""
    if human_authored or (author or "").strip().lower() in ("human", "bryan", "owner"):
        return HUMAN
    return MODEL


# --------------------------------------------------------------------------- #
# Dossiers
# --------------------------------------------------------------------------- #


def get_dossier(session, ticker: str) -> Dossier | None:
    return (
        session.query(Dossier).filter(Dossier.ticker == (ticker or "").upper()).first()
    )


def upsert_dossier(session, ticker: str, *, company_name="", sector="") -> Dossier:
    """One dossier per ticker. Metadata is updated; sections never are."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ResearchRefused("missing_ticker", "a dossier needs a ticker")
    dossier = get_dossier(session, ticker)
    now = utcnow_naive()
    if dossier is None:
        dossier = Dossier(
            ticker=ticker,
            company_name=company_name or "",
            sector=sector or "",
            created_at=now,
            updated_at=now,
        )
        session.add(dossier)
        session.flush()
        return dossier
    if company_name:
        dossier.company_name = company_name
    if sector:
        dossier.sector = sector
    dossier.updated_at = now
    session.flush()
    return dossier


def write_section(
    session,
    ticker: str,
    section_key: str,
    body_md: str,
    *,
    sources=(),
    author: str = HUMAN,
    human_authored: bool = False,
    company_name: str = "",
    sector: str = "",
) -> DossierSection:
    """Append a dossier revision. Never an update (Spec M §3).

    Refuses a section whose sources are **all** untrusted-tier unless
    ``human_authored`` is set (Spec P §5). A section with **no** sources is not
    refused: it is written with ``unsourced=True`` and rendered with a warning
    everywhere it appears (Spec M §3), because an honest gap in the record is
    more useful than a gap in the record that had to be laundered to be written.
    """
    if not (section_key or "").strip():
        raise ResearchRefused("missing_section_key", "a section needs a section_key")
    normalised = trust.normalise_sources(sources)
    if trust.refuses_all_untrusted(normalised) and not human_authored:
        raise ResearchRefused(
            "all_sources_untrusted",
            "every source for this section is untrusted-tier "
            f"({sorted(trust.tiers_of(normalised))}); a section resting only on "
            "text someone else wrote needs human_authored=true, asserting that "
            "a human wrote the prose and read the sources (Spec P §5).",
        )

    dossier = upsert_dossier(
        session, ticker, company_name=company_name, sector=sector
    )
    now = utcnow_naive()
    previous = current_section(session, dossier.id, section_key)
    section = DossierSection(
        dossier_id=dossier.id,
        section_key=section_key.strip(),
        body_md=body_md or "",
        sources_json=_canonical_json(normalised),
        author=author or HUMAN,
        author_kind=_author_kind(author, human_authored),
        unsourced=not normalised,
        human_authored=bool(human_authored),
        supersedes_id=previous.id if previous else None,
        superseded=False,
        created_at=now,
    )
    session.add(section)
    if previous is not None:
        previous.superseded = True
    dossier.updated_at = now
    session.flush()
    log.info(
        "research_section_written",
        ticker=dossier.ticker,
        section_key=section.section_key,
        unsourced=section.unsourced,
        content_trust=trust.content_trust(normalised),
        supersedes_id=section.supersedes_id,
    )
    return section


def current_section(session, dossier_id: int, section_key: str) -> DossierSection | None:
    return (
        session.query(DossierSection)
        .filter(DossierSection.dossier_id == dossier_id)
        .filter(DossierSection.section_key == section_key.strip())
        .filter(DossierSection.superseded.is_(False))
        .order_by(DossierSection.id.desc())
        .first()
    )


def current_sections(session, ticker: str) -> list[DossierSection]:
    """The live revision of every section, canonical keys first."""
    dossier = get_dossier(session, ticker)
    if dossier is None:
        return []
    rows = (
        session.query(DossierSection)
        .filter(DossierSection.dossier_id == dossier.id)
        .filter(DossierSection.superseded.is_(False))
        .order_by(DossierSection.id.asc())
        .all()
    )
    order = {key: i for i, key in enumerate(CANONICAL_SECTION_KEYS)}
    return sorted(rows, key=lambda r: (order.get(r.section_key, 999), r.section_key))


def section_history(session, ticker: str, section_key: str) -> list[DossierSection]:
    """Every revision of one section, oldest first. Nothing was overwritten."""
    dossier = get_dossier(session, ticker)
    if dossier is None:
        return []
    return (
        session.query(DossierSection)
        .filter(DossierSection.dossier_id == dossier.id)
        .filter(DossierSection.section_key == section_key)
        .order_by(DossierSection.id.asc())
        .all()
    )


def is_stale(section: DossierSection, *, horizon_days: int, now=None) -> bool:
    """Spec M §6: older than the horizon is marked ``stale``, never hidden."""
    now = now or utcnow_naive()
    created = section.created_at or now
    return (now - created) > timedelta(days=int(horizon_days))


# --------------------------------------------------------------------------- #
# Theses
# --------------------------------------------------------------------------- #


def get_thesis(session, thesis_id: int) -> Thesis | None:
    return session.query(Thesis).filter(Thesis.id == int(thesis_id)).first()


def theses_for(session, ticker: str, *, statuses=None) -> list[Thesis]:
    query = session.query(Thesis).filter(Thesis.ticker == (ticker or "").upper())
    if statuses:
        query = query.filter(Thesis.status.in_(list(statuses)))
    return query.order_by(Thesis.id.asc()).all()


def create_thesis(
    session,
    *,
    ticker: str,
    title: str,
    claim: str = "",
    direction: str = "long",
    horizon_days: int | None = None,
    argument=None,
    bear_case: str = "",
    bear_case_author: str = "",
    author: str = HUMAN,
    linked_position_ref: str = "",
    position_opened_at: datetime | None = None,
    cohort_answer_ids=None,
    next_review_at: date | None = None,
) -> Thesis:
    """Create a thesis in ``draft``. Everything reaches ``active`` through
    :func:`activate`, so there is exactly one place the gate can be applied."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ResearchRefused("missing_ticker", "a thesis needs a ticker")
    if not (title or "").strip():
        raise ResearchRefused("missing_title", "a thesis needs a title")
    now = utcnow_naive()
    thesis = Thesis(
        ticker=ticker,
        slug=slugify(title),
        title=title.strip(),
        claim=claim or "",
        direction=direction or "long",
        horizon_days=horizon_days,
        argument_json=_canonical_json(list(argument or [])),
        bear_case=bear_case or "",
        bear_case_author=bear_case_author or "",
        status="draft",
        machine_checkable_invalidators=0,
        linked_position_ref=linked_position_ref or "",
        position_opened_at=position_opened_at,
        cohort_answer_ids_json=_canonical_json(list(cohort_answer_ids or [])),
        next_review_at=next_review_at,
        author=author or HUMAN,
        author_kind=_author_kind(author, False),
        created_at=now,
        updated_at=now,
    )
    session.add(thesis)
    session.flush()
    return thesis


def set_probability(
    session,
    thesis: Thesis,
    probability: float,
    *,
    resolution_at: date | None = None,
    observable: str = "",
    reason: str = "",
    author: str = HUMAN,
) -> Thesis:
    """State (or revise) the probability. The **original** is kept forever.

    Spec M §6: a probability revised after entry is scored on the original
    number and the revision is kept in history. So the first number written
    lands in ``original_probability`` and is never touched again.
    """
    try:
        probability = float(probability)
    except (TypeError, ValueError):
        raise ResearchRefused(
            "invalid_probability", f"probability must be a number, got {probability!r}"
        ) from None
    if not 0.0 <= probability <= 1.0:
        raise ResearchRefused(
            "invalid_probability",
            f"probability must be between 0 and 1, got {probability}",
        )
    now = utcnow_naive()
    history = thesis.probability_history
    history.append(
        {
            "probability": probability,
            "at": now.isoformat(),
            "author": author or HUMAN,
            "reason": reason or "",
            "status_at_revision": thesis.status,
        }
    )
    if thesis.original_probability is None:
        thesis.original_probability = probability
    thesis.probability = probability
    thesis.probability_history_json = _canonical_json(history)
    if resolution_at is not None:
        thesis.resolution_at = resolution_at
    if observable:
        thesis.resolution_observable = observable
    thesis.updated_at = now
    session.flush()
    return thesis


def set_argument(session, thesis: Thesis, argument) -> Thesis:
    """Replace the numbered argument. The claims are prose; the *gate* is
    elsewhere, so this needs no check of its own."""
    thesis.argument_json = _canonical_json(list(argument or []))
    thesis.updated_at = utcnow_naive()
    session.flush()
    return thesis


def add_invalidator(
    session,
    thesis: Thesis,
    *,
    description: str,
    type: str,
    params=None,
    author: str = HUMAN,
    now: datetime | None = None,
) -> ThesisInvalidator:
    """Attach an invalidator, validating its parameters at write time.

    ``post_hoc`` is decided here and never by the caller: an invalidator whose
    ``created_at`` is after the linked position's open date was not set before
    the position, whatever the person writing it believes (Spec M §4 rule 3).
    """
    if not (description or "").strip():
        raise ResearchRefused(
            "missing_description",
            "an invalidator needs a description a human can read; the parameters "
            "say how it is checked, not what it means",
        )
    validated = invalidator_module.validate_params(type, params)
    now = now or utcnow_naive()
    machine_checkable = invalidator_module.is_machine_checkable(type)
    post_hoc = bool(
        thesis.position_opened_at is not None and now > thesis.position_opened_at
    )
    row = ThesisInvalidator(
        thesis_id=thesis.id,
        description=description.strip(),
        type=type,
        params_json=_canonical_json(validated),
        machine_checkable=machine_checkable,
        post_hoc=post_hoc,
        # `armed` means "a job is watching this". A qualitative invalidator is
        # not watched by any job, so it is parked for the human instead.
        status="armed" if machine_checkable else "needs_human_review",
        created_at=now,
        author=author or HUMAN,
    )
    session.add(row)
    session.flush()
    refresh_invalidator_count(session, thesis)
    if post_hoc:
        log.warning(
            "research_post_hoc_invalidator",
            thesis_id=thesis.id,
            ticker=thesis.ticker,
            invalidator_id=row.id,
            detail="set after the position opened; excluded from honesty metrics",
        )
    return row


def invalidators_for(session, thesis_id: int) -> list[ThesisInvalidator]:
    return (
        session.query(ThesisInvalidator)
        .filter(ThesisInvalidator.thesis_id == int(thesis_id))
        .order_by(ThesisInvalidator.id.asc())
        .all()
    )


def refresh_invalidator_count(session, thesis: Thesis) -> int:
    """Keep the denormalised count the DB CHECK constraint reads.

    Counts machine-checkable invalidators that have not been retired,
    ``post_hoc`` included: a post-hoc invalidator is excluded from the *honesty
    metrics* (§4 rule 3), not from the set of things being watched. Excluding it
    here would mean an active thesis losing its falsifiability the moment its
    position opened, which is backwards.
    """
    count = (
        session.query(ThesisInvalidator)
        .filter(ThesisInvalidator.thesis_id == thesis.id)
        .filter(ThesisInvalidator.machine_checkable.is_(True))
        .filter(ThesisInvalidator.status != "retired")
        .count()
    )
    thesis.machine_checkable_invalidators = int(count)
    session.flush()
    return int(count)


def activation_blockers(session, thesis: Thesis) -> list[str]:
    """Every reason this thesis cannot go ``active``, in one pass.

    All of them, not the first: a caller told about the missing probability,
    who adds it and is then told about the missing invalidator, learns that the
    gate is a maze. It is a checklist.
    """
    blockers: list[str] = []
    rows = invalidators_for(session, thesis.id)
    live = [r for r in rows if r.status != "retired"]
    if not live:
        blockers.append(
            "no invalidator: a thesis with nothing that would change its mind "
            "cannot leave draft (Spec M §4 rule 1)"
        )
    elif not any(r.machine_checkable for r in live):
        blockers.append(
            "no machine-checkable invalidator: every invalidator here is "
            "qualitative, and a thesis that can only be disproved by a change of "
            "mood is a feeling (Spec M §4 rule 2)"
        )
    if thesis.probability is None:
        blockers.append(
            "no stated probability: without a number the thesis can never be "
            "scored (Spec M §3)"
        )
    if thesis.resolution_at is None:
        blockers.append(
            "no resolution_at: a probability with no deadline resolves to "
            "nothing (Spec M §3)"
        )
    if not (thesis.bear_case or "").strip():
        blockers.append(
            "no bear case: required and non-empty before active (Spec M §3)"
        )
    return blockers


def activate(session, thesis: Thesis, *, author: str = HUMAN) -> Thesis:
    """``draft`` (or ``weakened``) → ``active``, or a refusal listing every gap."""
    blockers = activation_blockers(session, thesis)
    if blockers:
        raise ResearchRefused(
            "thesis_not_activatable",
            "this thesis cannot become active: " + "; ".join(blockers),
        )
    now = utcnow_naive()
    thesis.status = "active"
    thesis.activated_at = thesis.activated_at or now
    thesis.updated_at = now
    thesis.next_review_at = thesis.next_review_at or (now.date() + timedelta(days=30))
    refresh_invalidator_count(session, thesis)
    session.flush()
    log.info(
        "research_thesis_activated",
        thesis_id=thesis.id,
        ticker=thesis.ticker,
        probability=thesis.probability,
        machine_checkable=thesis.machine_checkable_invalidators,
    )
    return thesis


REVIEW_VERDICTS = ("hold", "weakened", "invalidated")


def review(
    session,
    thesis: Thesis,
    *,
    verdict: str,
    note: str = "",
    author: str = HUMAN,
    next_review_at: date | None = None,
    outcome: str | None = None,
) -> Thesis:
    """Record a review verdict (Spec K §4.2 ``thesis_review``).

    A verdict is a statement about the *thesis*. It never closes a position,
    creates an order, or creates a proposal — the same boundary the automated
    trigger path honours, for the same reason.
    """
    if verdict not in REVIEW_VERDICTS:
        raise ResearchRefused(
            "unknown_verdict",
            f"unknown verdict {verdict!r}; valid verdicts are {list(REVIEW_VERDICTS)}",
        )
    now = utcnow_naive()
    if verdict == "hold":
        thesis.next_review_at = next_review_at or (now.date() + timedelta(days=30))
    elif verdict == "weakened":
        if thesis.status == "active":
            thesis.status = "weakened"
            thesis.weakened_at = now
        thesis.next_review_at = next_review_at or (now.date() + timedelta(days=7))
    else:
        thesis.status = "invalidated"
        thesis.close_reason = "invalidated"
        thesis.closed_at = now
        thesis.next_review_at = None
        if outcome in ("true", "false"):
            thesis.outcome = outcome
            thesis.resolved_at = now
        elif thesis.outcome is None:
            thesis.outcome = "false"
            thesis.resolved_at = now
    thesis.updated_at = now
    session.flush()
    journal_note = (note or "").strip()
    log.info(
        "research_thesis_reviewed",
        thesis_id=thesis.id,
        ticker=thesis.ticker,
        verdict=verdict,
        status=thesis.status,
        note_len=len(journal_note),
    )
    return thesis


def resolve(
    session,
    thesis: Thesis,
    *,
    outcome: str,
    close_reason: str = "resolved",
    now: datetime | None = None,
) -> Thesis:
    """Record how a thesis actually turned out, for §6 scoring."""
    if outcome not in ("true", "false"):
        raise ResearchRefused(
            "unknown_outcome", f"outcome must be 'true' or 'false', got {outcome!r}"
        )
    now = now or utcnow_naive()
    thesis.outcome = outcome
    thesis.resolved_at = now
    thesis.closed_at = thesis.closed_at or now
    thesis.close_reason = close_reason or "resolved"
    if thesis.status not in ("invalidated", "closed"):
        thesis.status = "closed"
    thesis.updated_at = now
    session.flush()
    return thesis


def abandon(session, thesis: Thesis, *, reason: str = "abandoned") -> Thesis:
    """Close a thesis without a verdict — and record that that is what happened.

    Spec M §6 measures the share of theses that were *invalidated* versus
    *quietly abandoned*. That number only exists if abandonment is a distinct,
    recorded act rather than a row that stops being updated.
    """
    now = utcnow_naive()
    thesis.status = "closed"
    thesis.close_reason = reason or "abandoned"
    thesis.closed_at = now
    thesis.next_review_at = None
    thesis.updated_at = now
    session.flush()
    return thesis


def thesis_hash(session, thesis: Thesis) -> str:
    """A content hash of the thesis *and its invalidators* at this moment.

    The journal stores it so a later revision cannot rewrite the record of what
    was believed when the decision was made.
    """
    payload = {
        "ticker": thesis.ticker,
        "title": thesis.title,
        "claim": thesis.claim,
        "direction": thesis.direction,
        "probability": thesis.probability,
        "resolution_at": thesis.resolution_at.isoformat() if thesis.resolution_at else None,
        "argument": thesis.argument,
        "bear_case": thesis.bear_case,
        "status": thesis.status,
        "invalidators": [
            {"description": i.description, "type": i.type, "params": i.params}
            for i in invalidators_for(session, thesis.id)
        ],
    }
    return _sha256(payload)


def due_for_review(session, *, on: date | None = None) -> list[Thesis]:
    """Theses past ``next_review_at`` (Spec M §6), oldest review first.

    Spec M ranks them by position size × staleness. Position size is Spec L's
    ledger, a parallel phase, so the ranking here is staleness alone and the
    caller supplies size when it has it — an invented size would be worse than
    an honest partial ordering.
    """
    on = on or utcnow_naive().date()
    return (
        session.query(Thesis)
        .filter(Thesis.status.in_(("active", "weakened")))
        .filter(Thesis.next_review_at.isnot(None))
        .filter(Thesis.next_review_at <= on)
        .order_by(Thesis.next_review_at.asc(), Thesis.id.asc())
        .all()
    )


# --------------------------------------------------------------------------- #
# Decision journal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CitedAnswer:
    """A cohort answer accepted for citation, reduced to what the journal keeps."""

    cohort_answer_id: str
    evidence_hash: str


def cite_cohort_answer(answer) -> CitedAnswer:
    """Accept a cohort answer for citation, or refuse it (Spec N §8, Spec L §6.6).

    ``assert_citable`` refuses a ``quick`` answer and an ``insufficient`` one.
    This adds the remaining half of the rule: ``inconclusive`` is not a
    citation either. An answer that failed to find an effect is a real finding
    and belongs in the note; it is not evidence *for* the trade, and the
    journal is where that distinction has to hold or the evidenced/
    discretionary split means nothing.
    """
    from comparables.report import NotCitableError, assert_citable, to_json

    try:
        full = assert_citable(answer)
    except NotCitableError as exc:
        raise ResearchRefused("uncitable_cohort_answer", str(exc)) from exc
    if full.status != "ok":
        raise ResearchRefused(
            "uncitable_cohort_answer",
            f"a depth='full' answer with status={full.status!r} is not a "
            f"citation: {full.refusal_reason or 'no effect was established'} "
            f"(Spec L §6.6 — the decision may still be made, as "
            f"budget='discretionary')",
        )
    return CitedAnswer(
        cohort_answer_id=f"{full.setup.slug}@{full.setup.content_hash[:16]}",
        evidence_hash=hashlib.sha256(to_json(full).encode("utf-8")).hexdigest(),
    )


def journal_append(
    session,
    *,
    decision: str,
    tickers,
    occurred_on: date | None = None,
    thesis: Thesis | None = None,
    cohort_answer=None,
    cohort_answer_id: str = "",
    budget: str = "discretionary",
    sizing_rationale: str = "",
    expected_holding_days: int | None = None,
    note_md: str = "",
    author: str = HUMAN,
) -> DecisionJournalEntry:
    """Append a decision — including the decision to pass (Spec M §7 step 5).

    ``budget`` is Spec L §6.6's evidenced/discretionary split. ``evidenced``
    requires a citable cohort answer, so the label cannot be self-awarded; a
    decision made on judgment is written down as judgment. That is the whole
    point of keeping the two budgets in separate rows.
    """
    if decision not in DECISION_KINDS:
        raise ResearchRefused(
            "unknown_decision",
            f"unknown decision {decision!r}; valid decisions are {list(DECISION_KINDS)}",
        )
    if budget not in DECISION_BUDGETS:
        raise ResearchRefused(
            "unknown_budget",
            f"unknown budget {budget!r}; valid budgets are {list(DECISION_BUDGETS)}",
        )
    if isinstance(tickers, str):
        tickers = [tickers]
    symbols = [t.strip().upper() for t in (tickers or []) if str(t).strip()]
    if not symbols:
        raise ResearchRefused("missing_ticker", "a journal entry names at least one ticker")

    if cohort_answer is None and cohort_answer_id:
        from research_workspace import citations

        cohort_answer = citations.resolve(cohort_answer_id)
        if cohort_answer is None:
            raise ResearchRefused(
                "unresolvable_cohort_answer",
                f"cohort answer {cohort_answer_id!r} cannot be resolved, so its "
                f"depth and status cannot be checked. An unverifiable citation "
                f"is not a citation (Spec N §8); record the decision as "
                f"budget='discretionary' and say so in the note."
                + (
                    ""
                    if citations.has_resolver()
                    else " No cohort answer store is registered yet — that "
                    "arrives with compare_setups in Phase 3."
                ),
            )

    cited: CitedAnswer | None = None
    if cohort_answer is not None:
        cited = cite_cohort_answer(cohort_answer)
    if budget == "evidenced" and cited is None:
        raise ResearchRefused(
            "evidenced_needs_citation",
            "budget='evidenced' requires a cited depth='full' cohort answer with "
            "status='ok'; an uncited decision draws from the discretionary "
            "budget (Spec L §6.6)",
        )

    now = utcnow_naive()
    entry = DecisionJournalEntry(
        occurred_on=occurred_on or now.date(),
        tickers=",".join(symbols),
        decision=decision,
        thesis_id=thesis.id if thesis is not None else None,
        thesis_hash=thesis_hash(session, thesis) if thesis is not None else "",
        cohort_answer_id=cited.cohort_answer_id if cited else "",
        cohort_evidence_hash=cited.evidence_hash if cited else "",
        budget=budget,
        sizing_rationale=sizing_rationale or "",
        expected_holding_days=expected_holding_days,
        note_md=note_md or "",
        author=author or HUMAN,
        author_kind=_author_kind(author, False),
        created_at=now,
    )
    session.add(entry)
    session.flush()
    log.info(
        "research_journal_appended",
        decision=decision,
        tickers=entry.tickers,
        budget=budget,
        thesis_id=entry.thesis_id,
        cohort_answer_id=entry.cohort_answer_id,
    )
    return entry


def journal_entries(session, *, ticker: str = "", month: str = "") -> list[DecisionJournalEntry]:
    """Journal rows, oldest first. ``month`` is ``YYYY-MM``."""
    query = session.query(DecisionJournalEntry)
    if ticker:
        query = query.filter(DecisionJournalEntry.tickers.contains(ticker.upper()))
    rows = query.order_by(
        DecisionJournalEntry.occurred_on.asc(), DecisionJournalEntry.id.asc()
    ).all()
    if month:
        rows = [r for r in rows if r.occurred_on.strftime("%Y-%m") == month]
    return rows


def record_outcome(
    session,
    entry: DecisionJournalEntry,
    *,
    note: str = "",
    return_pct: float | None = None,
    exit_reason: str = "",
    matched_reason: bool | None = None,
) -> DecisionJournalEntry:
    """Fill in what actually happened. Written by a job, never at decision time."""
    entry.outcome_note = note or entry.outcome_note
    entry.outcome_return_pct = (
        float(return_pct) if return_pct is not None else entry.outcome_return_pct
    )
    entry.outcome_exit_reason = exit_reason or entry.outcome_exit_reason
    if matched_reason is not None:
        entry.outcome_matched_reason = bool(matched_reason)
    entry.outcome_recorded_at = utcnow_naive()
    session.flush()
    return entry


# --------------------------------------------------------------------------- #
# Research questions
# --------------------------------------------------------------------------- #


def add_question(session, question: str, *, ticker: str = "", asked_by: str = HUMAN) -> ResearchQuestion:
    if not (question or "").strip():
        raise ResearchRefused("missing_question", "a research question needs text")
    row = ResearchQuestion(
        question=question.strip(),
        ticker=(ticker or "").upper(),
        status="open",
        asked_by=asked_by or HUMAN,
        created_at=utcnow_naive(),
    )
    session.add(row)
    session.flush()
    return row


def answer_question(session, question_id: int, answer_md: str) -> ResearchQuestion:
    row = (
        session.query(ResearchQuestion)
        .filter(ResearchQuestion.id == int(question_id))
        .first()
    )
    if row is None:
        raise ResearchRefused("unknown_question", f"no research question {question_id}")
    row.answer_md = answer_md or ""
    row.status = "answered"
    row.answered_at = utcnow_naive()
    session.flush()
    return row


def questions(session, *, status: str = "") -> list[ResearchQuestion]:
    query = session.query(ResearchQuestion)
    if status:
        query = query.filter(ResearchQuestion.status == status)
    return query.order_by(ResearchQuestion.id.asc()).all()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SearchHit:
    kind: str
    id: int
    ticker: str
    title: str
    excerpt: str
    content_trust: str = trust.TRUSTED
    unsourced: bool = False


def search(session, query_text: str, *, limit: int = 20) -> list[SearchHit]:
    """Substring search over dossier sections, theses, and journal entries.

    ``LIKE``, not a full-text index: it is engine-neutral across SQLite and
    Postgres, and at this corpus size (hundreds of rows, not millions) the
    index would be a maintenance cost with no measurable benefit. When the
    corpus makes that false, the seam to replace is this one function.
    """
    needle = (query_text or "").strip()
    if not needle:
        raise ResearchRefused("empty_query", "research_search needs a query")
    pattern = f"%{needle}%"
    hits: list[SearchHit] = []

    sections = (
        session.query(DossierSection, Dossier)
        .join(Dossier, DossierSection.dossier_id == Dossier.id)
        .filter(DossierSection.superseded.is_(False))
        .filter(DossierSection.body_md.ilike(pattern))
        .order_by(DossierSection.id.desc())
        .limit(limit)
        .all()
    )
    for section, dossier in sections:
        hits.append(
            SearchHit(
                kind="dossier_section",
                id=section.id,
                ticker=dossier.ticker,
                title=section.section_key,
                excerpt=_excerpt(section.body_md, needle),
                content_trust=trust.content_trust(section.sources),
                unsourced=bool(section.unsourced),
            )
        )

    theses = (
        session.query(Thesis)
        .filter(
            Thesis.title.ilike(pattern)
            | Thesis.claim.ilike(pattern)
            | Thesis.bear_case.ilike(pattern)
        )
        .order_by(Thesis.id.desc())
        .limit(limit)
        .all()
    )
    for thesis in theses:
        hits.append(
            SearchHit(
                kind="thesis",
                id=thesis.id,
                ticker=thesis.ticker,
                title=thesis.title,
                excerpt=_excerpt(f"{thesis.claim} {thesis.bear_case}", needle),
            )
        )

    entries = (
        session.query(DecisionJournalEntry)
        .filter(
            DecisionJournalEntry.note_md.ilike(pattern)
            | DecisionJournalEntry.sizing_rationale.ilike(pattern)
            | DecisionJournalEntry.tickers.ilike(pattern)
        )
        .order_by(DecisionJournalEntry.id.desc())
        .limit(limit)
        .all()
    )
    for entry in entries:
        hits.append(
            SearchHit(
                kind="decision",
                id=entry.id,
                ticker=entry.tickers,
                title=f"{entry.occurred_on.isoformat()} {entry.decision}",
                excerpt=_excerpt(entry.note_md, needle),
            )
        )
    return hits[:limit]


def _excerpt(body: str, needle: str, *, width: int = 160) -> str:
    body = (body or "").replace("\n", " ")
    position = body.lower().find(needle.lower())
    if position < 0:
        return body[:width]
    start = max(0, position - width // 2)
    return ("…" if start else "") + body[start : start + width].strip() + (
        "…" if start + width < len(body) else ""
    )
