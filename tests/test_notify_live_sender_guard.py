"""The guard that stops a test reaching a real Resend or Telegram endpoint.

The incident these assert against, 2026-09-13: the full suite was run inside
the production bot container, which holds live ``RESEND_API_KEY``,
``PAGER_EMAIL_FROM``, ``PAGER_EMAIL_TO``, ``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID`` and ``NOTIFY_EMAIL_ENABLED=true``. The notification tests
took the real send path and delivered 12 real emails and Telegram messages
carrying test-fixture trade proposals to the owner's personal inbox. The test
database was a throwaway; the credentials were not.

What is asserted here:

* building a live email channel under test raises, and so does a live Telegram
  channel — at construction, before anything can be sent;
* it raises **even with the credentials present in ``os.environ``**, which is
  exactly the container's state: the guard is not a function of configuration;
* the refusal is not an ``Exception``, so ``notify``'s "a channel never raises"
  handlers cannot downgrade it into a silent ``False``;
* the opt-in seams still work — an injected transport sends as before, and
  ``allow_inert_channels`` builds real channel objects that refuse to deliver;
* ``registry.configured_channels`` with a fully configured email setting — the
  shape of the incident — refuses rather than building a live sender;
* the delivery credentials are blanked in the running test process.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from notify import registry, testguard
from notify.channel import KIND_PAGE, Notification
from notify.resend import ResendChannel, _httpx_transport as resend_httpx
from notify.telegram import TelegramChannel, _httpx_transport as telegram_httpx
from tests import notifyfixture as nf
from tests.dbfixture import TestDatabase


def a_notification() -> Notification:
    return Notification(
        kind=KIND_PAGE,
        subject="[page] guard test",
        text="body",
        ref="guard-1",
    )


def an_email_channel(**overrides) -> ResendChannel:
    kwargs = dict(
        api_key=nf.FAKE_RESEND_KEY,
        sender="swingtrader@example.invalid",
        recipients="owner@example.invalid",
    )
    kwargs.update(overrides)
    return ResendChannel(**kwargs)


def a_telegram_channel(**overrides) -> TelegramChannel:
    kwargs = dict(bot_token=nf.FAKE_TELEGRAM_TOKEN, chat_id=nf.FAKE_CHAT_ID)
    kwargs.update(overrides)
    return TelegramChannel(**kwargs)


class GuardIsArmedTests(unittest.TestCase):
    """The guard is on for the whole suite, armed by ``tests/__init__.py``."""

    def test_the_guard_is_armed(self):
        self.assertTrue(testguard.is_armed())

    def test_the_delivery_credentials_are_blank_in_this_process(self):
        for name, neutral in testguard.CREDENTIAL_ENV.items():
            self.assertEqual(os.environ.get(name), neutral, name)


class ConstructionIsRefusedTests(unittest.TestCase):
    def test_a_live_email_channel_cannot_be_built(self):
        with self.assertRaises(testguard.LiveSenderRefused) as caught:
            an_email_channel()
        message = str(caught.exception)
        self.assertIn("2026-09-13", message)
        self.assertIn("transport=", message)

    def test_a_live_telegram_channel_cannot_be_built(self):
        with self.assertRaises(testguard.LiveSenderRefused):
            a_telegram_channel()

    def test_passing_the_real_transport_explicitly_is_the_same_request(self):
        """Naming the live transport is not a way round the guard."""
        with self.assertRaises(testguard.LiveSenderRefused):
            an_email_channel(transport=resend_httpx)
        with self.assertRaises(testguard.LiveSenderRefused):
            a_telegram_channel(transport=telegram_httpx)

    def test_credentials_in_the_environment_do_not_unlock_it(self):
        """The container's state: real keys exported. It still refuses."""
        live = {
            "RESEND_API_KEY": nf.FAKE_RESEND_KEY,
            "PAGER_EMAIL_FROM": "swingtrader@example.invalid",
            "PAGER_EMAIL_TO": "owner@example.invalid",
            "TELEGRAM_BOT_TOKEN": nf.FAKE_TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": nf.FAKE_CHAT_ID,
            "NOTIFY_EMAIL_ENABLED": "true",
        }
        with mock.patch.dict(os.environ, live, clear=False):
            with self.assertRaises(testguard.LiveSenderRefused):
                an_email_channel()
            with self.assertRaises(testguard.LiveSenderRefused):
                a_telegram_channel()

    def test_the_registry_refuses_rather_than_building_a_live_sender(self):
        """``configured_channels`` on real credentials is the incident's shape."""
        settings = nf.email_settings(
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN, telegram_chat_id=nf.FAKE_CHAT_ID
        )
        with self.assertRaises(testguard.LiveSenderRefused):
            registry.configured_channels(settings)

    def test_the_refusal_is_not_an_exception_so_send_cannot_swallow_it(self):
        """``notify``'s handlers all catch ``Exception``; this must outrank them."""
        self.assertFalse(issubclass(testguard.LiveSenderRefused, Exception))
        self.assertTrue(issubclass(testguard.LiveSenderRefused, BaseException))


class OptInSeamsTests(unittest.TestCase):
    """The sending tests here write a delivery row, so each gets its own DB."""

    def setUp(self):
        from database.db import init_db

        self.db = TestDatabase("notify_guard")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)

    def test_an_injected_transport_still_builds_and_sends(self):
        transport = nf.RecordingTransport()
        channel = an_email_channel(transport=transport)
        self.assertIs(channel.transport, transport)
        self.assertTrue(channel.send(a_notification()))
        self.assertEqual(transport.last["url"], channel.api_url)
        self.assertEqual(transport.last["payload"]["to"], ["owner@example.invalid"])

    def test_an_injected_transport_still_builds_and_sends_on_telegram(self):
        transport = nf.RecordingTransport(body={"ok": True, "result": {"message_id": 7}})
        channel = a_telegram_channel(transport=transport)
        self.assertTrue(channel.send(a_notification()))
        self.assertEqual(transport.last["payload"]["chat_id"], nf.FAKE_CHAT_ID)

    def test_allow_inert_channels_builds_the_objects(self):
        with testguard.allow_inert_channels():
            channels = registry.configured_channels(nf.email_settings())
        self.assertEqual([c.name for c in channels], ["email"])

    def test_an_inert_channel_refuses_loudly_rather_than_no_opping(self):
        """A silent ``False`` would hide a test that thinks it asserted delivery."""
        with testguard.allow_inert_channels():
            channel = an_email_channel()
        with self.assertRaises(testguard.LiveSenderRefused):
            channel.send(a_notification())

    def test_the_opt_in_does_not_leak_past_its_block(self):
        with testguard.allow_inert_channels():
            an_email_channel()
        with self.assertRaises(testguard.LiveSenderRefused):
            an_email_channel()

    def test_nesting_the_opt_in_does_not_unlock_the_outer_block(self):
        with testguard.allow_inert_channels():
            with testguard.allow_inert_channels():
                an_email_channel()
            # Still inside the outer block: construction is allowed, sending is not.
            channel = an_email_channel()
        with self.assertRaises(testguard.LiveSenderRefused):
            channel.send(a_notification())


class EnvironmentNeutralisationTests(unittest.TestCase):
    """``neutralise_environment`` on a copied mapping, not the live process."""

    def test_it_blanks_every_credential_and_names_what_was_set(self):
        env = {
            "RESEND_API_KEY": "re_live_key_shaped_value",
            "TELEGRAM_BOT_TOKEN": "12345:live-shaped-token",
            "NOTIFY_EMAIL_ENABLED": "true",
            "UNRELATED": "kept",
        }
        populated = testguard.neutralise_environment(env, warn=False)

        self.assertEqual(
            sorted(populated),
            ["NOTIFY_EMAIL_ENABLED", "RESEND_API_KEY", "TELEGRAM_BOT_TOKEN"],
        )
        self.assertEqual(env["RESEND_API_KEY"], "")
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "")
        self.assertEqual(env["NOTIFY_EMAIL_ENABLED"], "false")
        self.assertEqual(env["UNRELATED"], "kept")

    def test_it_blanks_rather_than_deletes(self):
        """An unset variable falls through to ``.env``; a blank one does not."""
        env: dict[str, str] = {}
        testguard.neutralise_environment(env, warn=False)
        self.assertEqual(set(env), set(testguard.CREDENTIAL_ENV))

    def test_an_already_clean_environment_reports_nothing_populated(self):
        env = dict(testguard.CREDENTIAL_ENV)
        self.assertEqual(testguard.neutralise_environment(env, warn=False), [])


class DisarmedTests(unittest.TestCase):
    """Off, the constructors behave exactly as they did before this shipped.

    Production must be untouched by the guard, and this is the assertion that
    says so. It re-arms in ``addCleanup`` so an exploding assertion cannot leave
    the rest of the suite unguarded.
    """

    def setUp(self):
        testguard.disarm()
        self.addCleanup(testguard.arm)

    def test_the_default_transport_is_the_live_one(self):
        self.assertIs(an_email_channel().transport, resend_httpx)
        self.assertIs(a_telegram_channel().transport, telegram_httpx)


if __name__ == "__main__":
    unittest.main()
