"""
Pipeline scheduler — runs the trading pipeline 3x daily.
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

    def start(self):
        """Start the scheduler with 3 daily runs (ET timezone)."""
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

        self.scheduler.start()
        log.info(
            "scheduler_started",
            jobs=3,
            pre_market=f"{self.settings.pre_market_hour}:00 ET",
            midday=f"{self.settings.midday_hour}:00 ET",
            post_market=f"{self.settings.post_market_hour}:00 ET",
        )

    def stop(self):
        self.scheduler.shutdown()

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
