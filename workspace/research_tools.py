"""The five Spec M tools on the workspace MCP surface (Spec K §4.2).

    research_get     read              dossier + theses + invalidators for a ticker
    research_search  read              substring search across the workspace
    research_write   research:write    a dossier section, thesis, invalidator, or question
    thesis_review    research:write    a review verdict: hold / weakened / invalidated
    journal_append   research:write    a dated decision, including a decision to pass

Three properties hold across all five:

**Every read returns a ``provenance`` block** (Spec K §4.2): ``as_of_utc``,
per-field source, staleness flags, and a ``data_quality`` tier. Stale content is
returned **with the flag set**, never hidden and never replaced by a guess.

**Untrusted content is marked and delimited.** A section resting on text
someone else wrote is marked ``content_trust: "untrusted"`` and wrapped in
delimiters carrying a per-response random nonce (Spec P §5). Two responses never
share a nonce. This is a parsing aid, not a control: the controls are that no
tool here can place an order and no tool here can produce a statistic.

**Writes refuse rather than improvise.** Every refusal comes back as a readable
``ToolRefused`` carrying the store's error code, so an agent is told which rule
it hit and what to do instead — an exception it cannot read is an exception it
will paper over.

The tools are registered only when ``RESEARCH_WORKSPACE_ENABLED`` is true.
Advertising a tool that answers "disabled" is worse than not advertising it:
``tools/list`` is how a client decides what this workspace can do.
"""

from __future__ import annotations

from datetime import date

import anyio
from mcp.server.fastmcp import Context, FastMCP

from research_workspace import render, store
from research_workspace.errors import ResearchRefused
from research_workspace.trust import UnknownTier

#: Registered when the flag is on. Their scopes live in ``workspace.scopes``.
RESEARCH_TOOLS: tuple[str, ...] = (
    "research_get",
    "research_search",
    "research_write",
    "thesis_review",
    "journal_append",
)

WRITE_KINDS = ("dossier_section", "thesis", "invalidator", "research_question")


def _stale_horizon(settings) -> int:
    return int(getattr(settings, "research_section_stale_days", 90) or 90)


async def _in_session(fn):
    """Run one unit of database work off the event loop, in its own session."""

    def run():
        from database.db import get_session

        with get_session() as session:
            return fn(session)

    return await anyio.to_thread.run_sync(run)


def _parse_date(value: str, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ResearchRefused(
            "invalid_date", f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}"
        ) from None


def register(mcp: FastMCP, settings, *, authorize_call, ToolRefused) -> tuple[str, ...]:
    """Attach the five tools. Returns the names registered."""

    def refuse(exc: Exception):
        """Turn a store refusal into something the agent can read and act on."""
        if isinstance(exc, ResearchRefused):
            return ToolRefused(f"{exc.code}: {exc.message}")
        if isinstance(exc, UnknownTier):
            return ToolRefused(f"unknown_source_tier: {exc}")
        return None

    # -- read ---------------------------------------------------------------

    @mcp.tool(
        name="research_get",
        description=(
            "Everything recorded about a ticker: the dossier's current sections "
            "with their sources, every thesis with its stated probability and "
            "its invalidators, open research questions, and recent decisions. "
            "Read-only. Call it before answering anything about a name — never "
            "re-derive what is already recorded, and never contradict it "
            "silently (Spec M §7)."
        ),
    )
    async def research_get(
        ctx: Context,
        ticker: str,
        include_history: bool = False,
        include_journal: bool = True,
    ) -> dict:
        authorize_call(ctx, "research_get", {"ticker": ticker})
        horizon = _stale_horizon(settings)
        nonce = render.new_nonce()

        def work(session):
            dossier = store.get_dossier(session, ticker)
            sections = [
                render.section_payload(s, horizon_days=horizon, nonce=nonce)
                for s in store.current_sections(session, ticker)
            ]
            theses = [
                render.thesis_payload(session, t)
                for t in store.theses_for(session, ticker)
            ]
            payload = {
                "ticker": (ticker or "").upper(),
                "dossier": (
                    {
                        "dossier_id": dossier.id,
                        "company_name": dossier.company_name,
                        "sector": dossier.sector,
                        "updated_at": dossier.updated_at.isoformat(),
                    }
                    if dossier
                    else None
                ),
                "sections": sections,
                "theses": theses,
                "open_questions": [
                    {"question_id": q.id, "question": q.question, "ticker": q.ticker}
                    for q in store.questions(session, status="open")
                    if not q.ticker or q.ticker == (ticker or "").upper()
                ],
                "due_for_review": [
                    t.id for t in store.due_for_review(session)
                    if t.ticker == (ticker or "").upper()
                ],
            }
            if include_journal:
                payload["recent_decisions"] = [
                    render.journal_payload(e)
                    for e in store.journal_entries(session, ticker=ticker)[-10:]
                ]
            if include_history:
                payload["section_history"] = {
                    key: [
                        render.section_payload(s, horizon_days=horizon, nonce=nonce)
                        for s in store.section_history(session, ticker, key)
                    ]
                    for key in {s["section_key"] for s in sections}
                }
            return payload

        payload = await _in_session(work)
        stale = [s["section_key"] for s in payload["sections"] if s["stale"]]
        untrusted = any(
            s["content_trust"] == "untrusted" for s in payload["sections"]
        )
        payload["provenance"] = render.provenance_block(
            sources={
                "sections": "dossier_sections (append-only revisions)",
                "theses": "theses + thesis_invalidators",
                "recent_decisions": "decision_journal",
            },
            stale_fields=stale,
            data_quality="recorded_research",
            content_trust="untrusted" if untrusted else "trusted",
            nonce=nonce,
            notes=[
                f"sections older than {horizon} days are flagged stale, never hidden",
                "no number here was produced by a model; figures are quoted from "
                "their cited source or from comparables/",
            ],
        )
        return payload

    @mcp.tool(
        name="research_search",
        description=(
            "Substring search across dossier sections, theses, and decision "
            "journal entries. Read-only. Use it to find whether a view has "
            "already been tested and rejected before forming a new one."
        ),
    )
    async def research_search(ctx: Context, query: str, limit: int = 20) -> dict:
        authorize_call(ctx, "research_search", {"query_length": len(query or "")})
        nonce = render.new_nonce()

        def work(session):
            hits = store.search(session, query, limit=limit)
            return [
                {
                    "kind": hit.kind,
                    "id": hit.id,
                    "ticker": hit.ticker,
                    "title": hit.title,
                    "excerpt": (
                        render.wrap_untrusted(hit.excerpt, nonce)
                        if hit.content_trust == "untrusted"
                        else hit.excerpt
                    ),
                    "content_trust": hit.content_trust,
                    "unsourced": hit.unsourced,
                }
                for hit in hits
            ]

        try:
            hits = await _in_session(work)
        except Exception as exc:
            if (refusal := refuse(exc)) is not None:
                raise refusal from exc
            raise

        return {
            "query": query,
            "hits": hits,
            "provenance": render.provenance_block(
                sources={"hits": "dossier_sections, theses, decision_journal"},
                data_quality="recorded_research",
                content_trust=(
                    "untrusted"
                    if any(h["content_trust"] == "untrusted" for h in hits)
                    else "trusted"
                ),
                nonce=nonce,
                notes=["substring match, not ranked relevance"],
            ),
        }

    # -- write --------------------------------------------------------------

    @mcp.tool(
        name="research_write",
        description=(
            "Write to the research workspace. `kind` selects what: "
            "'dossier_section' appends a section revision (never an overwrite); "
            "'thesis' creates or updates a thesis, and status='active' runs the "
            "Spec M §4 gate (bear case, stated probability, resolution_at, and "
            "at least one machine-checkable invalidator); 'invalidator' attaches "
            "an invalidator — set these BEFORE the position, one added after "
            "entry is flagged post_hoc; 'research_question' records something "
            "you could not resolve instead of guessing. A section whose sources "
            "are all unaccountable third-party text (news, vendor data, a "
            "scraped page) is refused unless human_authored is true."
        ),
    )
    async def research_write(
        ctx: Context,
        kind: str,
        ticker: str = "",
        section_key: str = "",
        body_md: str = "",
        sources: list | None = None,
        author: str = "",
        human_authored: bool = False,
        company_name: str = "",
        sector: str = "",
        thesis_id: int = 0,
        title: str = "",
        claim: str = "",
        direction: str = "long",
        horizon_days: int = 0,
        argument: list | None = None,
        bear_case: str = "",
        bear_case_author: str = "",
        probability: float = -1.0,
        resolution_at: str = "",
        resolution_observable: str = "",
        probability_reason: str = "",
        status: str = "",
        description: str = "",
        invalidator_type: str = "",
        params: dict | None = None,
        question: str = "",
    ) -> dict:
        authorize_call(ctx, "research_write", {"kind": kind, "ticker": ticker})
        if kind not in WRITE_KINDS:
            raise ToolRefused(
                f"unknown_kind: kind must be one of {list(WRITE_KINDS)}, got {kind!r}"
            )
        writer = author or "unattributed-model"

        def work(session):
            if kind == "dossier_section":
                section = store.write_section(
                    session,
                    ticker,
                    section_key,
                    body_md,
                    sources=sources or [],
                    author=writer,
                    human_authored=human_authored,
                    company_name=company_name,
                    sector=sector,
                )
                return {
                    "written": "dossier_section",
                    "section": render.section_payload(
                        section, horizon_days=_stale_horizon(settings)
                    ),
                }

            if kind == "thesis":
                thesis = store.get_thesis(session, thesis_id) if thesis_id else None
                if thesis is None:
                    thesis = store.create_thesis(
                        session,
                        ticker=ticker,
                        title=title,
                        claim=claim,
                        direction=direction,
                        horizon_days=horizon_days or None,
                        argument=argument or [],
                        bear_case=bear_case,
                        bear_case_author=bear_case_author,
                        author=writer,
                    )
                else:
                    if claim:
                        thesis.claim = claim
                    if bear_case:
                        thesis.bear_case = bear_case
                        thesis.bear_case_author = bear_case_author or thesis.bear_case_author
                    if argument is not None:
                        store.set_argument(session, thesis, argument)
                    session.flush()
                if probability >= 0.0:
                    store.set_probability(
                        session,
                        thesis,
                        probability,
                        resolution_at=_parse_date(resolution_at, "resolution_at"),
                        observable=resolution_observable,
                        reason=probability_reason,
                        author=writer,
                    )
                elif resolution_at:
                    thesis.resolution_at = _parse_date(resolution_at, "resolution_at")
                    session.flush()
                if status == "active":
                    store.activate(session, thesis, author=writer)
                elif status and status != thesis.status:
                    raise ResearchRefused(
                        "unsupported_status_change",
                        f"research_write moves a thesis to 'active' only; "
                        f"{status!r} is a review verdict — use thesis_review.",
                    )
                return {"written": "thesis", "thesis": render.thesis_payload(session, thesis)}

            if kind == "invalidator":
                thesis = store.get_thesis(session, thesis_id)
                if thesis is None:
                    raise ResearchRefused(
                        "unknown_thesis", f"no thesis with id {thesis_id}"
                    )
                row = store.add_invalidator(
                    session,
                    thesis,
                    description=description,
                    type=invalidator_type,
                    params=params or {},
                    author=writer,
                )
                return {
                    "written": "invalidator",
                    "invalidator": render.invalidator_payload(row),
                    "post_hoc_warning": (
                        "set after the position opened: kept and watched, and "
                        "excluded from the honesty metrics (Spec M §4 rule 3)"
                        if row.post_hoc
                        else ""
                    ),
                    "activation_blockers": store.activation_blockers(session, thesis),
                }

            row = store.add_question(session, question, ticker=ticker, asked_by=writer)
            return {
                "written": "research_question",
                "question": {"question_id": row.id, "question": row.question},
            }

        try:
            return await _in_session(work)
        except Exception as exc:
            if (refusal := refuse(exc)) is not None:
                raise refusal from exc
            raise

    @mcp.tool(
        name="thesis_review",
        description=(
            "Record a review verdict on a thesis: 'hold', 'weakened', or "
            "'invalidated'. This is a statement about the thesis only. It never "
            "closes a position, creates an order, or creates a proposal — that "
            "decision is the owner's."
        ),
    )
    async def thesis_review(
        ctx: Context,
        thesis_id: int,
        verdict: str,
        note: str = "",
        author: str = "",
        next_review_at: str = "",
        outcome: str = "",
    ) -> dict:
        authorize_call(ctx, "thesis_review", {"thesis_id": thesis_id, "verdict": verdict})

        def work(session):
            thesis = store.get_thesis(session, thesis_id)
            if thesis is None:
                raise ResearchRefused("unknown_thesis", f"no thesis with id {thesis_id}")
            store.review(
                session,
                thesis,
                verdict=verdict,
                note=note,
                author=author or "unattributed-model",
                next_review_at=_parse_date(next_review_at, "next_review_at"),
                outcome=outcome or None,
            )
            return {
                "thesis": render.thesis_payload(session, thesis),
                "position_effect": (
                    "none: a review changes the thesis, never a position "
                    "(Spec M §3)"
                ),
            }

        try:
            return await _in_session(work)
        except Exception as exc:
            if (refusal := refuse(exc)) is not None:
                raise refusal from exc
            raise

    @mcp.tool(
        name="journal_append",
        description=(
            "Append a dated decision to the journal — opened, added, trimmed, "
            "closed, or passed. Record the decision to pass too: a track record "
            "without the passes is not a track record. budget='evidenced' "
            "requires a cited cohort answer that is depth='full' with "
            "status='ok'; anything else draws from the discretionary budget and "
            "is recorded as such (Spec L §6.6)."
        ),
    )
    async def journal_append(
        ctx: Context,
        decision: str,
        tickers: list,
        note_md: str = "",
        budget: str = "discretionary",
        thesis_id: int = 0,
        cohort_answer_id: str = "",
        sizing_rationale: str = "",
        expected_holding_days: int = 0,
        occurred_on: str = "",
        author: str = "",
    ) -> dict:
        authorize_call(
            ctx,
            "journal_append",
            {"decision": decision, "tickers": tickers, "budget": budget},
        )

        def work(session):
            thesis = store.get_thesis(session, thesis_id) if thesis_id else None
            if thesis_id and thesis is None:
                raise ResearchRefused("unknown_thesis", f"no thesis with id {thesis_id}")
            entry = store.journal_append(
                session,
                decision=decision,
                tickers=tickers,
                occurred_on=_parse_date(occurred_on, "occurred_on"),
                thesis=thesis,
                cohort_answer_id=cohort_answer_id,
                budget=budget,
                sizing_rationale=sizing_rationale,
                expected_holding_days=expected_holding_days or None,
                note_md=note_md,
                author=author or "unattributed-model",
            )
            return {"entry": render.journal_payload(entry)}

        try:
            return await _in_session(work)
        except Exception as exc:
            if (refusal := refuse(exc)) is not None:
                raise refusal from exc
            raise

    return RESEARCH_TOOLS
