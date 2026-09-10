#!/usr/bin/env python3
"""Sync the research workspace to the repo's Markdown mirror (Spec M §5).

    python -m scripts.sync_research_mirror                 # Postgres -> repo
    python -m scripts.sync_research_mirror --check         # would anything change?
    python -m scripts.sync_research_mirror --import        # repo -> Postgres

**The direction is Postgres → repo.** ``--import`` exists for the one case
where Bryan edits Markdown directly, and it round-trips through the same
validation the tools use: an edited section becomes an append-only revision, an
unknown source tier is refused, and a hand-edited thesis ``status`` or
``probability`` is refused with the name of the tool that owns it.

The export is **deliberately partial**. A section whose sources include a
news-, vendor-data- or scraped-page tier is written as a withheld marker
carrying its Postgres id (Spec K §3.3 forbids that content in a public repo),
and ``--import`` reads the marker as "keep the database copy".

Off by default: with ``RESEARCH_WORKSPACE_ENABLED`` false this refuses to run
rather than writing files nobody asked for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--import", dest="do_import", action="store_true",
        help="read the mirror back into Postgres, through validation",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="report what an export would change; write nothing",
    )
    parser.add_argument(
        "--root", default="",
        help="mirror directory (default: RESEARCH_MIRROR_DIR, relative to the repo root)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    return parser


def resolve_root(settings, override: str = "") -> Path:
    configured = override or getattr(settings, "research_mirror_dir", "research")
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


def main(argv=None) -> int:
    from config.settings import Settings
    from database.db import get_session, init_db
    from research_workspace import mirror
    from utils.logger import get_logger, setup_logging

    args = build_parser().parse_args(argv)
    setup_logging()
    log = get_logger("sync_research_mirror")
    settings = Settings()

    if not getattr(settings, "research_workspace_enabled", False):
        print(
            "RESEARCH_WORKSPACE_ENABLED is false; refusing to touch the mirror.",
            file=sys.stderr,
        )
        return 2

    root = resolve_root(settings, args.root)
    init_db(settings.database_url)

    with get_session() as session:
        if args.do_import:
            report = mirror.import_all(session, root).as_dict()
            action = "import"
        elif args.check:
            report = _check(session, root, mirror)
            action = "check"
        else:
            report = mirror.export_all(session, root, settings=settings).as_dict()
            action = "export"

    if args.json:
        print(json.dumps({"action": action, "root": str(root), **report}, indent=2))
    else:
        print(f"{action}: {root}")
        for key, value in report.items():
            print(f"  {key}: {value}")
    log.info("research_mirror_sync", action=action, root=str(root))
    return 0


def _check(session, root: Path, mirror) -> dict:
    """Export into a scratch directory and diff against the committed files."""
    import filecmp
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        mirror.export_all(session, Path(scratch))
        differences = []
        for produced in sorted(Path(scratch).rglob("*.md")):
            relative = produced.relative_to(scratch)
            committed = Path(root) / relative
            if not committed.exists():
                differences.append(f"missing: {relative}")
            elif not filecmp.cmp(produced, committed, shallow=False):
                # `generated_at` differs on every run, so a byte diff is only
                # meaningful once that line is discounted.
                if _without_generated_at(produced) != _without_generated_at(committed):
                    differences.append(f"stale: {relative}")
    return {"differences": differences, "in_sync": not differences}


def _without_generated_at(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("generated_at:")
    )


if __name__ == "__main__":
    sys.exit(main())
