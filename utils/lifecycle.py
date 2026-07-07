"""
Process-lifecycle recovery: restart-on-hang plumbing (Spec B).

Railway's restart policy is ON_FAILURE, so a non-zero exit yields a fresh
container. Two callers use this:
  - MonitorWatchdog — trips when a monitor loop stops ticking during market hours.
  - the scheduler's daily pre-market restart — routine hygiene so no hang lasts a day.

`force_restart` uses os._exit rather than sys.exit on purpose: it runs inside
background asyncio tasks / APScheduler jobs whose frameworks swallow SystemExit,
so sys.exit alone would not reliably terminate the process.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

from utils.logger import get_logger
from utils.market_hours import is_market_open

log = get_logger("lifecycle")


def force_restart(reason: str, exit_code: int = 1) -> None:
    """
    Terminate the process with a non-zero code so Railway restarts the container.

    Best-effort flush of stdio, then os._exit — guaranteed to kill the process
    from any context (background task, executor thread, APScheduler job).
    """
    log.critical("force_restart", reason=reason, exit_code=exit_code)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(exit_code)


class MonitorWatchdog:
    """
    Periodically checks that each monitor's loop is still ticking. If a monitor's
    last tick is older than `stale_s` *during market hours*, logs CRITICAL, attempts
    a Telegram alert, and force-restarts the process.

    Monitors expose `.name` and `.last_tick` (a tz-aware UTC datetime, or None
    before the first iteration). Off-hours staleness is ignored — the loops only do
    broker work while the market is open, and the production hang happened intraday.
    """

    def __init__(self, monitors, notification_manager, settings):
        self.monitors = list(monitors)
        self.nm = notification_manager
        self.settings = settings
        self.interval_s = getattr(settings, "monitor_watchdog_interval_s", 300)
        self.stale_s = getattr(settings, "monitor_watchdog_stale_s", 900)
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("watchdog_started", interval_s=self.interval_s, stale_s=self.stale_s)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        log.info("watchdog_stopped")

    async def _loop(self):
        while self._running:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check_once()
            except Exception as e:
                # Never let a watchdog bug crash the watchdog — only a genuine
                # stale monitor should terminate the process.
                log.error("watchdog_check_error", error=str(e))

    async def check_once(self, now: datetime | None = None) -> bool:
        """
        Run one staleness check. Returns True if a restart was triggered.
        Only trips during market hours. Exposed for direct testing.
        """
        now = now or datetime.now(timezone.utc)
        if not is_market_open(now):
            return False

        for monitor in self.monitors:
            last_tick = getattr(monitor, "last_tick", None)
            if last_tick is None:
                continue
            age_s = (now - last_tick).total_seconds()
            if age_s > self.stale_s:
                name = getattr(monitor, "name", monitor.__class__.__name__)
                log.critical("monitor_watchdog_stale", monitor=name, stale_s=round(age_s))
                await self._alert(name, age_s)
                force_restart(f"monitor_stale:{name}")
                return True  # not reached in production (process exits)
        return False

    async def _alert(self, name: str, age_s: float):
        """Best-effort Telegram alert. A notifier failure must not block the restart."""
        if not self.nm:
            return
        try:
            await self.nm.system_message(
                f"⚠️ Watchdog: '{name}' loop stuck {int(age_s)}s during market hours. "
                f"Restarting the bot so Railway relaunches a fresh container."
            )
        except Exception as e:
            log.error("watchdog_alert_failed", monitor=name, error=str(e))
