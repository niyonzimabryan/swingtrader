"""Resolving a cited cohort answer (Spec N §8, Spec M `journal_append`).

Spec N §8: *"`full` is **mandatory** for any answer referenced by
`journal_append`, `research_write`, or a Spec Q promotion; a `quick` answer
cannot be cited, and the type system enforces it."* Phase 3b enforced it on the
in-memory object (`report.assert_citable`). This is the stored half: a citation
is an identifier a journal entry can carry, and resolving it hands back the
answer that was actually produced, from the row that was actually stored.

The identifier is `cohort:<cohort_answers.id>`. It is deliberately not the
setup hash: two questions asked on different `as_of` dates against different
price snapshots are different answers, and a citation has to point at one of
them, not at the family of them.

**Phase 2's seam.** Spec M's research workspace exposes
`research_workspace/citations.py`, which had not merged when this landed. When
it does, it should call :func:`resolve` — that is the whole integration, and
`workspace/tools.py` performs the optional binding so that nothing in
`comparables/` has to import a package that may not exist. Until then this
module *is* the seam and `journal_append` can call it directly.

Nothing here re-derives a number. It reads the stored JSON of an answer the
engine computed and refuses the ones §8 says may not be cited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from comparables import registry

CITATION_PREFIX = "cohort"


class CitationError(ValueError):
    """A citation that cannot be resolved to a stored, citable answer."""


class NotCitable(CitationError):
    """The answer exists and may not be cited (Spec N §8)."""


def citation_id(answer_row_id: int) -> str:
    """The identifier a journal entry, write-up or promotion carries."""
    return f"{CITATION_PREFIX}:{int(answer_row_id)}"


def parse_citation(answer_id: str | int) -> int:
    """`cohort:41` or `41` -> `41`. Anything else raises."""
    if isinstance(answer_id, int):
        return answer_id
    text = str(answer_id).strip()
    if text.startswith(f"{CITATION_PREFIX}:"):
        text = text.split(":", 1)[1]
    try:
        return int(text)
    except ValueError as exc:
        raise CitationError(
            f"{answer_id!r} is not a cohort citation; the form is "
            f"'{CITATION_PREFIX}:<id>' as returned by compare_setups"
        ) from exc


@dataclass(frozen=True)
class ResolvedCitation:
    """A stored answer, resolved, with everything a citer must render.

    `provenance_mix` and `archival_block` travel with it because §8 forbids
    pooling the archival block into the headline: a consumer that renders the
    citation has to be able to render both, and it cannot do that from a
    payload that only carries one.
    """

    citation_id: str
    answer_id: int
    query_id: int | None
    setup_hash: str
    family_slug: str
    as_of: date
    depth: str
    status: str
    evidence_tier: str
    answer: dict
    archival_block: dict | None
    provenance_mix: dict
    #: The security this answer was asked *about* (Spec L §6.6), or `""` when
    #: the question named none. A `SetupSpec` is a pattern, so this is a fact
    #: about the query and is recorded at query time or not at all.
    subject_ticker: str = ""
    #: Whether that name met the setup's conditions at its most recent candidate
    #: on or before `as_of`. `None` means no subject was named — which is not
    #: the same statement as `False`.
    subject_qualifies: bool | None = None
    subject_reason: str = ""
    subject_event_date: date | None = None

    @property
    def citable(self) -> bool:
        """Whether §8 lets this answer be cited **at all**.

        Deliberately not a same-ticker check. `full`/not-`insufficient` is what
        Spec N §8 governs, and it is the whole rule for a journal note or a
        write-up quoting the cohort. Spec L §6.6's extra condition — that the
        answer is for *this* proposal's ticker and that the ticker qualified —
        is a sizing rule, lives in `portfolio/evidence.py`, and fails toward the
        discretionary budget rather than toward a refusal.
        """
        return self.depth == "full" and self.status != "insufficient"

    @property
    def citable_for(self) -> str:
        """The one ticker this answer may back an evidenced proposal in, or `""`."""
        return self.subject_ticker if self.subject_qualifies else ""


def resolve(session, answer_id: str | int, *, require_citable: bool = True) -> ResolvedCitation:
    """Resolve a citation to the stored answer behind it.

    `require_citable=True` is the default and the point: a `quick` answer and
    an `insufficient` one both refuse, with a reason a reader can act on. Pass
    `False` only to *display* an answer — never to quote a number from one.
    """
    row_id = parse_citation(answer_id)
    row = registry.answer(session, row_id)
    if row is None:
        raise CitationError(
            f"no stored cohort answer with id {row_id}; a citation resolves "
            f"against `cohort_answers`, and an answer that was never stored "
            f"was never computed"
        )

    resolved = ResolvedCitation(
        citation_id=citation_id(row.id),
        answer_id=row.id,
        query_id=row.query_id,
        setup_hash=row.setup_hash,
        family_slug=row.family_slug,
        as_of=row.as_of_date,
        depth=row.depth,
        status=row.status,
        evidence_tier=row.evidence_tier,
        answer=row.answer,
        archival_block=row.archival_block,
        provenance_mix=row.provenance_mix,
        subject_ticker=(row.subject_ticker or "").strip().upper(),
        subject_qualifies=row.subject_qualifies,
        subject_reason=row.subject_reason or "",
        subject_event_date=row.subject_event_date,
    )
    if require_citable and not resolved.citable:
        if resolved.status == "insufficient":
            reason = (
                resolved.answer.get("refusal_reason")
                or "the cohort was below the floor and carries no statistic"
            )
            raise NotCitable(
                f"{resolved.citation_id} is an `insufficient` answer and carries "
                f"no statistic to cite: {reason}. Render it as a refusal, never "
                f"as a hedge (Spec N §8)."
            )
        raise NotCitable(
            f"{resolved.citation_id} has depth={resolved.depth!r}. `full` is "
            f"mandatory for any answer referenced by journal_append, "
            f"research_write or a Spec Q promotion (Spec N §8). Re-run the same "
            f"question at depth='full' — the setup hash and the as_of are "
            f"unchanged, so the cohort is the same one."
        )
    return resolved


def citation_payload(resolved: ResolvedCitation) -> dict:
    """The flat dict a journal entry stores beside its own prose.

    Every number in it came from `comparables/` and from a stored query; the
    citer copies, it does not compute (Spec N §9).
    """
    return {
        "citation_id": resolved.citation_id,
        "query_id": resolved.query_id,
        "setup_hash": resolved.setup_hash,
        "family_slug": resolved.family_slug,
        "as_of": resolved.as_of.isoformat(),
        "depth": resolved.depth,
        "status": resolved.status,
        "evidence_tier": resolved.evidence_tier,
        "provenance_mix": resolved.provenance_mix,
        "has_archival_block": resolved.archival_block is not None,
        "subject_ticker": resolved.subject_ticker or None,
        "subject_qualifies": resolved.subject_qualifies,
        "subject_reason": resolved.subject_reason or None,
        "subject_event_date": (
            resolved.subject_event_date.isoformat()
            if resolved.subject_event_date else None
        ),
    }


def render_json(resolved: ResolvedCitation) -> str:
    return json.dumps(citation_payload(resolved), sort_keys=True, separators=(",", ":"))


#: What Phase 2 binds. Named so the binding is a one-line assignment rather
#: than an import cycle: `research_workspace.citations.register("cohort", RESOLVER)`.
RESOLVER: Any = resolve
