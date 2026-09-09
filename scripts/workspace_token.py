#!/usr/bin/env python3
"""Issue, list, and revoke workspace owner tokens (Spec K §4.1).

    python -m scripts.workspace_token --issue --label "codex-laptop"
    python -m scripts.workspace_token --issue --label "claude-web" --scopes read,research:write
    python -m scripts.workspace_token --list
    python -m scripts.workspace_token --revoke --label "codex-laptop"

The secret is printed **once**. Only its SHA-256 digest is stored, so a lost
token is re-issued, never recovered. Put it in the client's environment as
``WORKSPACE_TOKEN`` (see ``docs/WORKSPACE_ACCESS.md``); never commit it.

Scopes are ``read``, ``research:write``, ``propose``, ``admin``. There is no
``execute`` scope and no flag that creates one — order placement is not
reachable by token at all (Spec L §6).

Admin tokens are separate on purpose: ``--scopes admin`` issues a token for the
owner's own use and it must not be placed in an agent's environment.
"""

from __future__ import annotations

import argparse
import sys

from config.settings import Settings
from database.db import get_session, init_db
from workspace import scopes as scope_module
from workspace import tokens


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--issue", action="store_true", help="Create a token.")
    action.add_argument("--list", action="store_true", help="List tokens (no secrets).")
    action.add_argument("--revoke", action="store_true", help="Revoke a token by label.")
    parser.add_argument("--label", default="", help='Client name, e.g. "codex-laptop".')
    parser.add_argument(
        "--scopes",
        default=scope_module.READ,
        help=f"Comma-separated, from {','.join(scope_module.SCOPES)}. Default: read.",
    )
    parser.add_argument("--note", default="", help="Free text stored with the token.")
    parser.add_argument(
        "--database-url",
        default="",
        help="Override DATABASE_URL (the workspace database to write to).",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    settings = Settings()
    init_db(args.database_url or settings.database_url)

    if args.list:
        with get_session() as session:
            rows = tokens.listing(session)
        if not rows:
            print("no workspace tokens issued")
            return 0
        print(f"{'label':<24} {'prefix':<14} {'scopes':<28} {'state':<8} last used")
        for row in rows:
            print(
                f"{row['label']:<24} {row['prefix']:<14} {row['scopes']:<28} "
                f"{'revoked' if row['revoked'] else 'active':<8} "
                f"{row['last_used_at'] or 'never'}"
            )
        return 0

    if args.revoke:
        with get_session() as session:
            revoked = tokens.revoke(session, args.label)
        if not revoked:
            print(f"no active token labelled {args.label!r}", file=sys.stderr)
            return 1
        print(f"revoked {args.label!r}")
        return 0

    requested = [s.strip() for s in args.scopes.split(",") if s.strip()]
    try:
        with get_session() as session:
            secret = tokens.issue(session, args.label, requested, note=args.note)
    except (tokens.TokenError, scope_module.UnknownScope) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"label:  {args.label}")
    print(f"scopes: {scope_module.render(requested)}")
    print(f"token:  {secret}")
    print()
    print("Shown once — only its SHA-256 digest is stored. Set it as")
    print("WORKSPACE_TOKEN in the client's environment; do not commit it.")
    if scope_module.ADMIN in requested:
        print()
        print("This is an ADMIN token. Spec K section 4.1: it must never be placed")
        print("in an agent's environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
