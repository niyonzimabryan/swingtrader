"""Token scopes for the workspace API (Spec K §4.1).

Four scopes, and **no ``execute`` scope**. Order placement is not reachable by
token at all: there is no scope that grants it, no route that performs it, and
no import path from this package to a broker adapter (Spec L §6). That is
asserted by ``tests/test_no_execute_scope.py`` rather than left to convention.

``TOOL_SCOPES`` mirrors the tool table in Spec K §4.2. Only ``whoami`` exists in
Phase 0b; the rest are declared here so that the phase which adds a tool has to
choose its scope deliberately, and so ``test_token_scopes`` can assert today
that a ``read`` token is refused on ``research_write`` and ``propose_order``.
"""

from __future__ import annotations

READ = "read"
RESEARCH_WRITE = "research:write"
PROPOSE = "propose"
ADMIN = "admin"

#: Every scope a token may carry. Order is the display order.
SCOPES: tuple[str, ...] = (READ, RESEARCH_WRITE, PROPOSE, ADMIN)

#: The scope each tool in Spec K §4.2 requires. Phases 1-4 add the tools; the
#: mapping is fixed here so no phase can quietly ship a write tool at `read`.
TOOL_SCOPES: dict[str, str] = {
    # Phase 0b
    "whoami": READ,
    # Phase 1 (Spec L)
    "portfolio_overview": READ,
    "position_detail": READ,
    "orders_open": READ,
    # Phase 2 (Spec M)
    "research_get": READ,
    "research_search": READ,
    "research_write": RESEARCH_WRITE,
    "thesis_review": RESEARCH_WRITE,
    "journal_append": RESEARCH_WRITE,
    # Phase 3 (Spec N)
    "compare_setups": READ,
    "cohort_detail": READ,
    # Phase 4 (Spec O)
    "filings_recent": READ,
    "macro_state": READ,
    "news_timeline": READ,
    # Phase 5 (Spec Q)
    "experiments_status": READ,
    # Phase 6 (Spec L §6) — creates a `proposed` row. Places nothing.
    "propose_order": PROPOSE,
}

#: Scopes whose calls count against the write rate limit rather than the read
#: one (Spec K §4.1: 60 read / 10 write per minute per token).
WRITE_SCOPES = frozenset({RESEARCH_WRITE, PROPOSE, ADMIN})


class UnknownScope(ValueError):
    """A scope string that is not one of :data:`SCOPES`."""


def parse(raw: str) -> tuple[str, ...]:
    """Parse a stored comma-separated scope string, rejecting unknown scopes."""
    scopes = tuple(s.strip() for s in (raw or "").split(",") if s.strip())
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise UnknownScope(
            f"unknown scope(s) {unknown}; valid scopes are {list(SCOPES)}"
        )
    return scopes


def render(scopes) -> str:
    """Canonical stored form: known scopes, in :data:`SCOPES` order, deduped."""
    wanted = set(scopes)
    unknown = sorted(wanted - set(SCOPES))
    if unknown:
        raise UnknownScope(
            f"unknown scope(s) {unknown}; valid scopes are {list(SCOPES)}"
        )
    return ",".join(s for s in SCOPES if s in wanted)


def kind_for(scope: str) -> str:
    """``"write"`` if calls needing this scope count as writes, else ``"read"``."""
    return "write" if scope in WRITE_SCOPES else "read"
