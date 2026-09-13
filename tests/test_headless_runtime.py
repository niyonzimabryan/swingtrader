"""`main.py` with TELEGRAM_ENABLED=false: everything runs, Telegram does not.

The claim this file exists to hold is narrow and load-bearing: *with the flag
off nothing changes*, and with it on the process still does all of its work. So
the same startup path is driven twice against the same fakes, and the two runs
are compared on what each one built.

Nothing here touches a network, a broker, or a database. `headlessfixture`
patches every constructor `main()` calls and drives the shutdown through the
SIGTERM handler `main()` installs, so a run either comes up and shuts down
cleanly or the test fails — there is no third outcome and nothing is left
running.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

from tests.headlessfixture import (
    FakeBot,
    FakeMessageQueue,
    FakeSettings,
    drop_telegram_modules,
    patched_main,
    run_main,
)
from tests.notifyfixture import FAKE_CARD_SECRET, FAKE_RESEND_KEY

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:  # pragma: no cover - depends on how the suite was invoked
    sys.path.insert(0, REPO)


def email_overrides() -> dict:
    return dict(
        notify_email_enabled=True,
        resend_api_key=FAKE_RESEND_KEY,
        pager_email_from="swingtrader@example.invalid",
        pager_email_to="owner@example.invalid",
        card_link_secret=FAKE_CARD_SECRET,
    )


class HeadlessStartupTests(unittest.TestCase):
    """The flag off: no Telegram anywhere, and the runtime otherwise intact."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SCHEDULER_ENABLED": "true"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        from portfolio import approvals as approvals_mod
        from research_workspace import paging as research_paging
        from utils import billing_alerts

        self.addCleanup(approvals_mod.clear_card_sender)
        self.addCleanup(billing_alerts.register, None, None)
        self.addCleanup(research_paging.register, None, None)

    def _run(self, settings, *, patch_telegram=False):
        with patched_main(settings, patch_telegram=patch_telegram) as harness:
            asyncio.run(run_main(harness))
        return harness

    def test_headless_starts_with_no_telegram_variables_at_all(self):
        drop_telegram_modules()
        settings = FakeSettings(telegram_enabled=False, **email_overrides())

        harness = self._run(settings)

        self.assertNotIn(
            "bot.telegram_bot",
            sys.modules,
            "headless must not even import the module that builds the Application",
        )
        self.assertNotIn("bot.message_queue", sys.modules)
        self.assertEqual(FakeBot.instances, [], "no SwingTraderBot may be constructed")
        self.assertEqual(FakeMessageQueue.instances, [])
        self.assertIsNotNone(harness.pipeline)

    def test_headless_starts_the_scheduler_and_every_monitor(self):
        settings = FakeSettings(telegram_enabled=False, **email_overrides())

        harness = self._run(settings)

        self.assertTrue(harness.scheduler.started)
        self.assertTrue(harness.scheduler.enable_scans)
        self.assertEqual(len(harness.monitors), 3, "order monitor, position monitor, watchdog")
        for monitor in harness.monitors:
            self.assertTrue(monitor.started, monitor.args)

    def test_headless_shutdown_stops_everything_it_started(self):
        settings = FakeSettings(telegram_enabled=False, **email_overrides())

        harness = self._run(settings)

        for monitor in harness.monitors:
            self.assertTrue(monitor.stopped)
        self.assertTrue(harness.scheduler.stopped)

    def test_the_scheduler_still_gets_a_graceful_restart_callback(self):
        # The daily pre-market self-restart is what keeps the container fresh;
        # it must survive the loss of the thing it used to stop last.
        settings = FakeSettings(telegram_enabled=False, **email_overrides())

        harness = self._run(settings)

        self.assertIsNotNone(harness.scheduler.restart_callback)
        asyncio.run(harness.scheduler.restart_callback())  # must not raise with no bot

    def test_the_pipeline_gets_a_notify_backed_manager(self):
        settings = FakeSettings(telegram_enabled=False, **email_overrides())

        harness = self._run(settings)

        from bot.notifications import NotifySink

        manager = harness.pipeline.notification_manager
        self.assertIsInstance(manager.sink, NotifySink)
        self.assertFalse(manager.telegram)
        self.assertIsNone(manager.mq)

    def test_the_approval_poller_starts_when_both_flags_are_on(self):
        settings = FakeSettings(
            telegram_enabled=False,
            phase6_execution_enabled=True,
            owner_action_poller_enabled=True,
            owner_id="owner-1",
            execution_approval_secret="secret",
            **email_overrides(),
        )

        harness = self._run(settings)

        self.assertEqual(len(harness.pollers), 1)
        self.assertTrue(harness.poller.started)
        self.assertTrue(harness.poller.stopped)
        self.assertIs(
            harness.poller.kwargs["execution_service"], harness.execution_services[0]
        )
        self.assertIs(
            harness.poller.kwargs["strategy_execution_service"],
            harness.strategy_services[0],
        )

    def test_no_poller_when_its_own_flag_is_off(self):
        settings = FakeSettings(
            telegram_enabled=False,
            phase6_execution_enabled=True,
            owner_id="owner-1",
            execution_approval_secret="secret",
            **email_overrides(),
        )

        harness = self._run(settings)

        self.assertEqual(harness.pollers, [])

    def test_headless_registers_the_email_card_sender_for_proposals(self):
        from portfolio import approvals as approvals_mod

        settings = FakeSettings(
            telegram_enabled=False,
            phase6_execution_enabled=True,
            owner_id="owner-1",
            execution_approval_secret="secret",
            **email_overrides(),
        )

        self._run(settings)

        self.assertTrue(approvals_mod.has_card_sender())
        sender = approvals_mod._SENDER
        self.assertEqual(type(sender).__name__, "EmailCardSender")
        self.assertEqual(sender.approval_route, "mcp")


class HeadlessRefusalTests(unittest.TestCase):
    """The one thing headless refuses to start without."""

    def setUp(self):
        from portfolio import approvals as approvals_mod

        self.addCleanup(approvals_mod.clear_card_sender)

    def test_phase6_headless_with_no_owner_id_refuses_to_start(self):
        settings = FakeSettings(
            telegram_enabled=False,
            phase6_execution_enabled=True,
            owner_id="",
            telegram_chat_id="",
            **email_overrides(),
        )

        with patched_main(settings, patch_telegram=False) as harness:
            with self.assertRaises(SystemExit) as caught:
                asyncio.run(run_main(harness))

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(harness.pipelines, [], "it must refuse before building anything")

    def test_a_leftover_chat_id_still_resolves_an_owner(self):
        # The switch-over leaves TELEGRAM_CHAT_ID set so it stays reversible.
        # An approval minted before the switch was bound to that value, so it is
        # a legitimate owner id and the refusal must not fire on it.
        settings = FakeSettings(
            telegram_enabled=False,
            phase6_execution_enabled=True,
            owner_id="",
            telegram_chat_id="987654321",
            execution_approval_secret="secret",
            **email_overrides(),
        )

        with patched_main(settings, patch_telegram=False) as harness:
            asyncio.run(run_main(harness))

        self.assertEqual(len(harness.pipelines), 1)

    def test_headless_does_not_require_a_telegram_token(self):
        settings = FakeSettings(telegram_enabled=False, telegram_bot_token="", telegram_chat_id="")

        with patched_main(settings, patch_telegram=False) as harness:
            asyncio.run(run_main(harness))

        self.assertEqual(len(harness.pipelines), 1)

    def test_telegram_mode_still_requires_the_token(self):
        settings = FakeSettings(telegram_enabled=True, telegram_bot_token="", telegram_chat_id="")

        with patched_main(settings) as harness:
            with self.assertRaises(SystemExit) as caught:
                asyncio.run(run_main(harness))

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(harness.pipelines, [])


class TelegramModeUnchangedTests(unittest.TestCase):
    """The flag defaulted on: today's wiring, unchanged."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SCHEDULER_ENABLED": "true"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        from portfolio import approvals as approvals_mod
        from research_workspace import paging as research_paging
        from utils import billing_alerts

        self.addCleanup(approvals_mod.clear_card_sender)
        self.addCleanup(billing_alerts.register, None, None)
        self.addCleanup(research_paging.register, None, None)

    def _settings(self, **overrides):
        base = dict(
            telegram_enabled=True,
            telegram_bot_token="1234567890:TTTTTTTTTTTTTTTTTTTT",
            telegram_chat_id="987654321",
        )
        base.update(overrides)
        return FakeSettings(**base)

    def test_the_bot_is_built_started_and_stopped(self):
        with patched_main(self._settings()) as harness:
            asyncio.run(run_main(harness))

        self.assertEqual(len(FakeBot.instances), 1)
        built = FakeBot.instances[0]
        self.assertTrue(built.started)
        self.assertTrue(built.stopped)
        self.assertEqual(len(FakeMessageQueue.instances), 1)
        self.assertIsInstance(harness.pipeline.notification_manager.mq, FakeMessageQueue)

    def test_the_manager_still_sinks_to_the_queue(self):
        from bot.notifications import TelegramSink

        with patched_main(self._settings()) as harness:
            asyncio.run(run_main(harness))

        manager = harness.pipeline.notification_manager
        self.assertIsInstance(manager.sink, TelegramSink)
        self.assertTrue(manager.telegram)
        self.assertEqual(manager.chat_id, "987654321")

    def test_bot_data_is_populated_from_the_runtime_container(self):
        settings = self._settings(
            phase6_execution_enabled=True,
            owner_id="owner-1",
            execution_approval_secret="secret",
        )

        with patched_main(settings) as harness:
            asyncio.run(run_main(harness))

        bot_data = FakeBot.instances[-1].app.bot_data
        self.assertIn("execution_service", bot_data)
        self.assertIn("strategy_execution_service", bot_data)
        self.assertIn("strategy_lab_adapters", bot_data)
        self.assertIn("pipeline", bot_data)
        self.assertIs(bot_data["execution_service"], harness.execution_services[0])

        from orchestrator.runtime import RuntimeContainer

        self.assertIsInstance(bot_data["runtime"], RuntimeContainer)
        self.assertIs(bot_data["runtime"].execution_service, bot_data["execution_service"])


class ChannelReportingTests(unittest.TestCase):
    """The startup line that says who will actually be told anything."""

    def test_telegram_mode_names_telegram_first(self):
        import main as main_module

        settings = FakeSettings(
            telegram_enabled=True,
            telegram_bot_token="1234567890:TTTTTTTTTTTTTTTTTTTT",
            telegram_chat_id="987654321",
            **email_overrides(),
        )

        self.assertEqual(main_module._channel_names(settings, True), ["telegram", "email"])

    def test_headless_never_names_telegram_even_with_credentials_set(self):
        import main as main_module

        settings = FakeSettings(
            telegram_enabled=False,
            telegram_bot_token="1234567890:TTTTTTTTTTTTTTTTTTTT",
            telegram_chat_id="987654321",
            **email_overrides(),
        )

        self.assertEqual(main_module._channel_names(settings, False), ["email"])

    def test_headless_with_nothing_configured_reports_no_channel(self):
        import main as main_module

        settings = FakeSettings(telegram_enabled=False)

        self.assertEqual(main_module._channel_names(settings, False), [])


if __name__ == "__main__":
    unittest.main()
