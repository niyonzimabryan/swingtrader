"""The page a triggered invalidator fires (Spec M §4).

It pages. That is the whole surface. A triggered invalidator moves the thesis
to ``weakened`` and tells Bryan the reason for holding may be gone — it never
creates an order, never creates a proposal, and this module imports nothing
from ``execution`` or from any broker adapter so that "never" is a property of
the import graph and not of a comment.

The default sink is the same Telegram path the rest of the system pages
through: a ``NotificationManager`` plus the bot's event loop, registered once
from ``main.py`` exactly as ``utils.billing_alerts`` is. With nothing
registered — a script, a test, a cron job — paging is a logged no-op and the
structlog line is the record. :func:`set_pager` swaps the sink for a callable,
which is how the tests observe a page without a Telegram account.

Unlike ``billing_alerts.page_once`` there is no per-process deduplication.
That module suppresses repeats because a billing outage fires on every LLM
call; an invalidator trigger is a rare, individually meaningful event, and the
*store* already guarantees it fires once per invalidator by moving the row to
``triggered``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("research_paging")

_notification_manager = None
_bot_loop = None


@dataclass(frozen=True)
class PageEvent:
    """What a page carries. No prices, no sizes, no suggested action."""

    kind: str
    ticker: str
    thesis_id: int
    thesis_title: str
    detail: str
    invalidator_id: int | None = None
    invalidator_type: str = ""
    at: datetime = field(default_factory=utcnow_naive)

    def as_message(self) -> str:
        head = f"Thesis invalidator triggered — {self.ticker}: {self.thesis_title}"
        return (
            f"{head}\n"
            f"{self.invalidator_type}: {self.detail}\n"
            f"Status moved to weakened. No position was changed and no order or "
            f"proposal was created — that decision is yours."
        )


def register(notification_manager, bot_loop) -> None:
    """Wire the live Telegram sink. Called once from ``main.py``."""
    global _notification_manager, _bot_loop
    _notification_manager = notification_manager
    _bot_loop = bot_loop


def _telegram_pager(event: PageEvent) -> bool:
    """Best effort: a Telegram failure must not roll back the status change."""
    if not _notification_manager or not _bot_loop or _bot_loop.is_closed():
        return False
    try:
        asyncio.run_coroutine_threadsafe(
            _notification_manager.system_message(event.as_message()), _bot_loop
        )
        return True
    except Exception as exc:  # pragma: no cover - exercised by hand, not in CI
        log.error("research_page_failed", error=str(exc), ticker=event.ticker)
        return False


_pager = _telegram_pager


def set_pager(pager):
    """Replace the sink; returns the previous one so a test can restore it."""
    global _pager
    previous = _pager
    _pager = pager or _telegram_pager
    return previous


def page(event: PageEvent) -> bool:
    """Emit ``event``. Returns whether a sink accepted it; never raises."""
    log.warning(
        "research_page",
        kind=event.kind,
        ticker=event.ticker,
        thesis_id=event.thesis_id,
        invalidator_id=event.invalidator_id,
        invalidator_type=event.invalidator_type,
        detail=event.detail,
    )
    try:
        return bool(_pager(event))
    except Exception as exc:  # pragma: no cover - a sink is not allowed to break the job
        log.error("research_page_sink_failed", error=str(exc))
        return False
