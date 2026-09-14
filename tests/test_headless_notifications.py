"""`NotificationManager` in both modes, and the headless proposal card.

The claim: **every** public method delivers exactly once, on whichever sink the
manager was built with, and headless never touches Telegram. "Every" is checked
by reflection against :data:`CALLS` rather than by a list somebody maintains —
a method added without a row here fails
:meth:`CoverageTests.test_every_public_method_is_exercised`, which is the point.

The second half is the proposal path: with Telegram off the email card is the
only channel a proposal has, it says approval happens through the MCP owner
tools, and it prints the uid `approve_order` takes. That is asserted end to end
— a real Strategy Lab paper dispatch, a real `create_proposal`, a real card, and
a fake Resend transport at the far end.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import unittest
from unittest import mock

from bot.notifications import NotificationManager, NotifySink, TelegramSink
from database.db import get_session
from notify import registry, testguard
from notify.channel import KIND_ALERT, KIND_SCAN_MEMO
from tests import notifyfixture as nf
from tests.dbfixture import init_test_db
from tests.headlessfixture import FakeMessageQueue

#: One plausible call per public method. Keyword-only so a signature change is a
#: loud TypeError rather than a quietly-wrong assertion.
CALLS: dict[str, dict] = {
    "order_filled": dict(
        ticker="AMD", shares=10, price=108.0, side="buy", stop_loss=99.0, position_pct=3.5
    ),
    "stop_triggered": dict(
        ticker="AMD", shares=10, entry_price=108.0, exit_price=99.0,
        pnl_pct=-8.33, pnl_abs=-90.0,
    ),
    "target_hit": dict(ticker="AMD", target_num=1, exit_price=120.0, pnl_pct=11.1, pnl_abs=120.0),
    "regime_change": dict(
        old_regime="neutral", new_regime="risk-on", reasoning="breadth improved"
    ),
    "drawdown_warning": dict(drawdown_pct=6.0, circuit_breaker_pct=10.0),
    "agent_failure": dict(agent_name="screener", error="provider 503", next_retry="in 5m"),
    "deep_research_update": dict(ticker="AMD", message="Deep research 40% complete"),
    "deep_research_started": dict(ticker="AMD", score=0.81),
    "send_deep_research_pdf": dict(ticker="AMD", pdf_path=""),  # filled in per test
    "scan_complete": dict(
        scan_type="pre_market", duration_s=95.0, total_scanned=500,
        escalated=12, memos_generated=2, memo_details=nf.memo_details(),
    ),
    "system_message": dict(message="portfolio sync completed"),
    "position_stop_breached": dict(
        ticker="AMD", current_price=98.0, stop_price=99.0, pnl_pct=-9.2,
        pnl_abs=-100.0, direction="long", trade_id=7,
    ),
    "position_target_approaching": dict(
        ticker="AMD", target_num=1, current_price=118.0, target_price=120.0,
        distance_pct=1.7, pnl_pct=9.2, pnl_abs=100.0, trade_id=7,
    ),
    "position_target_hit": dict(
        ticker="AMD", target_num=1, current_price=120.0, target_price=120.0,
        pnl_pct=11.1, pnl_abs=120.0, entry_price=108.0, trade_id=7,
    ),
    "position_time_expiring": dict(
        ticker="AMD", days_held=17, max_days=20, pnl_pct=2.0, pnl_abs=20.0, trade_id=7
    ),
    "position_time_expired": dict(
        ticker="AMD", days_held=20, max_days=20, pnl_pct=2.0, pnl_abs=20.0, trade_id=7
    ),
    "position_profit_giveback": dict(
        ticker="AMD", peak_pnl_pct=14.0, current_pnl_pct=6.0, giveback_pct=8.0, trade_id=7
    ),
    "portfolio_strong_day": dict(pnl_today_pct=2.4),
    "portfolio_rough_day": dict(pnl_today_pct=-2.4),
    "portfolio_drawdown_warning": dict(drawdown_pct=6.0),
    "portfolio_circuit_breaker": dict(drawdown_pct=11.0),
    "position_big_gain": dict(ticker="AMD", pnl_pct=12.0, trade_id=7),
    "position_near_stop": dict(ticker="AMD", pnl_pct=-5.5, stop_price=99.0, trade_id=7),
    "send_report": dict(text="*Today*\nAMD: \\-2\\.1%"),
}

#: `send_report` is the Telegram half of a report whose email half is the digest
#: card the report itself sends; headless it is a deliberate no-op. Everything
#: else delivers exactly once on either sink.
HEADLESS_DELIVERIES = {"send_report": 0}


def public_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(NotificationManager, inspect.isfunction)
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }


class CoverageTests(unittest.TestCase):
    def test_every_public_method_is_exercised(self):
        self.assertEqual(
            public_methods(),
            set(CALLS),
            "a NotificationManager method was added or removed without a row in CALLS",
        )


class ManagerTestCase(unittest.TestCase):
    """A scratch database, because delivering a card writes one."""

    def setUp(self):
        self.db = init_test_db("headless_notifications")
        self.addCleanup(self.db.cleanup)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.pdf = os.path.join(scratch.name, "AMD_research.pdf")
        with open(self.pdf, "wb") as handle:
            handle.write(b"%PDF-1.4 fake report\n")

    def kwargs_for(self, name: str) -> dict:
        kwargs = dict(CALLS[name])
        if name == "send_deep_research_pdf":
            kwargs["pdf_path"] = self.pdf
        return kwargs

    def call(self, manager: NotificationManager, name: str) -> None:
        asyncio.run(getattr(manager, name)(**self.kwargs_for(name)))


class TelegramModeTests(ManagerTestCase):
    """The queue, exactly as before. No email configured, so nothing else fires."""

    def manager(self) -> NotificationManager:
        settings = nf.telegram_settings()
        queue = FakeMessageQueue(object())
        return NotificationManager(queue, nf.FAKE_CHAT_ID, settings)

    def test_the_sink_is_the_queue(self):
        self.assertIsInstance(self.manager().sink, TelegramSink)

    def test_every_method_sends_exactly_one_queue_message(self):
        for name in sorted(CALLS):
            with self.subTest(method=name):
                manager = self.manager()
                # `NotificationManager.email_card` asks the registry for the
                # email channels, and `configured_channels` builds every
                # configured channel before filtering — so with Telegram
                # configured it constructs a Telegram channel here and throws
                # it away. Inert, because this asserts the queue, not delivery.
                with testguard.allow_inert_channels():
                    self.call(manager, name)
                self.assertEqual(
                    len(manager.mq.sent), 1, f"{name} sent {manager.mq.sent}"
                )
                self.assertEqual(manager.mq.sent[0]["chat_id"], nf.FAKE_CHAT_ID)

    def test_the_wording_is_unchanged(self):
        manager = self.manager()
        self.call(manager, "order_filled")
        self.assertEqual(
            manager.mq.sent[0]["text"],
            "✅ *Order Filled*\n\n"
            "BUY `10` shares of `AMD` @ `$108.00`\n"
            "Stop\\-loss: `$99.00`\n"
            "Position: `3.5%` of portfolio",
        )

    def test_a_keyboard_still_rides_along(self):
        manager = self.manager()
        self.call(manager, "position_stop_breached")
        self.assertIsNotNone(manager.mq.sent[0]["reply_markup"])

    def test_the_pdf_still_goes_as_a_document(self):
        manager = self.manager()
        self.call(manager, "send_deep_research_pdf")
        self.assertEqual(manager.mq.sent[0]["document"], self.pdf)
        self.assertEqual(manager.mq.sent[0]["caption"], "📄 Deep Research Report: AMD")

    def test_deep_research_updates_still_bypass_markdown(self):
        manager = self.manager()
        self.call(manager, "deep_research_update")
        self.assertTrue(manager.mq.sent[0].get("plain"))


class HeadlessModeTests(ManagerTestCase):
    """The `notify/` sink. One delivery each, and no Telegram anywhere."""

    def manager(self):
        # Telegram credentials are *set* on purpose: leaving them in place is how
        # the switch stays reversible, and the manager must still not use them.
        settings = nf.email_settings(**vars(nf.telegram_settings()))
        settings.notify_email_enabled = True
        settings.resend_api_key = nf.FAKE_RESEND_KEY
        settings.pager_email_from = "swingtrader@example.invalid"
        settings.pager_email_to = "owner@example.invalid"
        channel = nf.RecordingChannel(name="email")
        manager = NotificationManager(settings=settings)
        manager.sink = NotifySink(settings, channels=[channel])
        return manager, channel

    def test_the_sink_is_notify_and_there_is_no_queue(self):
        manager, _ = self.manager()
        self.assertIsInstance(manager.sink, NotifySink)
        self.assertIsNone(manager.mq)
        self.assertFalse(manager.telegram)

    def test_every_method_delivers_exactly_once(self):
        for name in sorted(CALLS):
            with self.subTest(method=name):
                manager, channel = self.manager()
                self.call(manager, name)
                self.assertEqual(
                    len(channel.sent),
                    HEADLESS_DELIVERIES.get(name, 1),
                    f"{name} delivered {[n.subject for n in channel.sent]}",
                )

    def test_every_delivery_carries_a_subject_and_a_text_body(self):
        for name in sorted(set(CALLS) - set(HEADLESS_DELIVERIES)):
            with self.subTest(method=name):
                manager, channel = self.manager()
                self.call(manager, name)
                note = channel.last
                self.assertTrue(note.subject.strip(), f"{name} has no subject")
                self.assertTrue(note.text.strip(), f"{name} has no text part")
                self.assertTrue(note.html.strip(), f"{name} has no HTML part")

    def test_a_sample_subject_and_body(self):
        manager, channel = self.manager()
        self.call(manager, "order_filled")
        note = channel.last
        self.assertEqual(note.kind, KIND_ALERT)
        self.assertEqual(note.ref, "order_filled")
        self.assertEqual(note.subject, "[FILLED] AMD — BUY 10 @ $108.00")
        self.assertIn("BUY 10 shares @ $108.00", note.text)
        self.assertIn("stop-loss: $99.00", note.text)
        self.assertIn("3.5% of portfolio", note.text)
        # No MarkdownV2 escaping leaks into the email body.
        self.assertNotIn("\\-", note.text)

    def test_the_scan_summary_reuses_the_scan_memo_card(self):
        manager, channel = self.manager()
        self.call(manager, "scan_complete")
        self.assertEqual(channel.last.kind, KIND_SCAN_MEMO)
        self.assertIn("HIMS", channel.last.text)

    def test_the_pdf_becomes_an_attachment(self):
        manager, channel = self.manager()
        self.call(manager, "send_deep_research_pdf")
        attachments = channel.last.detail.get("attachments")
        self.assertEqual(
            attachments, [{"path": self.pdf, "filename": "AMD_deep_research.pdf"}]
        )

    def test_the_attachment_reaches_the_resend_body_as_base64(self):
        import base64

        from notify.resend import ResendChannel

        transport = nf.RecordingTransport()
        settings = nf.email_settings()
        channel = ResendChannel(
            api_key=nf.FAKE_RESEND_KEY,
            sender="swingtrader@example.invalid",
            recipients="owner@example.invalid",
            transport=transport,
        )
        manager = NotificationManager(settings=settings)
        manager.sink = NotifySink(settings, channels=[channel])
        self.call(manager, "send_deep_research_pdf")

        payload = transport.last["payload"]
        self.assertEqual(payload["attachments"][0]["filename"], "AMD_deep_research.pdf")
        self.assertEqual(
            base64.b64decode(payload["attachments"][0]["content"]),
            b"%PDF-1.4 fake report\n",
        )

    def test_an_oversized_attachment_is_dropped_and_the_email_still_sends(self):
        from notify import resend as resend_mod
        from notify.resend import ResendChannel

        transport = nf.RecordingTransport()
        settings = nf.email_settings()
        channel = ResendChannel(
            api_key=nf.FAKE_RESEND_KEY,
            sender="swingtrader@example.invalid",
            recipients="owner@example.invalid",
            transport=transport,
        )
        manager = NotificationManager(settings=settings)
        manager.sink = NotifySink(settings, channels=[channel])

        original = resend_mod.MAX_ATTACHMENT_BYTES
        resend_mod.MAX_ATTACHMENT_BYTES = 4
        try:
            self.call(manager, "send_deep_research_pdf")
        finally:
            resend_mod.MAX_ATTACHMENT_BYTES = original

        payload = transport.last["payload"]
        self.assertNotIn("attachments", payload)
        self.assertTrue(payload["subject"])

    def test_a_report_is_a_no_op_because_the_digest_card_is_the_delivery(self):
        manager, channel = self.manager()
        self.call(manager, "send_report")
        self.assertEqual(channel.sent, [])

    def test_nothing_delivers_when_no_channel_is_configured(self):
        settings = nf.FakeSettings()
        manager = NotificationManager(settings=settings)
        # No recording channel: the real lookup runs and finds nothing.
        self.call(manager, "order_filled")  # must not raise

    def test_a_channel_that_raises_does_not_unwind_the_caller(self):
        settings = nf.email_settings()
        manager = NotificationManager(settings=settings)
        manager.sink = NotifySink(settings, channels=[nf.ExplodingChannel()])
        self.call(manager, "order_filled")  # must not raise


class ChannelSelectionTests(unittest.TestCase):
    """`non_telegram_channels`: the set a headless process may use."""

    def test_telegram_is_excluded_even_when_configured(self):
        settings = nf.email_settings(
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN, telegram_chat_id=nf.FAKE_CHAT_ID
        )
        # Inert: this asserts which channels the registry builds, not delivery.
        with testguard.allow_inert_channels():
            configured = [c.name for c in registry.configured_channels(settings)]
            headless = [c.name for c in registry.non_telegram_channels(settings)]
        self.assertEqual(configured, ["email", "telegram"])
        self.assertEqual(headless, ["email"])

    def test_nothing_configured_is_an_empty_list_not_a_failure(self):
        self.assertEqual(registry.non_telegram_channels(nf.FakeSettings()), [])


class HeadlessProposalCardTests(unittest.TestCase):
    """A proposal from the paper dispatcher, delivered by email, saying `approve_order`."""

    def setUp(self):
        from tests.test_strategy_lab_paper import PaperFixture

        # Borrow the paper tournament's fixture wholesale rather than rebuild an
        # arm, a snapshot and two decisions: the point of this test is what
        # happens to the card the dispatch produces, not the dispatch.
        self.paper = PaperFixture("run")
        self.paper.setUp()
        self.addCleanup(self.paper.doCleanups)

        from portfolio import approvals as approvals_mod

        self.addCleanup(approvals_mod.clear_card_sender)

        # `send_card` runs *inside* `create_proposal`'s open write transaction,
        # and the card store opens a second connection — which on SQLite waits
        # out the busy timeout and then fails soft. Postgres, which is what
        # production runs, has no such conflict. Stubbing the two writes here
        # keeps the test about the email rather than about SQLite's locking.
        from notify import store as store_mod

        for name in ("store_card", "record_send"):
            patcher = mock.patch.object(store_mod, name, lambda **kwargs: True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings_with_email(self):
        return self.paper.settings_for(
            notify_email_enabled=True,
            resend_api_key=nf.FAKE_RESEND_KEY,
            pager_email_from="swingtrader@example.invalid",
            pager_email_to="owner@example.invalid",
            card_link_secret=nf.FAKE_CARD_SECRET,
            workspace_base_url="https://workspace.example.invalid",
        )

    def test_a_dispatched_proposal_reaches_the_email_channel(self):
        from notify.approval import register_email_card_sender
        from notify.resend import ResendChannel

        settings = self.settings_with_email()
        transport = nf.RecordingTransport()
        channel = ResendChannel(
            api_key=nf.FAKE_RESEND_KEY,
            sender="swingtrader@example.invalid",
            recipients="owner@example.invalid",
            transport=transport,
            session_factory=get_session,
        )
        registered = register_email_card_sender(
            settings, session_factory=get_session, channels=[channel]
        )
        self.assertTrue(registered)

        summary = self.paper.dispatch(settings=settings)

        self.assertEqual(summary.proposed, 2, summary.skipped)
        self.paper.assertNoOrders()
        self.assertEqual(
            len(transport.calls), 2, "one email per proposal the dispatch made"
        )

    def test_the_card_names_the_owner_tool_and_the_proposal_uid(self):
        from database import models
        from notify.approval import register_email_card_sender
        from notify.resend import ResendChannel

        settings = self.settings_with_email()
        transport = nf.RecordingTransport()
        register_email_card_sender(
            settings,
            session_factory=get_session,
            channels=[
                ResendChannel(
                    api_key=nf.FAKE_RESEND_KEY,
                    sender="swingtrader@example.invalid",
                    recipients="owner@example.invalid",
                    transport=transport,
                    session_factory=get_session,
                )
            ],
        )

        self.paper.dispatch(settings=settings)

        with get_session() as session:
            uids = [
                row.proposal_uid
                for row in session.query(models.Proposal).order_by(models.Proposal.id)
            ]
        bodies = "\n".join(call["payload"]["text"] for call in transport.calls)
        html = "\n".join(call["payload"].get("html", "") for call in transport.calls)
        for uid in uids:
            self.assertIn(f'approve_order(proposal_uid="{uid}")', bodies)
        self.assertIn("MCP owner tools", bodies)
        self.assertNotIn("Approval happens on Telegram", bodies)
        self.assertNotIn("Approval happens on Telegram", html)

    def test_registration_refuses_when_no_channel_is_configured(self):
        from portfolio import approvals as approvals_mod
        from notify.approval import register_email_card_sender

        # Nowhere for a card to go is not "register a sender that drops them".
        self.assertFalse(register_email_card_sender(self.paper.settings_for()))
        self.assertFalse(approvals_mod.has_card_sender())


if __name__ == "__main__":
    unittest.main()
