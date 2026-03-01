"""
Pipeline scheduler — runs the trading pipeline 3x daily + daily digest at 5 PM ET.
Uses APScheduler for local development.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from utils.logger import get_logger

log = get_logger("scheduler")


class PipelineScheduler:
    def __init__(self, pipeline, settings):
        self.pipeline = pipeline
        self.settings = settings
        self.scheduler = AsyncIOScheduler()
        self.daily_digest = None
        self.weekly_report = None

    def set_daily_digest(self, daily_digest):
        """Set the daily digest instance for scheduling."""
        self.daily_digest = daily_digest

    def set_weekly_report(self, weekly_report):
        """Set the weekly report instance for scheduling."""
        self.weekly_report = weekly_report

    def start(self):
        """Start the scheduler with 3 daily scans + daily digest (ET timezone)."""
        # Pre-market scan
        self.scheduler.add_job(
            self._run_scan,
            CronTrigger(hour=self.settings.pre_market_hour, timezone="America/New_York"),
            id="pre_market",
            name="Pre-market scan",
        )

        # Midday scan
        self.scheduler.add_job(
            self._run_scan,
            CronTrigger(hour=self.settings.midday_hour, timezone="America/New_York"),
            id="midday",
            name="Midday scan",
        )

        # Post-market scan
        self.scheduler.add_job(
            self._run_scan,
            CronTrigger(hour=self.settings.post_market_hour, timezone="America/New_York"),
            id="post_market",
            name="Post-market scan",
        )

        # Daily digest at 5 PM ET (weekdays only)
        if self.daily_digest:
            self.scheduler.add_job(
                self._run_digest,
                CronTrigger(hour=17, minute=0, day_of_week="mon-fri", timezone="America/New_York"),
                id="daily_digest",
                name="Daily digest (5 PM ET)",
            )

        # Weekly performance report (Sunday 6 PM ET)
        if self.weekly_report:
            self.scheduler.add_job(
                self._run_weekly_report,
                CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="America/New_York"),
                id="weekly_report",
                name="Weekly report (Sun 6 PM ET)",
            )

        self.scheduler.start()
        job_count = 3 + (1 if self.daily_digest else 0) + (1 if self.weekly_report else 0)
        log.info(
            "scheduler_started",
            jobs=job_count,
            pre_market=f"{self.settings.pre_market_hour}:00 ET",
            midday=f"{self.settings.midday_hour}:00 ET",
            post_market=f"{self.settings.post_market_hour}:00 ET",
            daily_digest="17:00 ET (weekdays)" if self.daily_digest else "disabled",
            weekly_report="Sun 18:00 ET" if self.weekly_report else "disabled",
        )

    def stop(self):
        self.scheduler.shutdown()

    async def _run_digest(self):
        """Send the daily digest. Only called on weekdays by CronTrigger."""
        try:
            log.info("daily_digest_start")
            await self.daily_digest.send_digest()
        except Exception as e:
            log.error("daily_digest_failed", error=str(e))

    async def _run_weekly_report(self):
        """Send the weekly Sonnet performance report. Sunday evenings."""
        try:
            log.info("weekly_report_start")
            await self.weekly_report.send_report()
        except Exception as e:
            log.error("weekly_report_failed", error=str(e))

    async def _run_scan(self):
        """Execute a full pipeline scan. Skips weekends (markets closed)."""
        from datetime import datetime
        import pytz

        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
            log.info("scheduled_scan_skipped", reason="weekend", day=now_et.strftime("%A"))
            return

        try:
            log.info("scheduled_scan_start")
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.pipeline.run_full_scan)
            log.info("scheduled_scan_complete")
        except Exception as e:
            log.error("scheduled_scan_failed", error=str(e))
            # Notify operator of failure
            if self.pipeline.notification_manager:
                await self.pipeline.notification_manager.agent_failure("Scheduler", str(e))
