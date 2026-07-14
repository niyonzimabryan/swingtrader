"""BRY-301 — billing-exhaustion Telegram paging (module-level, once per process per provider)."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils import billing_alerts


def _close_scheduled_coroutines(mock_run_coroutine_threadsafe):
    """asyncio.run_coroutine_threadsafe is mocked in these tests, so the real
    coroutine built from notification_manager.system_message(...) is never
    awaited — close it to avoid a 'coroutine was never awaited' warning."""
    for call in mock_run_coroutine_threadsafe.call_args_list:
        call.args[0].close()


class BillingAlertsPageOnceTest(unittest.TestCase):
    def setUp(self):
        billing_alerts._paged_providers.clear()
        billing_alerts._notification_manager = None
        billing_alerts._bot_loop = None

    def tearDown(self):
        billing_alerts._paged_providers.clear()
        billing_alerts._notification_manager = None
        billing_alerts._bot_loop = None

    def test_page_fires_exactly_once_across_repeated_errors(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        billing_alerts.register(nm, loop)

        with patch("utils.billing_alerts.asyncio.run_coroutine_threadsafe") as mock_rct:
            billing_alerts.page_once("anthropic", "first failure")
            billing_alerts.page_once("anthropic", "second failure")
            billing_alerts.page_once("anthropic", "third failure")

        mock_rct.assert_called_once()
        self.assertIs(mock_rct.call_args.args[1], loop)
        _close_scheduled_coroutines(mock_rct)

    def test_providers_are_tracked_independently(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        billing_alerts.register(nm, loop)

        with patch("utils.billing_alerts.asyncio.run_coroutine_threadsafe") as mock_rct:
            billing_alerts.page_once("anthropic", "claude down")
            billing_alerts.page_once("gemini", "gemini down")
            billing_alerts.page_once("anthropic", "claude down again")
            billing_alerts.page_once("gemini", "gemini down again")

        self.assertEqual(mock_rct.call_count, 2)
        _close_scheduled_coroutines(mock_rct)

    def test_no_notifier_registered_does_not_raise(self):
        # register() is never called in this test — module state starts clean.
        try:
            billing_alerts.page_once("anthropic", "should be a silent no-op")
        except Exception as e:
            self.fail(f"page_once raised with no notifier registered: {e}")

    def test_closed_loop_does_not_raise(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: True)
        billing_alerts.register(nm, loop)
        try:
            billing_alerts.page_once("anthropic", "loop is closed")
        except Exception as e:
            self.fail(f"page_once raised with a closed loop: {e}")

    def test_page_send_failure_is_swallowed(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        billing_alerts.register(nm, loop)
        with patch(
            "utils.billing_alerts.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop is not running"),
        ) as mock_rct:
            try:
                billing_alerts.page_once("anthropic", "boom")
            except Exception as e:
                self.fail(f"page_once raised: {e}")
        _close_scheduled_coroutines(mock_rct)


if __name__ == "__main__":
    unittest.main()
