"""The Markdown mirror: Postgres → repo, deliberately partial (Spec M §5).

Why both stores: Postgres gives querying, staleness, and joins; git gives
durability, diffability, review, and an offline fallback that works when the
API is down (Spec K §3.3). Neither alone is sufficient, so the mirror is not a
backup — it is the second half of the design.

**The export is filtered on provenance and cannot be lossless.** Spec K §3.3
forbids news-derived content and vendor series in a public repo: Tiingo's
internal-use licence and Alpaca's bar on publishing "any derived products or
services" are the verified clauses. So a section whose sources include a
withheld tier is written as::

    <!-- withheld: news-derived, see dossier_sections/41 -->

carrying the Postgres id, and ``--import`` reads that marker as **keep the
database copy** rather than as an empty body. The round-trip guarantee applies
to permitted content — human and model prose, sourced claims, cohort ids,
summary figures — and ``test_mirror_withholds_by_provenance`` is what stops the
filter from quietly regressing into "export everything".

**What ``--import`` will and will not accept.** It applies prose: dossier
section bodies, and a thesis's claim and bear case — each through the same
validation the tools use, so an edited section becomes an append-only revision
and an unknown source tier is refused. ``questions.md`` is export-only; an
answer is a write, and writes go through the tools. It **refuses** a hand-edited ``status``, ``probability``,
``resolution_at`` or invalidator list, because those are exactly the fields the
Spec M §4 gate protects and a Markdown file cannot carry the gate with them.
The refusal names the tool to use instead. A file is a good place to write
prose and a bad place to change what a thesis is allowed to be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from database.models import Dossier, Thesis
from research_workspace import store, trust
from research_workspace.errors import ResearchRefused
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("research_mirror")

MIRROR_VERSION = 1

BANNER = (
    "<!-- Generated from Postgres by scripts/sync_research_mirror.py. "
    "Do not hand-edit: write through research_write / thesis_review. "
    "Prose edited here is read back only by --import. -->"
)

WITHHELD_TEMPLATE = "<!-- withheld: {reason}, see dossier_sections/{section_id} -->"
WITHHELD_RE = re.compile(
    r"<!--\s*withheld:\s*(?P<reason>.+?),\s*see\s+(?P<table>[a-z_]+)/(?P<row_id>\d+)\s*-->"
)
SECTION_MARKER = "<!-- section: {section_id} -->"
SECTION_RE = re.compile(r"<!--\s*section:\s*(?P<section_id>\d+)\s*-->")

FRONT_MATTER_DELIMITER = "---"

COMPANIES_DIR = "companies"
THESES_DIR = "theses"
JOURNAL_DIR = "journal"
QUESTIONS_FILE = "questions.md"

#: Thesis front-matter fields the import path will not accept an edit to, with
#: the tool that owns each.
PROTECTED_THESIS_FIELDS = {
    "status": "thesis_review",
    "probability": "research_write",
    "original_probability": "research_write",
    "resolution_at": "research_write",
    "invalidators": "research_write",
    "machine_checkable_invalidators": "research_write",
}


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #


def render_front_matter(data: dict) -> str:
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"{FRONT_MATTER_DELIMITER}\n{body}{FRONT_MATTER_DELIMITER}\n"


def split_front_matter(text: str) -> tuple[dict, str]:
    """``(front_matter, body)``. A file without front matter is not ours."""
    if not text.startswith(FRONT_MATTER_DELIMITER):
        raise ResearchRefused(
            "not_a_mirror_file",
            "this file has no YAML front matter; the mirror only reads files it "
            "wrote (Spec M §5)",
        )
    parts = text.split(f"\n{FRONT_MATTER_DELIMITER}\n", 1)
    if len(parts) != 2:
        raise ResearchRefused("not_a_mirror_file", "unterminated YAML front matter")
    loaded = yaml.safe_load(parts[0][len(FRONT_MATTER_DELIMITER):]) or {}
    if not isinstance(loaded, dict):
        raise ResearchRefused("not_a_mirror_file", "front matter is not a mapping")
    return loaded, parts[1]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


@dataclass
class ExportReport:
    files: list[str] = field(default_factory=list)
    withheld_sections: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": sorted(self.files),
            "withheld_sections": sorted(self.withheld_sections),
        }


def company_path(root: Path, ticker: str) -> Path:
    return Path(root) / COMPANIES_DIR / f"{ticker.upper()}.md"


def thesis_path(root: Path, thesis: Thesis) -> Path:
    created = thesis.created_at or utcnow_naive()
    slug = thesis.slug or store.slugify(thesis.title)
    return Path(root) / THESES_DIR / f"{created:%Y-%m}-{thesis.ticker.lower()}-{slug}.md"


def _sources_block(sources) -> str:
    if not sources:
        return "_No source recorded. Treat this as recall, not evidence._\n"
    lines = ["**Sources**", ""]
    for source in sources:
        title = source.get("title") or source["url"]
        as_of = f" ({source['as_of']})" if source.get("as_of") else ""
        lines.append(f"- [{title}]({source['url']}) — `{source['tier']}`{as_of}")
    return "\n".join(lines) + "\n"


def export_dossier(session, dossier: Dossier, root: Path, *, now=None) -> tuple[Path, list[int]]:
    now = now or utcnow_naive()
    sections = store.current_sections(session, dossier.ticker)
    withheld: list[int] = []

    meta_sections = []
    body_parts = [f"# {dossier.ticker} — dossier", ""]
    if dossier.company_name or dossier.sector:
        body_parts.append(
            f"_{dossier.company_name or dossier.ticker} · {dossier.sector or 'sector unrecorded'}_"
        )
        body_parts.append("")

    for section in sections:
        sources = section.sources
        is_withheld = trust.is_mirror_withheld(sources)
        entry = {
            "section_id": section.id,
            "section_key": section.section_key,
            "updated_at": _iso(section.created_at),
            "author": section.author,
            "author_kind": section.author_kind,
            "unsourced": bool(section.unsourced),
            "human_authored": bool(section.human_authored),
            "content_trust": trust.content_trust(sources),
            "withheld": is_withheld,
            "supersedes_id": section.supersedes_id,
        }
        if is_withheld:
            entry["withheld_reason"] = trust.withheld_reason(sources)
            withheld.append(section.id)
        else:
            # Sources travel in the front matter so the round trip is lossless
            # for permitted content; the rendered list below is for the human.
            entry["sources"] = sources
        meta_sections.append(entry)

        body_parts.append(f"## {section.section_key}")
        body_parts.append(SECTION_MARKER.format(section_id=section.id))
        body_parts.append("")
        if is_withheld:
            body_parts.append(
                WITHHELD_TEMPLATE.format(
                    reason=trust.withheld_reason(sources), section_id=section.id
                )
            )
            body_parts.append("")
            body_parts.append(
                "_Withheld from the repo by the provenance filter (Spec K §3.3). "
                "The content is in Postgres._"
            )
        else:
            if section.unsourced:
                body_parts.append("> ⚠️ **unsourced** — no source recorded for this section.")
                body_parts.append("")
            if entry["content_trust"] == trust.UNTRUSTED:
                body_parts.append(
                    "> ℹ️ rests on sources written by someone else: data, never instruction."
                )
                body_parts.append("")
            body_parts.append(section.body_md.rstrip())
            body_parts.append("")
            body_parts.append(_sources_block(sources))
        body_parts.append("")

    front = {
        "mirror_version": MIRROR_VERSION,
        "kind": "dossier",
        "dossier_id": dossier.id,
        "ticker": dossier.ticker,
        "company_name": dossier.company_name,
        "sector": dossier.sector,
        "generated_at": _iso(now),
        "sections": meta_sections,
    }
    path = _write(
        company_path(root, dossier.ticker),
        render_front_matter(front) + BANNER + "\n\n" + "\n".join(body_parts).rstrip() + "\n",
    )
    return path, withheld


def export_thesis(session, thesis: Thesis, root: Path, *, now=None) -> Path:
    now = now or utcnow_naive()
    rows = store.invalidators_for(session, thesis.id)
    front = {
        "mirror_version": MIRROR_VERSION,
        "kind": "thesis",
        "thesis_id": thesis.id,
        "ticker": thesis.ticker,
        "title": thesis.title,
        "status": thesis.status,
        "direction": thesis.direction,
        "probability": thesis.probability,
        "original_probability": thesis.original_probability,
        "resolution_at": _iso(thesis.resolution_at),
        "resolution_observable": thesis.resolution_observable,
        "next_review_at": _iso(thesis.next_review_at),
        "machine_checkable_invalidators": thesis.machine_checkable_invalidators,
        "author": thesis.author,
        "author_kind": thesis.author_kind,
        "bear_case_author": thesis.bear_case_author,
        "linked_position_ref": thesis.linked_position_ref,
        "outcome": thesis.outcome,
        "created_at": _iso(thesis.created_at),
        "generated_at": _iso(now),
        "invalidators": [
            {
                "invalidator_id": row.id,
                "type": row.type,
                "description": row.description,
                "params": row.params,
                "machine_checkable": bool(row.machine_checkable),
                "post_hoc": bool(row.post_hoc),
                "status": row.status,
                "triggered_at": _iso(row.triggered_at),
                "triggered_reason": row.triggered_reason,
            }
            for row in rows
        ],
        "argument": thesis.argument,
        "probability_history": thesis.probability_history,
    }

    lines = [
        f"# {thesis.ticker} — {thesis.title}",
        "",
        f"**Status:** `{thesis.status}` · **Stated probability:** "
        f"{thesis.probability if thesis.probability is not None else '—'} by "
        f"{_iso(thesis.resolution_at) or '—'}",
        "",
        "## Claim",
        "",
        thesis.claim.rstrip() or "_none recorded_",
        "",
        "## Argument",
        "",
    ]
    if thesis.argument:
        for item in thesis.argument:
            if isinstance(item, dict):
                lines.append(f"{item.get('n', '-')}. {item.get('claim', '')}")
                for evidence in item.get("evidence", []) or []:
                    lines.append(f"   - evidence: {evidence}")
            else:
                lines.append(f"- {item}")
    else:
        lines.append("_none recorded_")
    lines += [
        "",
        "## Bear case",
        "",
        (thesis.bear_case.rstrip() or "_none recorded_"),
        "",
        f"_Attributed to: {thesis.bear_case_author or 'unattributed'}_",
        "",
        "## What would change my mind",
        "",
    ]
    if rows:
        for row in rows:
            flags = []
            if row.post_hoc:
                flags.append("post-hoc")
            if not row.machine_checkable:
                flags.append("human review")
            if row.status == "triggered":
                flags.append("TRIGGERED")
            suffix = f" _({', '.join(flags)})_" if flags else ""
            lines.append(f"- **{row.type}** — {row.description}{suffix}")
            if row.params:
                lines.append(f"  - `{row.params}`")
    else:
        lines.append("_none recorded — this thesis cannot leave draft (Spec M §4)_")
    lines.append("")

    return _write(
        thesis_path(root, thesis),
        render_front_matter(front) + BANNER + "\n\n" + "\n".join(lines).rstrip() + "\n",
    )


def export_journal(session, root: Path, *, now=None) -> list[Path]:
    now = now or utcnow_naive()
    entries = store.journal_entries(session)
    by_month: dict[str, list] = {}
    for entry in entries:
        by_month.setdefault(entry.occurred_on.strftime("%Y-%m"), []).append(entry)

    written = []
    for month, rows in sorted(by_month.items()):
        front = {
            "mirror_version": MIRROR_VERSION,
            "kind": "journal",
            "month": month,
            "generated_at": _iso(now),
            "entries": [
                {
                    "entry_id": row.id,
                    "occurred_on": _iso(row.occurred_on),
                    "tickers": row.tickers,
                    "decision": row.decision,
                    "budget": row.budget,
                    "thesis_id": row.thesis_id,
                    "thesis_hash": row.thesis_hash,
                    "cohort_answer_id": row.cohort_answer_id,
                    "cohort_evidence_hash": row.cohort_evidence_hash,
                    "expected_holding_days": row.expected_holding_days,
                    "author": row.author,
                    "outcome_exit_reason": row.outcome_exit_reason,
                    "outcome_matched_reason": row.outcome_matched_reason,
                }
                for row in rows
            ],
        }
        lines = [f"# Decision journal — {month}", ""]
        for row in rows:
            lines += [
                f"## {row.occurred_on.isoformat()} · {row.decision} · {row.tickers}",
                "",
                f"- budget: `{row.budget}`"
                + (
                    f" · cohort answer: `{row.cohort_answer_id}`"
                    if row.cohort_answer_id
                    else " · no cohort answer cited"
                ),
                f"- thesis: {row.thesis_id or '—'} (hash `{row.thesis_hash[:12] or '—'}`)",
                f"- expected hold: {row.expected_holding_days or '—'} days",
                "",
                row.note_md.rstrip() or "_no note_",
                "",
            ]
            if row.sizing_rationale:
                lines += [f"**Sizing:** {row.sizing_rationale}", ""]
            if row.outcome_recorded_at:
                lines += [
                    f"**Outcome:** {row.outcome_note or '—'} "
                    f"(exit reason `{row.outcome_exit_reason or '—'}`, "
                    f"matched the journal: {row.outcome_matched_reason})",
                    "",
                ]
        written.append(
            _write(
                Path(root) / JOURNAL_DIR / f"{month}.md",
                render_front_matter(front)
                + BANNER
                + "\n\n"
                + "\n".join(lines).rstrip()
                + "\n",
            )
        )
    return written


def export_questions(session, root: Path, *, now=None) -> Path:
    now = now or utcnow_naive()
    rows = store.questions(session)
    front = {
        "mirror_version": MIRROR_VERSION,
        "kind": "questions",
        "generated_at": _iso(now),
        "questions": [
            {
                "question_id": row.id,
                "ticker": row.ticker,
                "status": row.status,
                "asked_by": row.asked_by,
                "created_at": _iso(row.created_at),
                "answered_at": _iso(row.answered_at),
            }
            for row in rows
        ],
    }
    lines = ["# Open research questions", ""]
    open_rows = [r for r in rows if r.status == "open"]
    if not open_rows:
        lines.append("_None open._")
    for row in open_rows:
        lines.append(f"- **#{row.id}** {f'({row.ticker}) ' if row.ticker else ''}{row.question}")
    answered = [r for r in rows if r.status != "open"]
    if answered:
        lines += ["", "## Answered", ""]
        for row in answered:
            lines += [
                f"### #{row.id} {row.question}",
                "",
                row.answer_md.rstrip() or "_no answer recorded_",
                "",
            ]
    return _write(
        Path(root) / QUESTIONS_FILE,
        render_front_matter(front) + BANNER + "\n\n" + "\n".join(lines).rstrip() + "\n",
    )


def export_all(session, root, *, now=None, settings=None) -> ExportReport:
    """Write every dossier, thesis, journal month and the question list."""
    root = Path(root)
    now = now or utcnow_naive()
    report = ExportReport()

    for dossier in session.query(Dossier).order_by(Dossier.ticker.asc()).all():
        path, withheld = export_dossier(session, dossier, root, now=now)
        report.files.append(str(path))
        report.withheld_sections.extend(withheld)
    for thesis in session.query(Thesis).order_by(Thesis.id.asc()).all():
        report.files.append(str(export_thesis(session, thesis, root, now=now)))
    for path in export_journal(session, root, now=now):
        report.files.append(str(path))
    report.files.append(str(export_questions(session, root, now=now)))

    log.info("research_mirror_exported", **report.as_dict())
    return report


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


@dataclass
class ImportReport:
    revisions: list[int] = field(default_factory=list)
    unchanged: int = 0
    withheld_preserved: list[int] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    theses_updated: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "revisions": self.revisions,
            "unchanged": self.unchanged,
            "withheld_preserved": self.withheld_preserved,
            "refusals": self.refusals,
            "theses_updated": self.theses_updated,
        }


def _section_bodies(body: str) -> dict[int, str]:
    """Body text per ``<!-- section: id -->`` marker, withheld ones excluded."""
    out: dict[int, str] = {}
    chunks = SECTION_RE.split(body)
    # split() yields [prefix, id, chunk, id, chunk, ...]
    for index in range(1, len(chunks) - 1, 2):
        section_id = int(chunks[index])
        chunk = chunks[index + 1]
        if WITHHELD_RE.search(chunk):
            continue
        out[section_id] = _strip_rendering(chunk)
    return out


def _strip_rendering(chunk: str) -> str:
    """Drop the parts the export added for the human: warnings and the source list."""
    text = chunk.split("**Sources**")[0]
    text = text.split("_No source recorded.")[0]
    lines = [
        line
        for line in text.splitlines()
        if not line.startswith("> ⚠️")
        and not line.startswith("> ℹ️")
        and not line.startswith("## ")
    ]
    return "\n".join(lines).strip()


def import_dossier_file(session, path: Path) -> ImportReport:
    report = ImportReport()
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    if front.get("kind") != "dossier":
        raise ResearchRefused("wrong_file_kind", f"{path.name} is not a dossier file")

    ticker = str(front.get("ticker") or "").upper()
    bodies = _section_bodies(body)
    for meta in front.get("sections") or []:
        section_id = int(meta["section_id"])
        if meta.get("withheld"):
            # "Keep the database copy" — the marker is not an empty body.
            report.withheld_preserved.append(section_id)
            continue
        if section_id not in bodies:
            report.refusals.append(
                f"section {section_id} ({meta.get('section_key')}) is in the front "
                f"matter but has no body in {path.name}; the database copy is kept"
            )
            continue
        current = store.current_section(session, _dossier_id(session, ticker), meta["section_key"])
        edited = bodies[section_id]
        if current is not None and current.id == section_id and current.body_md.strip() == edited:
            report.unchanged += 1
            continue
        if current is not None and current.id != section_id:
            report.refusals.append(
                f"section {meta.get('section_key')} was revised in the database "
                f"after this file was generated (file has {section_id}, database "
                f"has {current.id}); re-export before importing"
            )
            continue
        revision = store.write_section(
            session,
            ticker,
            meta["section_key"],
            edited,
            sources=meta.get("sources") or [],
            author=meta.get("author") or store.HUMAN,
            human_authored=bool(meta.get("human_authored")),
        )
        report.revisions.append(revision.id)
    return report


def _dossier_id(session, ticker: str) -> int:
    dossier = store.get_dossier(session, ticker)
    if dossier is None:
        raise ResearchRefused("unknown_dossier", f"no dossier for {ticker}")
    return dossier.id


def import_thesis_file(session, path: Path) -> ImportReport:
    """Apply prose edits to a thesis; refuse edits to the protected fields."""
    report = ImportReport()
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    if front.get("kind") != "thesis":
        raise ResearchRefused("wrong_file_kind", f"{path.name} is not a thesis file")

    thesis = store.get_thesis(session, int(front["thesis_id"]))
    if thesis is None:
        raise ResearchRefused("unknown_thesis", f"no thesis {front['thesis_id']}")

    for field_name, tool in PROTECTED_THESIS_FIELDS.items():
        if field_name not in front:
            continue
        if _protected_matches(session, thesis, field_name, front[field_name]):
            continue
        report.refusals.append(
            f"{path.name}: {field_name!r} was hand-edited. That field is the "
            f"Spec M §4 gate, not prose — change it through {tool}. The database "
            f"value is kept."
        )

    claim = _section_text(body, "## Claim")
    bear = _section_text(body, "## Bear case")
    changed = False
    if claim is not None and claim != (thesis.claim or "").strip():
        thesis.claim = claim
        changed = True
    if bear is not None and bear != (thesis.bear_case or "").strip():
        thesis.bear_case = bear
        changed = True
    if changed:
        thesis.updated_at = utcnow_naive()
        session.flush()
        report.theses_updated.append(thesis.id)
    return report


def _protected_matches(session, thesis, field_name, value) -> bool:
    if field_name == "invalidators":
        rows = store.invalidators_for(session, thesis.id)
        stored = [
            {"invalidator_id": r.id, "type": r.type, "description": r.description}
            for r in rows
        ]
        given = [
            {
                "invalidator_id": item.get("invalidator_id"),
                "type": item.get("type"),
                "description": item.get("description"),
            }
            for item in (value or [])
        ]
        return stored == given
    current = getattr(thesis, field_name, None)
    if field_name == "resolution_at":
        current = _iso(current)
    return current == value


def _section_text(body: str, heading: str) -> str | None:
    if heading not in body:
        return None
    after = body.split(heading, 1)[1]
    chunk = after.split("\n## ", 1)[0]
    text = chunk.strip()
    if text.startswith("_none recorded_"):
        return None
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("_Attributed to:")
    ).strip()


def import_all(session, root) -> ImportReport:
    """Read the mirror back. A marker means "keep the database copy"."""
    root = Path(root)
    combined = ImportReport()

    for path in sorted((root / COMPANIES_DIR).glob("*.md")) if (root / COMPANIES_DIR).exists() else []:
        part = import_dossier_file(session, path)
        combined.revisions += part.revisions
        combined.unchanged += part.unchanged
        combined.withheld_preserved += part.withheld_preserved
        combined.refusals += part.refusals
    for path in sorted((root / THESES_DIR).glob("*.md")) if (root / THESES_DIR).exists() else []:
        part = import_thesis_file(session, path)
        combined.refusals += part.refusals
        combined.theses_updated += part.theses_updated

    log.info("research_mirror_imported", **combined.as_dict())
    return combined
