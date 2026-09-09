"""The out-of-band page, as an injected callable.

Spec L §4 requires a sync that fails closed to **page** rather than write, and
Spec K §7 keeps Telegram as the out-of-band channel. The ledger does not import
that channel: ``bot`` is one of the packages the workspace's import closure may
never reach, and a module that pages by importing the bot would have to be kept
out of ``portfolio/`` forever.

So a pager is any callable ``(event: str, detail: dict) -> None``. The default
writes a structured log line, which is what a Railway deploy scrapes anyway;
``scripts/portfolio_sync.py`` is where a Telegram-backed pager is wired in.
"""

from __future__ import annotations

from utils.logger import get_logger

log = get_logger("portfolio_sync")

#: Events a pager may receive. Stable strings — an alert rule matches on them.
MASS_DELETION_BLOCKED = "portfolio_mass_deletion_blocked"
SYNC_ACCOUNT_FAILED = "portfolio_sync_account_failed"
SYNC_FAILED = "portfolio_sync_failed"
RECONCILIATION_REQUIRED = "reconciliation_required"


def log_pager(event: str, detail: dict) -> None:
    """Default pager: a structured ``error`` line, one per page."""
    log.error(event, **{k: v for k, v in (detail or {}).items()})


class RecordingPager:
    """A pager that keeps what it was sent. For tests and for ``--dry-run``."""

    def __init__(self):
        self.pages: list[tuple[str, dict]] = []

    def __call__(self, event: str, detail: dict) -> None:
        self.pages.append((event, dict(detail or {})))

    def events(self) -> list[str]:
        return [event for event, _ in self.pages]
