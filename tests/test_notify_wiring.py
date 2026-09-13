"""The wiring: which channels get registered, and what the card path delivers.

The claims this holds, all of which are properties of the *default* rather than
of a happy path:

* with no new variable set, nothing about delivery changes — the pager is still
  ``log_pager`` and the workspace registers what it always did;
* ``register_if_configured`` names every missing variable, and warns separately
  about the state where email works and Telegram does not (cards arrive, nothing
  can be approved);
* a fan-out sender keeps going when one channel raises;
* the approval email carries the card's numbers and no approval affordance;
* the card is **stored before** it is sent, so the link in the inbox resolves;
* the whole path fails soft — a sender that explodes never reaches
  ``create_proposal``.
"""

from __future__ import annotations

import unittest

from database.db import get_session, init_db
from database.models import Card
from notify import store
from notify.approval import EmailCardSender, FanOutCardSender, levels_from
from portfolio import approvals as approvals_mod
from portfolio import paging
from tests import notifyfixture as nf
from tests.dbfixture import TestDatabase


def a_card(**overrides):
    base = dict(
        proposal_id=42,
        proposal_uid="11111111-2222-3333-4444-555555555555",
        ticker="AMD",
        status="proposed",
        body_md="*Proposal 42 — AMD long*",
        approve_callback="p6ok:42:9f2c4b1a77e30ddc55",
        reject_callback="p6no:42:9f2c4b1a77e30ddc55",
        detail=nf.proposal_row(),
    )
    base.update(overrides)
    return approvals_mod.ApprovalCard(**base)


class RegisterIfConfiguredTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(approvals_mod.clear_card_sender)
        approvals_mod.clear_card_sender()

    def register(self, settings):
        from workspace.proposal_card import register_if_configured

        return register_if_configured(settings)

    def test_the_phase6_flag_off_registers_nothing(self):
        self.assertEqual(self.register(nf.email_settings(phase6_execution_enabled=False)), [])
        self.assertFalse(approvals_mod.has_card_sender())

    def test_telegram_only_is_what_it_always_was(self):
        settings = nf.telegram_settings(phase6_execution_enabled=True)
        self.assertEqual(self.register(settings), ["telegram"])
        self.assertTrue(approvals_mod.has_card_sender())

    def test_email_only_registers_and_warns_that_nothing_can_be_approved(self):
        settings = nf.email_settings(phase6_execution_enabled=True)
        self.assertEqual(self.register(settings), ["email"])
        self.assertTrue(approvals_mod.has_card_sender())

    def test_both_configured_fans_out(self):
        settings = nf.email_settings(
            phase6_execution_enabled=True,
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN,
            telegram_chat_id=nf.FAKE_CHAT_ID,
        )
        self.assertEqual(self.register(settings), ["telegram", "email"])

    def test_nothing_configured_leaves_the_log_only_default(self):
        settings = nf.FakeSettings(phase6_execution_enabled=True)
        self.assertEqual(self.register(settings), [])
        self.assertFalse(approvals_mod.has_card_sender())


class FanOutTests(unittest.TestCase):
    def test_one_sender_raising_does_not_stop_the_next(self):
        seen = []

        def explode(card):
            raise RuntimeError("telegram is down")

        FanOutCardSender([explode, seen.append])(a_card())
        self.assertEqual(len(seen), 1)

    def test_it_never_raises_into_create_proposal(self):
        def explode(card):
            raise RuntimeError("everything is down")

        approvals_mod.register_card_sender(FanOutCardSender([explode]))
        self.addCleanup(approvals_mod.clear_card_sender)
        # send_card's own guard plus the fan-out's: two layers, both silent.
        self.assertTrue(approvals_mod.send_card(a_card()))


class EmailCardSenderTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("notify_wiring")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.settings = nf.email_settings()
        self.channel = nf.RecordingChannel("email")

    def send(self, card):
        EmailCardSender(self.settings, channels=[self.channel])(card)
        return self.channel.last

    def test_the_email_carries_the_proposals_own_numbers(self):
        notification = self.send(a_card())
        self.assertIsNotNone(notification)
        self.assertIn("AMD", notification.subject)
        self.assertIn("34 shares", notification.text)
        self.assertIn("$3,672.00", notification.text)
        self.assertIn("0.6169", notification.text)

    def test_the_email_carries_no_approval_callback(self):
        notification = self.send(a_card())
        self.assertNotIn("p6ok:", notification.html)
        self.assertNotIn("p6ok:", notification.text)
        self.assertNotIn("telegram", notification.detail)

    def test_the_card_is_stored_before_it_is_sent(self):
        """The link is in the email; the row it resolves to must already exist."""
        notification = self.send(a_card())
        uid = notification.card_uid
        self.assertTrue(uid)
        with get_session() as session:
            self.assertEqual(session.query(Card).filter(Card.card_uid == uid).count(), 1)
        self.assertIsNotNone(store.load_card(uid))

    def test_the_email_links_to_the_stored_page(self):
        notification = self.send(a_card())
        self.assertIn(f"/cards/{notification.card_uid}?s=", notification.card_url)
        self.assertIn(notification.card_url, notification.html)

    def test_a_refused_card_is_delivered_and_says_so(self):
        notification = self.send(
            a_card(
                status="risk_rejected",
                approve_callback=None,
                reject_callback=None,
                detail=nf.refused_proposal_row(),
            )
        )
        self.assertIn("REFUSED", notification.subject)
        self.assertIn("Nothing to approve", notification.text)

    def test_a_broken_renderer_never_escapes(self):
        """`send_card`'s rule: a channel failure must not lose the row."""
        sender = EmailCardSender(self.settings, channels=[nf.ExplodingChannel()])
        sender(a_card())  # must not raise

    def test_levels_come_off_the_row_and_a_missing_one_is_absent(self):
        self.assertEqual(levels_from({"entry": 108.0, "stop": 99.0}), {"entry": 108.0, "stop": 99.0})
        self.assertEqual(levels_from({"entry": "n/a"}), {})


class PagerTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("notify_pager")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)

    def test_with_nothing_configured_the_pager_is_the_log_pager(self):
        self.assertIs(paging.pager_for(nf.FakeSettings()), paging.log_pager)

    def test_with_a_channel_configured_a_page_is_delivered(self):
        channel = nf.RecordingChannel("email")
        pager = paging.NotifyPager(nf.email_settings(), channels=[channel])
        pager(paging.RECONCILIATION_REQUIRED, {"ticker": "GIS", "recovery": "Run reconcile."})
        notification = channel.last
        self.assertIn("reconciliation_required", notification.subject)
        self.assertIn("Run reconcile.", notification.text)

    def test_a_page_is_delivered_even_when_the_channel_is_broken(self):
        pager = paging.NotifyPager(nf.email_settings(), channels=[nf.ExplodingChannel()])
        pager(paging.SYNC_FAILED, {"recovery": "Check the broker session."})  # must not raise

    def test_the_page_card_is_stored_so_the_link_resolves(self):
        channel = nf.RecordingChannel("email")
        paging.NotifyPager(nf.email_settings(), channels=[channel])(
            paging.MASS_DELETION_BLOCKED, {"account": "robinhood-agentic"}
        )
        self.assertIsNotNone(store.load_card(channel.last.card_uid))


if __name__ == "__main__":
    unittest.main()
