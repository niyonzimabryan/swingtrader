"""Spec B1 — watchdog exits on a stuck monitor during market hours only."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.lifecycle import MonitorWatchdog, force_restart


class _FakeMonitor:
    def __init__(self, name, last_tick):
        self.name = name
        self.last_tick = last_tick


def _watchdog(monitors, nm=None):
    settings = SimpleNamespace(monitor_watchdog_interval_s=300, monitor_watchdog_stale_s=900)
    return MonitorWatchdog(monitors, nm or AsyncMock(), settings)


class WatchdogTest(unittest.IsolatedAsyncioTestCase):
    async def test_restarts_when_stale_during_market_hours(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=1000)
        wd = _watchdog([_FakeMonitor("position_monitor", stale)])
        with patch("utils.lifecycle.is_market_open", return_value=True), \
                patch("utils.lifecycle.force_restart") as fr:
            tripped = await wd.check_once()
        self.assertTrue(tripped)
        fr.assert_called_once()
        wd.nm.system_message.assert_awaited()  # operator alert attempted

    async def test_no_restart_when_market_closed(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=1000)
        wd = _watchdog([_FakeMonitor("position_monitor", stale)])
        with patch("utils.lifecycle.is_market_open", return_value=False), \
                patch("utils.lifecycle.force_restart") as fr:
            tripped = await wd.check_once()
        self.assertFalse(tripped)
        fr.assert_not_called()

    async def test_no_restart_when_fresh(self):
        fresh = datetime.now(timezone.utc)
        wd = _watchdog([_FakeMonitor("order_monitor", fresh)])
        with patch("utils.lifecycle.is_market_open", return_value=True), \
                patch("utils.lifecycle.force_restart") as fr:
            tripped = await wd.check_once()
        self.assertFalse(tripped)
        fr.assert_not_called()

    async def test_no_restart_before_first_tick(self):
        wd = _watchdog([_FakeMonitor("order_monitor", None)])
        with patch("utils.lifecycle.is_market_open", return_value=True), \
                patch("utils.lifecycle.force_restart") as fr:
            tripped = await wd.check_once()
        self.assertFalse(tripped)
        fr.assert_not_called()

    async def test_alert_failure_does_not_block_restart(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=1000)
        nm = AsyncMock()
        nm.system_message.side_effect = RuntimeError("telegram down")
        wd = _watchdog([_FakeMonitor("position_monitor", stale)], nm=nm)
        with patch("utils.lifecycle.is_market_open", return_value=True), \
                patch("utils.lifecycle.force_restart") as fr:
            tripped = await wd.check_once()
        self.assertTrue(tripped)
        fr.assert_called_once()


class ForceRestartTest(unittest.TestCase):
    def test_force_restart_exits_with_code_1(self):
        with patch("utils.lifecycle.os._exit") as ex:
            force_restart("test-reason")
        ex.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
