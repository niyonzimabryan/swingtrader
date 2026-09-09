"""When the portfolio sync runs (Spec L §4).

    hourly during market hours, once pre-market, once after the close.

A swing-trading horizon does not need a 15-minute sync, and hourly is a quarter
of the API pressure on a broker surface whose rate limits are not published
anywhere — Robinhood's MCP has no developer documentation at all, so the only
safe assumption is that the limits exist and we do not know them.

This module builds APScheduler triggers and nothing else. It takes the callable
that performs a sync as an argument rather than importing one, so it stays free
of ``execution`` like the rest of the package; ``scripts/portfolio_sync.py``
supplies the real one.
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from utils.logger import get_logger
from utils.market_hours import ET, is_trading_day

log = get_logger("portfolio_sync")

#: Job ids, so a re-registration replaces rather than duplicates.
INTRADAY_JOB_ID = "portfolio_sync_intraday"
PRE_MARKET_JOB_ID = "portfolio_sync_pre_market"
POST_CLOSE_JOB_ID = "portfolio_sync_post_close"

#: 9:30-16:00 ET. The intraday trigger fires on the hour inside that span; the
#: pre-market and post-close jobs cover the edges.
MARKET_HOURS = "9-15"


def build_triggers(settings) -> dict:
    """``job_id -> CronTrigger`` for the Spec L §4 cadence, in US Eastern.

    Weekday-only at the trigger level; the holiday table is checked at fire time
    by :func:`should_run_now`, because a cron expression cannot express "not
    Thanksgiving".
    """
    interval = max(1, int(getattr(settings, "portfolio_sync_interval_minutes", 60) or 60))
    pre_hour = int(getattr(settings, "portfolio_sync_pre_market_hour", 8) or 8)
    close_hour = int(getattr(settings, "portfolio_sync_post_close_hour", 16) or 16)
    close_minute = int(getattr(settings, "portfolio_sync_post_close_minute", 30) or 30)

    if interval >= 60:
        # Hourly (or slower): fire on the hour within market hours.
        step = max(1, interval // 60)
        intraday = CronTrigger(
            hour=f"9-15/{step}" if step > 1 else MARKET_HOURS,
            minute=0,
            day_of_week="mon-fri",
            timezone=ET,
        )
    else:
        intraday = CronTrigger(
            hour=MARKET_HOURS,
            minute=f"*/{interval}",
            day_of_week="mon-fri",
            timezone=ET,
        )

    return {
        INTRADAY_JOB_ID: intraday,
        PRE_MARKET_JOB_ID: CronTrigger(
            hour=pre_hour, minute=0, day_of_week="mon-fri", timezone=ET
        ),
        POST_CLOSE_JOB_ID: CronTrigger(
            hour=close_hour, minute=close_minute, day_of_week="mon-fri", timezone=ET
        ),
    }


def should_run_now(now=None) -> bool:
    """False on weekends and full NYSE closures.

    The cron triggers are weekday-only; this catches the holidays a cron
    expression cannot, using the same closure table as the monitor and the
    scheduler rather than a second copy of it.
    """
    from datetime import datetime

    moment = now or datetime.now(ET)
    return is_trading_day(moment.date())


def register(scheduler, settings, run_sync_callable) -> list[str]:
    """Attach the sync jobs to an APScheduler instance. Returns the job ids.

    A no-op unless ``PORTFOLIO_SYNC_ENABLED`` is true: every new capability
    ships behind a flag defaulting to off, and a sync job that ran by default
    would start hitting a broker API the moment this merges.
    """
    if not bool(getattr(settings, "portfolio_sync_enabled", False)):
        log.info("portfolio_sync_disabled", detail="PORTFOLIO_SYNC_ENABLED is false")
        return []

    registered = []
    for job_id, trigger in build_triggers(settings).items():
        scheduler.add_job(
            run_sync_callable,
            trigger,
            id=job_id,
            name=f"Portfolio sync ({job_id})",
            kwargs={"reason": job_id},
            replace_existing=True,
        )
        registered.append(job_id)
    log.info("portfolio_sync_scheduled", jobs=registered)
    return registered
