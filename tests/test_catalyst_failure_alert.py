"""BRY-301 — scan-complete alert when nearly all catalyst/LLM calls failed."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.pipeline import TradingPipeline


def _pipeline(threshold=0.9, notification_manager=None, bot_loop=None):
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.settings = SimpleNamespace(catalyst_failure_rate_alert_threshold=threshold)
    pipeline.notification_manager = notification_manager
    pipeline.bot_loop = bot_loop
    return pipeline


class HighCatalystFailureRateAlertTest(unittest.TestCase):
    def test_alerts_when_failure_rate_exceeds_threshold(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        pipeline = _pipeline(notification_manager=nm, bot_loop=loop)

        with patch("orchestrator.pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            pipeline._maybe_alert_high_failure_rate(total_scanned=10, catalyst_failures=10)

        mock_rct.assert_called_once()
        self.assertIs(mock_rct.call_args.args[1], loop)
        mock_rct.call_args.args[0].close()  # coroutine never scheduled (run_coroutine_threadsafe mocked)

    def test_no_alert_when_failure_rate_at_or_below_threshold(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        pipeline = _pipeline(notification_manager=nm, bot_loop=loop)

        with patch("orchestrator.pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            pipeline._maybe_alert_high_failure_rate(total_scanned=10, catalyst_failures=9)

        mock_rct.assert_not_called()

    def test_no_alert_when_nothing_was_scanned(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        pipeline = _pipeline(notification_manager=nm, bot_loop=loop)

        with patch("orchestrator.pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            pipeline._maybe_alert_high_failure_rate(total_scanned=0, catalyst_failures=0)

        mock_rct.assert_not_called()

    def test_respects_custom_threshold(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        pipeline = _pipeline(threshold=0.5, notification_manager=nm, bot_loop=loop)

        with patch("orchestrator.pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            pipeline._maybe_alert_high_failure_rate(total_scanned=10, catalyst_failures=6)

        mock_rct.assert_called_once()
        mock_rct.call_args.args[0].close()

    def test_no_notifier_registered_does_not_raise(self):
        pipeline = _pipeline(notification_manager=None, bot_loop=None)
        try:
            pipeline._maybe_alert_high_failure_rate(total_scanned=5, catalyst_failures=5)
        except Exception as e:
            self.fail(f"_maybe_alert_high_failure_rate raised with no notifier registered: {e}")

    def test_notifier_failure_does_not_raise(self):
        nm = AsyncMock()
        loop = SimpleNamespace(is_closed=lambda: False)
        pipeline = _pipeline(notification_manager=nm, bot_loop=loop)

        with patch(
            "orchestrator.pipeline.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop is not running"),
        ) as mock_rct:
            try:
                pipeline._maybe_alert_high_failure_rate(total_scanned=10, catalyst_failures=10)
            except Exception as e:
                self.fail(f"_maybe_alert_high_failure_rate raised: {e}")
        mock_rct.call_args.args[0].close()


if __name__ == "__main__":
    unittest.main()
