"""Reading the mirror with no database and no network (Spec K §3.3).

This is the fallback the two-store design exists for: if the workspace API is
down, an agent session that has cloned the repo still has every thesis, every
invalidator, and every past decision — just not live prices. That degradation
is designed, and this module is what makes it testable rather than aspirational.

Nothing here imports ``database``, ``sqlalchemy``, or anything that opens a
socket. If it ever does, ``test_offline_read`` stops meaning anything.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPANIES_DIR = "companies"
THESES_DIR = "theses"
JOURNAL_DIR = "journal"
QUESTIONS_FILE = "questions.md"

FRONT_MATTER_DELIMITER = "---"


class MirrorUnreadable(ValueError):
    """A file under ``research/`` that the mirror did not write."""


def read_front_matter(path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith(FRONT_MATTER_DELIMITER):
        raise MirrorUnreadable(f"{path} has no front matter")
    head = text.split(f"\n{FRONT_MATTER_DELIMITER}\n", 1)[0]
    loaded = yaml.safe_load(head[len(FRONT_MATTER_DELIMITER):]) or {}
    if not isinstance(loaded, dict):
        raise MirrorUnreadable(f"{path} front matter is not a mapping")
    return loaded


def read_theses(root) -> list[dict]:
    directory = Path(root) / THESES_DIR
    if not directory.exists():
        return []
    return [read_front_matter(path) for path in sorted(directory.glob("*.md"))]


def theses_for(root, ticker: str, *, statuses=None) -> list[dict]:
    ticker = (ticker or "").upper()
    found = [t for t in read_theses(root) if str(t.get("ticker", "")).upper() == ticker]
    if statuses:
        found = [t for t in found if t.get("status") in set(statuses)]
    return found


def current_view(root, ticker: str) -> dict:
    """"What is my view on X, and what would change it" — from files alone.

    Returns the live theses (``active`` or ``weakened``), each with its stated
    probability, its resolution date, and its invalidators. An empty
    ``theses`` list is the honest answer when nothing is recorded, not an error.
    """
    live = theses_for(root, ticker, statuses=("active", "weakened"))
    return {
        "ticker": (ticker or "").upper(),
        "source": "repository mirror (offline; the workspace API was not used)",
        "theses": [
            {
                "thesis_id": t.get("thesis_id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "probability": t.get("probability"),
                "resolution_at": t.get("resolution_at"),
                "resolution_observable": t.get("resolution_observable"),
                "invalidators": [
                    {
                        "type": i.get("type"),
                        "description": i.get("description"),
                        "machine_checkable": i.get("machine_checkable"),
                        "status": i.get("status"),
                        "post_hoc": i.get("post_hoc"),
                    }
                    for i in (t.get("invalidators") or [])
                ],
            }
            for t in live
        ],
        "note": (
            "Prices are not available offline. Everything here was written by "
            "the workspace and committed to the repo."
        ),
    }


def what_would_change_my_mind(root, ticker: str) -> list[str]:
    return [
        f"{i['type']}: {i['description']}"
        for thesis in theses_for(root, ticker, statuses=("active", "weakened"))
        for i in (thesis.get("invalidators") or [])
    ]


def read_dossier(root, ticker: str) -> dict | None:
    path = Path(root) / COMPANIES_DIR / f"{(ticker or '').upper()}.md"
    return read_front_matter(path) if path.exists() else None


def read_questions(root) -> dict | None:
    path = Path(root) / QUESTIONS_FILE
    return read_front_matter(path) if path.exists() else None
