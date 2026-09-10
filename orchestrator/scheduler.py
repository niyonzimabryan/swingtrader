"""
Pipeline scheduler — runs the trading pipeline 3x daily + daily digest at 5 PM ET.
Uses APScheduler for local development.
"""

import asyncio
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from utils.logger import get_logger
from utils.market_hours import ET, is_market_holiday
from utils.lifecycle import force_restart

log = get_logger("scheduler")

# How long to wait before the single retry of a pre-market restart deferred by an
# in-progress scan.
DAILY_RESTART_RETRY_MINUTES = 15


class PipelineScheduler:
    def __init__(self, pipeline, settings):
        self.pipeline = pipeline
        self.settings = settings
        self.scheduler = AsyncIOScheduler()
        self.daily_digest = None
        self.weekly_report = None
        # Mutual exclusion so overlapping triggers never run two scans at once.
        self._scan_lock = asyncio.Lock()
        # Optional async cleanup hook invoked before the daily self-restart.
        self._restart_callback = None

    def set_daily_digest(self, daily_digest):
        """Set the daily digest instance for scheduling."""
        self.daily_digest = daily_digest

    def set_weekly_report(self, weekly_report):
        """Set the weekly report instance for scheduling."""
        self.weekly_report = weekly_report

    def set_restart_callback(self, callback):
        """Register an async cleanup hook run just before the daily self-restart."""
        self._restart_callback = callback

    @property
    def scan_running(self) -> bool:
        """True while a scan is executing (used to defer the pre-market restart)."""
        return self._scan_lock.locked()

    def start(self, enable_scans: bool = True):
        """
        Start the scheduler. Scan/digest/report jobs are gated by `enable_scans`
        (SCHEDULER_ENABLED); the daily self-restart is process hygiene and runs
        regardless so no hang can outlive a trading day.
        """
        if enable_scans:
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

        # Shadow calibration ledger: nightly forward-returns fill (3:30 AM ET, off
        # market hours). Gated on enable_scans like the other jobs. Trading-day
        # aware and idempotent — it only fills matured horizons.
        if enable_scans:
            self.scheduler.add_job(
                self._run_shadow_returns,
                CronTrigger(hour=3, minute=30, timezone="America/New_York"),
                id="shadow_returns",
                name="Shadow calibration returns fill (3:30 AM ET)",
            )

        # Strategy Lab shadow maturation (daily 4:15 AM ET, off market hours and
        # clear of the two existing nightly jobs). Gated on the Strategy Lab
        # flags rather than on enable_scans: it spends no model budget and calls
        # no broker or vendor — it settles decisions that were already made
        # against bars that are already stored (Spec Q §14, PR 4).
        strategy_lab_shadow = bool(
            getattr(self.settings, "strategy_lab_enabled", False)
        ) and bool(getattr(self.settings, "strategy_lab_shadow_enabled", False))
        if strategy_lab_shadow:
            self.scheduler.add_job(
                self._run_strategy_lab_maturation,
                CronTrigger(hour=4, minute=15, timezone="America/New_York"),
                id="strategy_lab_maturation",
                name="Strategy Lab shadow maturation (4:15 AM ET)",
            )

        # Pattern backfill queue drain (daily 3 AM ET, off market hours). Gated on
        # enable_scans like the other jobs — it spends Gemini quota, so a paused
        # scheduler must not keep draining. Only scheduled when the analog engine
        # is enabled; the job also no-ops defensively.
        pattern_backfill = enable_scans and getattr(self.settings, "pattern_analog_engine_enabled", False)
        if pattern_backfill:
            self.scheduler.add_job(
                self._run_pattern_backfill,
                CronTrigger(hour=3, minute=0, timezone="America/New_York"),
                id="pattern_backfill",
                name="Pattern backfill queue drain (3 AM ET)",
            )

        # Portfolio ledger sync (Spec L §4): hourly in market hours, plus
        # pre-market and after the close. Gated on PORTFOLIO_SYNC_ENABLED
        # rather than on enable_scans — it spends no model budget and a paused
        # scan pipeline is not a reason to stop knowing what is held — but it
        # does reach a broker API, so it stays off until the owner turns it on.
        portfolio_jobs = self._add_portfolio_sync_jobs()

        # Daily pre-market self-restart — runs regardless of SCHEDULER_ENABLED.
        self._add_daily_restart_job()

        self.scheduler.start()
        scan_jobs = 3 if enable_scans else 0
        job_count = (
            scan_jobs
            + (1 if enable_scans and self.daily_digest else 0)
            + (1 if enable_scans and self.weekly_report else 0)
            + (1 if enable_scans else 0)  # shadow_returns
            + (1 if pattern_backfill else 0)
            + (1 if strategy_lab_shadow else 0)
            + len(portfolio_jobs)
        )
        log.info(
            "scheduler_started",
            scans_enabled=enable_scans,
            jobs=job_count,
            pre_market=f"{self.settings.pre_market_hour}:00 ET" if enable_scans else "disabled",
            midday=f"{self.settings.midday_hour}:00 ET" if enable_scans else "disabled",
            post_market=f"{self.settings.post_market_hour}:00 ET" if enable_scans else "disabled",
            daily_digest="17:00 ET (weekdays)" if (enable_scans and self.daily_digest) else "disabled",
            weekly_report="Sun 18:00 ET" if (enable_scans and self.weekly_report) else "disabled",
            shadow_returns="03:30 ET" if enable_scans else "disabled",
            pattern_backfill="03:00 ET" if pattern_backfill else "disabled",
            strategy_lab_maturation="04:15 ET" if strategy_lab_shadow else "disabled",
            portfolio_sync=portfolio_jobs or "disabled",
            daily_restart=self._restart_time_str() or "disabled",
        )

    def stop(self):
        self.scheduler.shutdown()

    # ── Daily pre-market self-restart (Spec B1b) ─────────────────────────────

    def _restart_time_str(self) -> str | None:
        value = getattr(self.settings, "daily_restart_time_et", None)
        value = (value or "").strip() if isinstance(value, str) else None
        return value or None

    def _add_portfolio_sync_jobs(self) -> list:
        """Register the Spec L §4 portfolio sync cadence. Returns the job ids.

        The import is deferred into the method so that the module that knows
        about both the ledger and a broker adapter
        (``scripts/portfolio_sync.py``) is reached only when the flag is on.
        """
        if not bool(getattr(self.settings, "portfolio_sync_enabled", False)):
            return []
        try:
            from scripts.portfolio_sync import register_on

            return register_on(self.scheduler, self.settings)
        except Exception as exc:
            # A broken sync registration must not stop the trading monitor from
            # starting; it pages through the log and the scheduler carries on.
            log.error("portfolio_sync_registration_failed", error=str(exc))
            return []

    def _add_daily_restart_job(self):
        """Register the daily self-restart cron job if a time is configured."""
        time_str = self._restart_time_str()
        if not time_str:
            log.info("daily_restart_disabled")
            return
        try:
            hour, minute = (int(part) for part in time_str.split(":", 1))
        except (ValueError, TypeError):
            log.error("daily_restart_bad_time", value=time_str)
            return
        self.scheduler.add_job(
            self._daily_restart,
            CronTrigger(hour=hour, minute=minute, timezone="America/New_York"),
            id="daily_restart",
            name="Daily pre-market self-restart",
        )

    async def _daily_restart(self, is_retry: bool = False):
        """
        Clean pre-market self-restart so no hang survives more than a day.
        Defers (once, 15 min later) if a scan is in progress, then skips until
        tomorrow rather than killing an active scan mid-flight.
        """
        if self.scan_running:
            if is_retry:
                log.warning("daily_restart_skipped", reason="scan_running_after_retry")
            else:
                retry_at = datetime.now(ET) + timedelta(minutes=DAILY_RESTART_RETRY_MINUTES)
                log.warning(
                    "daily_restart_skipped",
                    reason="scan_running",
                    retry_at=retry_at.strftime("%H:%M ET"),
                )
                self.scheduler.add_job(
                    self._daily_restart,
                    DateTrigger(run_date=retry_at),
                    kwargs={"is_retry": True},
                    id="daily_restart_retry",
                    name="Daily restart retry",
                    replace_existing=True,
                )
            return

        log.info("daily_scheduled_restart")
        await self._perform_restart()

    async def _perform_restart(self):
        """Best-effort graceful cleanup, then terminate for a fresh container."""
        if self._restart_callback:
            try:
                await self._restart_callback()
            except Exception as e:
                log.error("daily_restart_cleanup_failed", error=str(e))
        force_restart("daily_scheduled_restart")

    # ── Scheduled jobs ───────────────────────────────────────────────────────

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

    async def _run_shadow_returns(self):
        """Fill matured forward returns on the shadow calibration ledger (I1)."""
        try:
            from tracking.shadow_ledger import compute_matured_returns

            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None, lambda: compute_matured_returns(self.settings)
            )
            log.info(
                "shadow_returns_job",
                processed=summary.get("processed", 0),
                rows_updated=summary.get("rows_updated", 0),
                horizons_filled=summary.get("horizons_filled", 0),
            )
        except Exception as e:
            log.error("shadow_returns_failed", error=str(e))

    async def _run_pattern_backfill(self):
        """Drain the cold-ticker pattern backfill queue. No-op if the engine is off."""
        if not getattr(self.settings, "pattern_analog_engine_enabled", False):
            return
        try:
            from scripts.backfill_historical_events import drain_queue

            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(None, lambda: drain_queue(self.settings))
            log.info(
                "pattern_backfill_run",
                tickers=summary.get("tickers", 0),
                events_stored=summary.get("events_stored", 0),
                outcomes_computed=summary.get("outcomes_computed", 0),
            )
        except Exception as e:
            log.error("pattern_backfill_failed", error=str(e))

    async def _run_strategy_lab_maturation(self):
        """Settle shadow decisions whose forward bars have arrived (Spec Q §14).

        No-op defensively as well as by registration, so a flag turned off
        without a restart stops the work. It reaches no broker and no vendor:
        the bars it reads are already in `price_bars`, and `strategy_lab/`
        cannot import a broker at all.
        """
        if not (
            getattr(self.settings, "strategy_lab_enabled", False)
            and getattr(self.settings, "strategy_lab_shadow_enabled", False)
        ):
            return
        try:
            from orchestrator.strategy_lab_shadow import mature_shadow_decisions

            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None, lambda: mature_shadow_decisions(self.settings)
            )
            log.info("strategy_lab_maturation_job", **summary.as_log_fields())
        except Exception as e:
            log.error("strategy_lab_maturation_job_failed", error=str(e)[:300])

    async def _notify_system(self, message: str):
        """Send an operator Telegram message; never let a notifier failure propagate."""
        nm = getattr(self.pipeline, "notification_manager", None)
        if not nm:
            return
        try:
            await nm.system_message(message)
        except Exception as e:
            log.error("scan_notify_failed", error=str(e))

    async def _run_scan(self):
        """Execute a full pipeline scan. Skips weekends/holidays and overlaps."""
        now_et = datetime.now(ET)
        if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
            log.info("scheduled_scan_skipped", reason="weekend", day=now_et.strftime("%A"))
            return
        if is_market_holiday(now_et.date()):
            log.info("scheduled_scan_skipped", reason="market_holiday", date=now_et.strftime("%Y-%m-%d"))
            return

        # Mutual exclusion: skip (do NOT queue) if a scan is already running.
        if self.scan_running:
            log.warning("scan_skipped_overlap")
            await self._notify_system(
                "⚠️ Scheduled scan skipped — a previous scan is still running. "
                "No new scan was started (skips are safe; backlogs are not)."
            )
            return

        async with self._scan_lock:
            try:
                log.info("scheduled_scan_start")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.pipeline.run_full_scan)
                log.info("scheduled_scan_complete")
            except Exception as e:
                log.error("scheduled_scan_failed", error=str(e))
                # Provider credit exhaustion gets an explicit page — every LLM call
                # fails until the balance is topped up, not just this scan.
                error_text = str(e).lower()
                if "credit balance" in error_text or "billing" in error_text:
                    await self._notify_system(
                        "🚨 Anthropic API credit balance appears exhausted — the scan "
                        "failed and ALL LLM calls will fail until the balance is topped "
                        f"up. Error: {str(e)[:200]}"
                    )
                # Surface the dead scan to the operator. Wrap so a Telegram failure
                # can't mask the original error.
                nm = getattr(self.pipeline, "notification_manager", None)
                if nm:
                    try:
                        await nm.agent_failure("Scheduler", str(e))
                    except Exception as notify_err:
                        log.error("scan_failure_notify_failed", error=str(notify_err))
