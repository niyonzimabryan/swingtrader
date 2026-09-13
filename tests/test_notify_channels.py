"""The channels and the registry: what goes on the wire, and what gets logged.

No network. Both transports are injected, so these assert the exact request
body the provider would receive rather than that something was attempted.

The rows asserted here:

* the Resend request body — from, to, subject, text, html, tags;
* the Authorization header carries the key as a bearer;
* a 4xx, a transport exception and a missing configuration each fail closed,
  return ``False``, and write a ``failed``/``skipped`` row;
* the Telegram payload is unchanged, inline keyboard included;
* ``configured_channels`` for every flag combination;
* ``broadcast`` survives a channel that raises, and reports ``{}`` with none.
"""

from __future__ import annotations

import unittest

from database.db import get_session, init_db
from database.models import NotificationSend
from notify import registry, store
from notify.channel import KIND_PAGE, KIND_PROPOSAL, Notification
from notify.resend import ResendChannel, parse_recipients
from notify.telegram import TELEGRAM_MSG_LIMIT, CallableChannel, TelegramChannel
from tests import notifyfixture as nf
from tests.dbfixture import TestDatabase


def a_notification(**overrides) -> Notification:
    base = dict(
        kind=KIND_PROPOSAL,
        subject="[approval needed] AMD — proposal 42",
        text="AMD  [awaiting approval]\n  entry: $108.00\n",
        html="<html><body>AMD</body></html>",
        ref="11111111-2222-3333-4444-555555555555",
        detail={"card_uid": "abc123", "card_url": "https://workspace.example.invalid/cards/abc123?s=x"},
    )
    base.update(overrides)
    return Notification(**base)


class ChannelDatabaseTests(unittest.TestCase):
    """Every test here writes a delivery row, so each gets its own database."""

    def setUp(self):
        self.db = TestDatabase("notify")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)

    def sends(self) -> list[dict]:
        """Delivery rows as plain dicts, read inside the session.

        Detached ORM instances raise on attribute access after the session
        closes; a dict is what these assertions actually want anyway.
        """
        with get_session() as session:
            return [
                {
                    "kind": row.kind,
                    "ref": row.ref,
                    "channel": row.channel,
                    "status": row.status,
                    "provider_id": row.provider_id,
                    "error": row.error,
                    "card_uid": row.card_uid,
                }
                for row in session.query(NotificationSend).order_by(NotificationSend.id).all()
            ]


class ResendChannelTests(ChannelDatabaseTests):
    def _channel(self, transport):
        return ResendChannel(
            api_key=nf.FAKE_RESEND_KEY,
            sender="swingtrader@example.invalid",
            recipients="owner@example.invalid",
            transport=transport,
        )

    def test_the_request_body_is_exactly_what_resend_expects(self):
        transport = nf.RecordingTransport(body={"id": "re-1"})
        self.assertTrue(self._channel(transport).send(a_notification()))

        call = transport.last
        self.assertEqual(call["url"], "https://api.resend.com/emails")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {nf.FAKE_RESEND_KEY}")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        payload = call["payload"]
        self.assertEqual(payload["from"], "swingtrader@example.invalid")
        self.assertEqual(payload["to"], ["owner@example.invalid"])
        self.assertEqual(payload["subject"], "[approval needed] AMD — proposal 42")
        self.assertIn("AMD", payload["text"])
        self.assertIn("<html>", payload["html"])
        self.assertEqual(
            payload["tags"],
            [
                {"name": "kind", "value": "proposal"},
                {"name": "ref", "value": "11111111-2222-3333-4444-555555555555"},
            ],
        )

    def test_a_text_only_notification_carries_no_html_key(self):
        transport = nf.RecordingTransport()
        self._channel(transport).send(a_notification(html=""))
        self.assertNotIn("html", transport.last["payload"])
        self.assertIn("text", transport.last["payload"])

    def test_a_successful_send_records_the_provider_id(self):
        self._channel(nf.RecordingTransport(body={"id": "re-42"})).send(a_notification())
        (row,) = self.sends()
        self.assertEqual(row["status"], store.SENT)
        self.assertEqual(row["channel"], "email")
        self.assertEqual(row["kind"], KIND_PROPOSAL)
        self.assertEqual(row["provider_id"], "re-42")
        self.assertEqual(row["card_uid"], "abc123")
        self.assertEqual(row["error"], "")

    def test_a_4xx_fails_closed_and_records_why(self):
        transport = nf.RecordingTransport(status=422, body={"message": "domain not verified"})
        self.assertFalse(self._channel(transport).send(a_notification()))
        (row,) = self.sends()
        self.assertEqual(row["status"], store.FAILED)
        self.assertIn("422", row["error"])
        self.assertIn("domain not verified", row["error"])

    def test_a_transport_exception_never_escapes(self):
        transport = nf.RecordingTransport(raises=RuntimeError("connection reset"))
        self.assertFalse(self._channel(transport).send(a_notification()))
        (row,) = self.sends()
        self.assertEqual(row["status"], store.FAILED)
        self.assertIn("connection reset", row["error"])

    def test_an_unconfigured_channel_skips_rather_than_posting(self):
        transport = nf.RecordingTransport()
        channel = ResendChannel(
            api_key="", sender="swingtrader@example.invalid",
            recipients="owner@example.invalid", transport=transport,
        )
        self.assertFalse(channel.send(a_notification()))
        self.assertEqual(transport.calls, [])
        (row,) = self.sends()
        self.assertEqual(row["status"], store.SKIPPED)

    def test_a_tag_value_is_sanitised_rather_than_rejected_by_resend(self):
        transport = nf.RecordingTransport()
        self._channel(transport).send(a_notification(kind="page", ref="sync failed: AMD"))
        values = {tag["name"]: tag["value"] for tag in transport.last["payload"]["tags"]}
        self.assertEqual(values["ref"], "sync_failed__AMD")


class ResendRecipientTests(unittest.TestCase):
    def test_a_comma_separated_list_becomes_a_list(self):
        self.assertEqual(
            parse_recipients("a@x.invalid, b@x.invalid ;c@x.invalid"),
            ["a@x.invalid", "b@x.invalid", "c@x.invalid"],
        )

    def test_duplicates_and_blanks_are_dropped_and_order_is_kept(self):
        self.assertEqual(parse_recipients("b@x, , a@x, b@x"), ["b@x", "a@x"])

    def test_none_is_no_recipients(self):
        self.assertEqual(parse_recipients(None), [])


class TelegramChannelTests(ChannelDatabaseTests):
    def _channel(self, transport):
        return TelegramChannel(
            bot_token=nf.FAKE_TELEGRAM_TOKEN, chat_id=nf.FAKE_CHAT_ID, transport=transport
        )

    def test_the_payload_is_the_same_request_the_bot_api_always_got(self):
        transport = nf.RecordingTransport(body={"ok": True, "result": {"message_id": 7}})
        keyboard = {"inline_keyboard": [[{"text": "✅", "callback_data": "p6ok:42:9f2c"}]]}
        notification = a_notification(
            detail={
                "card_uid": "abc123",
                "telegram": {
                    "text": "*Proposal 42 — AMD long*",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            }
        )
        self.assertTrue(self._channel(transport).send(notification))

        payload = transport.last["payload"]
        self.assertEqual(payload["chat_id"], nf.FAKE_CHAT_ID)
        self.assertEqual(payload["text"], "*Proposal 42 — AMD long*")
        self.assertEqual(payload["parse_mode"], "Markdown")
        self.assertEqual(payload["reply_markup"], keyboard)
        self.assertIn(nf.FAKE_TELEGRAM_TOKEN, transport.last["url"])

    def test_without_an_override_it_sends_the_plain_text_and_no_keyboard(self):
        transport = nf.RecordingTransport(body={"ok": True, "result": {"message_id": 8}})
        self._channel(transport).send(a_notification(kind=KIND_PAGE))
        payload = transport.last["payload"]
        self.assertNotIn("reply_markup", payload)
        self.assertIn("AMD", payload["text"])

    def test_an_over_long_body_is_truncated_and_says_so(self):
        transport = nf.RecordingTransport(body={"ok": True, "result": {"message_id": 9}})
        self._channel(transport).send(a_notification(text="x" * (TELEGRAM_MSG_LIMIT + 500)))
        text = transport.last["payload"]["text"]
        self.assertLessEqual(len(text), TELEGRAM_MSG_LIMIT)
        self.assertIn("truncated", text)

    def test_ok_false_is_a_failure_even_on_http_200(self):
        transport = nf.RecordingTransport(
            status=200, body={"ok": False, "description": "chat not found"}
        )
        self.assertFalse(self._channel(transport).send(a_notification()))
        (row,) = self.sends()
        self.assertEqual(row["status"], store.FAILED)
        self.assertIn("chat not found", row["error"])

    def test_a_callable_channel_records_a_send_and_swallows_a_raise(self):
        seen = []
        ok = CallableChannel(lambda text, markup: seen.append((text, markup)))
        self.assertTrue(ok.send(a_notification()))
        self.assertEqual(len(seen), 1)

        def explode(text, markup):
            raise RuntimeError("queue is closed")

        self.assertFalse(CallableChannel(explode).send(a_notification()))
        statuses = [row["status"] for row in self.sends()]
        self.assertEqual(statuses, [store.SENT, store.FAILED])


class RegistryTests(unittest.TestCase):
    """Every flag combination, and what ``broadcast`` does with each."""

    def names(self, settings) -> list[str]:
        return [channel.name for channel in registry.configured_channels(settings)]

    def test_nothing_configured_is_no_channel(self):
        self.assertEqual(self.names(nf.FakeSettings()), [])

    def test_telegram_alone(self):
        self.assertEqual(self.names(nf.telegram_settings()), ["telegram"])

    def test_email_alone(self):
        self.assertEqual(self.names(nf.email_settings()), ["email"])

    def test_both(self):
        settings = nf.email_settings(
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN, telegram_chat_id=nf.FAKE_CHAT_ID
        )
        self.assertEqual(self.names(settings), ["email", "telegram"])

    def test_email_enabled_but_unconfigured_builds_no_email_channel(self):
        settings = nf.email_settings(resend_api_key="", pager_email_to="")
        settings.telegram_bot_token = nf.FAKE_TELEGRAM_TOKEN
        settings.telegram_chat_id = nf.FAKE_CHAT_ID
        self.assertEqual(self.names(settings), ["telegram"])
        usable, missing = registry.email_configured(settings)
        self.assertFalse(usable)
        self.assertEqual(missing, ["RESEND_API_KEY", "PAGER_EMAIL_TO"])

    def test_the_flag_off_names_the_flag_as_what_is_missing(self):
        usable, missing = registry.email_configured(nf.FakeSettings())
        self.assertFalse(usable)
        self.assertEqual(missing, ["NOTIFY_EMAIL_ENABLED"])

    def test_email_channels_excludes_telegram(self):
        settings = nf.email_settings(
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN, telegram_chat_id=nf.FAKE_CHAT_ID
        )
        self.assertEqual([c.name for c in registry.email_channels(settings)], ["email"])

    def test_broadcast_reaches_every_channel_and_reports_each(self):
        first = nf.RecordingChannel("email")
        second = nf.RecordingChannel("telegram", result=False)
        results = registry.broadcast(a_notification(), channels=[first, second])
        self.assertEqual(results, {"email": True, "telegram": False})
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(second.sent), 1)

    def test_a_channel_that_raises_does_not_stop_the_others(self):
        good = nf.RecordingChannel("email")
        results = registry.broadcast(
            a_notification(), channels=[nf.ExplodingChannel(), good]
        )
        self.assertEqual(results, {"exploding": False, "email": True})
        self.assertEqual(len(good.sent), 1)

    def test_no_channel_is_an_empty_result_rather_than_a_raise(self):
        self.assertEqual(registry.broadcast(a_notification(), channels=[]), {})


if __name__ == "__main__":
    unittest.main()
