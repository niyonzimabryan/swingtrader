"""Spec B1 — broker-call timeouts + heartbeat keep the monitor loops alive."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.async_call import call_with_timeout


_SETTINGS = SimpleNamespace(monitor_broker_call_timeout_s=30, max_holding_days=20)


class _TimeoutAlpaca:
    """Every broker method raises asyncio.TimeoutError, as a stuck SDK call would."""

    def get_account_info(self):
        raise asyncio.TimeoutError()

    def get_positions_detail(self):
        raise asyncio.TimeoutError()

    def get_order_status(self, order_id):
        raise asyncio.TimeoutError()


class CallWithTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_value_from_sync_fn(self):
        result = await call_with_timeout(lambda x: x + 1, 41, timeout_s=5)
        self.assertEqual(result, 42)

    async def test_raises_timeout_when_call_blocks(self):
        def slow():
            time.sleep(0.3)
            return "done"

        with self.assertRaises(asyncio.TimeoutError):
            await call_with_timeout(slow, timeout_s=0.05)

    async def test_reraises_underlying_exception(self):
        def boom():
            raise ValueError("broker said no")

        with self.assertRaises(ValueError):
            await call_with_timeout(boom, timeout_s=5)


class PositionMonitorTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_iteration_survives_broker_timeout(self):
        from execution.position_monitor import PositionMonitor

        monitor = PositionMonitor(_TimeoutAlpaca(), AsyncMock(), _SETTINGS)
        monitor._is_market_hours = lambda: True
        monitor._running = True

        async def stop_after_one(_seconds):
            monitor._running = False

        # One full iteration: both broker calls raise TimeoutError; the loop must
        # log-and-continue, never propagate, and still record its heartbeat.
        with patch("execution.position_monitor.asyncio.sleep", stop_after_one):
            await monitor._monitor_loop()

        self.assertIsNotNone(monitor.last_tick)


class OrderMonitorTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_iteration_survives_broker_timeout(self):
        from execution.order_monitor import OrderMonitor

        monitor = OrderMonitor(_TimeoutAlpaca(), AsyncMock(), _SETTINGS)
        monitor._running = True

        async def check_via_real_broker_call():
            # Exercise the real _broker_call path with a call that times out.
            await monitor._broker_call(monitor.alpaca.get_order_status, "abc")

        monitor._check_open_trades = check_via_real_broker_call

        async def stop_after_one(_seconds):
            monitor._running = False

        with patch("execution.order_monitor.asyncio.sleep", stop_after_one):
            await monitor._monitor_loop()

        self.assertIsNotNone(monitor.last_tick)


if __name__ == "__main__":
    unittest.main()
