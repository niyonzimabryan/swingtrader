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
#: Startup could not read what the broker actually holds. The ledger is not
#: wrong after this — it is *blank*, and blank reads as no exposure to anything
#: that forgets to check freshness. Same string the runtime already logged, so
#: an existing alert rule keeps matching.
STARTUP_RECONCILIATION_FAILED = "startup_position_reconciliation_failed"


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


class NotifyPager:
    """A pager that renders a page card and broadcasts it (``notify/``).

    Wired where a Telegram-backed pager already is; it does not replace the log
    line, it adds a channel. Constructed only when a channel is configured, so
    with ``NOTIFY_EMAIL_ENABLED`` off and no Telegram credentials this class is
    never instantiated and paging is exactly what it was.

    **Never raises.** A page is a report about something that already went
    wrong; a delivery failure inside it must not become a second failure inside
    the sync that was trying to tell you about the first.
    """

    def __init__(self, settings, *, channels=None, session_factory=None, also_log: bool = True):
        self.settings = settings
        self.channels = channels
        self.session_factory = session_factory
        self.also_log = also_log

    def __call__(self, event: str, detail: dict) -> None:
        if self.also_log:
            log_pager(event, detail)
        try:
            from notify.cards import deliver
            from notify.cards.alert import build_payload
            from utils.timeutils import utcnow_naive

            payload = build_payload(
                event=event,
                detail=detail,
                created_at_utc=utcnow_naive().isoformat(),
            )
            deliver(
                payload,
                settings=self.settings,
                channels=self.channels,
                session_factory=self.session_factory,
            )
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error("page_delivery_failed", event=event, error=str(exc))


def pager_for(settings, *, channels=None, session_factory=None):
    """The pager a process should use: the notifying one, or the log-only default.

    Returns :func:`log_pager` when no channel is configured, so a deployment
    that has set nothing up behaves exactly as it did before this existed.
    """
    if channels is None:
        from notify.registry import configured_channels

        channels = configured_channels(settings, session_factory=session_factory)
    if not channels:
        return log_pager
    return NotifyPager(
        settings, channels=channels, session_factory=session_factory
    )
