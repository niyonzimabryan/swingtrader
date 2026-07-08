"""Spec B1b + B3 — scan mutual exclusion, failure alerting, daily self-restart."""

import asyncio
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from orchestrator.scheduler import PipelineScheduler
from utils.market_hours import ET

# A normal trading Tuesday used to bypass the weekend/holiday skip in _run_scan.
TRADING_TUESDAY = datetime(2026, 7, 7, 10, 0, tzinfo=ET)


def _settings(daily_restart_time_et="08:57"):
    return SimpleNamespace(
        pre_market_hour=7,
        midday_hour=12,
        post_market_hour=17,
        daily_restart_time_et=daily_restart_time_et,
    )


def _pipeline(run_full_scan):
    return SimpleNamespace(run_full_scan=run_full_scan, notification_manager=AsyncMock())


class ScanMutualExclusionTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_scan_skipped_and_notified(self):
        release = threading.Event()
        scan_calls = []

        def blocking_scan():
            scan_calls.append(1)
            release.wait(timeout=5)

        pipeline = _pipeline(blocking_scan)
        sched = PipelineScheduler(pipeline, _settings())

        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = TRADING_TUESDAY
            first = asyncio.create_task(sched._run_scan())
            # Wait until the first scan has acquired the lock and entered the executor.
            for _ in range(200):
                if sched.scan_running and scan_calls:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(sched.scan_running)

            # A second trigger while the first is still running must skip + notify.
            await sched._run_scan()
            pipeline.notification_manager.system_message.assert_awaited()

            release.set()
            await first

        self.assertEqual(len(scan_calls), 1)  # only one scan actually ran

    async def test_weekend_skips_without_running(self):
        pipeline = _pipeline(Mock())
        sched = PipelineScheduler(pipeline, _settings())
        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = datetime(2026, 7, 11, 10, 0, tzinfo=ET)  # Saturday
            await sched._run_scan()
        pipeline.run_full_scan.assert_not_called()

    async def test_holiday_skips_without_running(self):
        pipeline = _pipeline(Mock())
        sched = PipelineScheduler(pipeline, _settings())
        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = datetime(2026, 7, 3, 10, 0, tzinfo=ET)  # market holiday
            await sched._run_scan()
        pipeline.run_full_scan.assert_not_called()


class ScanFailureAlertTest(unittest.IsolatedAsyncioTestCase):
    async def test_scan_exception_notifies_operator(self):
        def boom():
            raise RuntimeError("scan blew up")

        pipeline = _pipeline(boom)
        sched = PipelineScheduler(pipeline, _settings())
        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = TRADING_TUESDAY
            await sched._run_scan()
        pipeline.notification_manager.agent_failure.assert_awaited()

    async def test_billing_error_sends_explicit_credit_page(self):
        def boom():
            raise RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API.")

        pipeline = _pipeline(boom)
        sched = PipelineScheduler(pipeline, _settings())
        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = TRADING_TUESDAY
            await sched._run_scan()
        messages = [c.args[0] for c in pipeline.notification_manager.system_message.await_args_list]
        self.assertTrue(any("credit balance" in m for m in messages))
        pipeline.notification_manager.agent_failure.assert_awaited()

    async def test_notifier_failure_does_not_mask_scan_error(self):
        def boom():
            raise RuntimeError("scan blew up")

        pipeline = _pipeline(boom)
        pipeline.notification_manager.agent_failure.side_effect = RuntimeError("telegram down")
        sched = PipelineScheduler(pipeline, _settings())
        with patch("orchestrator.scheduler.datetime") as md:
            md.now.return_value = TRADING_TUESDAY
            # Must return cleanly — the Telegram failure is swallowed, not raised.
            await sched._run_scan()


class DailyRestartTest(unittest.IsolatedAsyncioTestCase):
    async def test_restart_triggers_when_idle(self):
        sched = PipelineScheduler(_pipeline(Mock()), _settings())
        cleanup_calls = []

        async def cleanup():
            cleanup_calls.append("cleanup")

        sched.set_restart_callback(cleanup)
        with patch("orchestrator.scheduler.force_restart") as fr:
            await sched._daily_restart()
        fr.assert_called_once_with("daily_scheduled_restart")
        self.assertEqual(cleanup_calls, ["cleanup"])

    async def test_restart_skips_when_scan_running_and_schedules_retry(self):
        sched = PipelineScheduler(_pipeline(Mock()), _settings())
        sched.scheduler.add_job = Mock()
        await sched._scan_lock.acquire()
        try:
            with patch("orchestrator.scheduler.force_restart") as fr:
                await sched._daily_restart()
            fr.assert_not_called()
            sched.scheduler.add_job.assert_called_once()  # one 15-min retry scheduled
            self.assertEqual(sched.scheduler.add_job.call_args.kwargs.get("kwargs"), {"is_retry": True})
        finally:
            sched._scan_lock.release()

    async def test_restart_skips_after_retry_still_running(self):
        sched = PipelineScheduler(_pipeline(Mock()), _settings())
        sched.scheduler.add_job = Mock()
        await sched._scan_lock.acquire()
        try:
            with patch("orchestrator.scheduler.force_restart") as fr:
                await sched._daily_restart(is_retry=True)
            fr.assert_not_called()
            sched.scheduler.add_job.assert_not_called()  # no second retry
        finally:
            sched._scan_lock.release()

    async def test_restart_job_not_registered_when_disabled(self):
        sched = PipelineScheduler(_pipeline(Mock()), _settings(daily_restart_time_et=None))
        sched.scheduler.add_job = Mock()
        sched._add_daily_restart_job()
        sched.scheduler.add_job.assert_not_called()

    async def test_restart_job_registered_regardless_of_scheduler_enabled(self):
        sched = PipelineScheduler(_pipeline(Mock()), _settings())
        sched.scheduler.add_job = Mock()
        sched.scheduler.start = Mock()
        sched.start(enable_scans=False)
        job_ids = [c.kwargs.get("id") for c in sched.scheduler.add_job.call_args_list]
        self.assertIn("daily_restart", job_ids)   # hygiene job runs even with scans off
        self.assertNotIn("pre_market", job_ids)    # scan jobs stay disabled


if __name__ == "__main__":
    unittest.main()
