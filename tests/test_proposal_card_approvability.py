"""What the workspace says at startup about whether a proposal can be approved.

The startup log line is the only thing that tells the owner, before any
proposal exists, whether there is an approval route at all. Until the owner
ruling of 2026-09-13 there was exactly one route — the Telegram callback, in
the bot process — so "no Telegram" and "nothing can be approved" were the same
sentence. They are not any more: with ``WORKSPACE_OWNER_TOOLS_ENABLED`` on, the
owner approves with the ``approve_order`` MCP owner tool (Spec K §10, Spec L
§10), and the production deployment runs exactly that way — owner tools on,
Telegram off, email as the delivery channel.

So there are four cases, and the claim these tests hold is that each one is
told the truth:

* Telegram configured — approvable on the button, nothing is warned about;
* no Telegram, owner tools **on** — approvable over MCP; the stale
  ``proposal_card_not_approvable`` warning must **not** fire, because a false
  warning trains its reader to ignore the true one;
* no Telegram, owner tools **off** — genuinely not approvable; the warning
  fires and names *both* remedies;
* no channel at all — the card is only logged either way, and what the existing
  ``proposal_card_channel_unconfigured`` line says about approvability still
  depends on the owner tools.

None of this changes which channels are registered or what is delivered; these
tests assert the registration result alongside the logging so a future change
to the wording cannot quietly move delivery.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from portfolio import approvals as approvals_mod
from tests import notifyfixture as nf
from workspace import proposal_card


def _events(mock_log, level: str) -> dict[str, dict]:
    """``{event: kwargs}`` for every call made at ``level``."""
    calls = getattr(mock_log, level).call_args_list
    return {call.args[0]: call.kwargs for call in calls if call.args}


class ApprovabilityLoggingTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(approvals_mod.clear_card_sender)
        approvals_mod.clear_card_sender()

    def register(self, settings):
        """Register, returning ``(channel names, warnings, infos)``."""
        with patch.object(proposal_card, "log") as mock_log:
            names = proposal_card.register_if_configured(settings)
        return names, _events(mock_log, "warning"), _events(mock_log, "info")

    # -- Telegram configured: unchanged -------------------------------------- #

    def test_telegram_configured_says_nothing_about_approvability(self):
        settings = nf.telegram_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=False
        )
        names, warnings, infos = self.register(settings)
        self.assertEqual(names, ["telegram"])
        self.assertEqual(warnings, {})
        self.assertNotIn("proposal_card_approval_route_mcp", infos)

    def test_telegram_and_owner_tools_together_still_say_nothing(self):
        settings = nf.email_settings(
            phase6_execution_enabled=True,
            workspace_owner_tools_enabled=True,
            telegram_bot_token=nf.FAKE_TELEGRAM_TOKEN,
            telegram_chat_id=nf.FAKE_CHAT_ID,
        )
        names, warnings, _ = self.register(settings)
        self.assertEqual(names, ["telegram", "email"])
        self.assertEqual(warnings, {})

    # -- No Telegram, owner tools on: the production shape ------------------- #

    def test_owner_tools_on_is_not_warned_about(self):
        """The bug: this configuration was told nothing could be approved."""
        settings = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=True
        )
        names, warnings, infos = self.register(settings)
        self.assertEqual(names, ["email"])
        self.assertNotIn("proposal_card_not_approvable", warnings)
        self.assertEqual(warnings, {})
        self.assertIn("proposal_card_approval_route_mcp", infos)

    def test_the_mcp_route_is_named_in_the_informational_line(self):
        settings = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=True
        )
        _, _, infos = self.register(settings)
        event = infos["proposal_card_approval_route_mcp"]
        self.assertEqual(event["approval_route"], "mcp")
        self.assertIn("approve_order", event["note"])

    def test_owner_tools_on_does_not_change_what_is_delivered(self):
        """Same channel, same sender: this is about a log line, not delivery."""
        without = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=False
        )
        names_without, _, _ = self.register(without)
        approvals_mod.clear_card_sender()
        with_tools = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=True
        )
        names_with, _, _ = self.register(with_tools)
        self.assertEqual(names_without, names_with)
        self.assertTrue(approvals_mod.has_card_sender())

    # -- No Telegram, owner tools off: genuinely not approvable -------------- #

    def test_owner_tools_off_still_warns(self):
        settings = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=False
        )
        names, warnings, infos = self.register(settings)
        self.assertEqual(names, ["email"])
        self.assertIn("proposal_card_not_approvable", warnings)
        self.assertNotIn("proposal_card_approval_route_mcp", infos)

    def test_the_warning_names_both_remedies(self):
        settings = nf.email_settings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=False
        )
        _, warnings, _ = self.register(settings)
        note = warnings["proposal_card_not_approvable"]["note"]
        self.assertIn("TELEGRAM_BOT_TOKEN", note)
        self.assertIn("TELEGRAM_CHAT_ID", note)
        self.assertIn("WORKSPACE_OWNER_TOOLS_ENABLED", note)

    def test_an_absent_setting_is_read_as_off(self):
        """A settings object predating the flag must warn, not crash."""
        settings = nf.email_settings(phase6_execution_enabled=True)
        self.assertFalse(hasattr(settings, "workspace_owner_tools_enabled"))
        _, warnings, _ = self.register(settings)
        self.assertIn("proposal_card_not_approvable", warnings)

    # -- No channel at all: unchanged behaviour, honest wording -------------- #

    def test_no_channel_registers_nothing_either_way(self):
        for owner_tools in (True, False):
            with self.subTest(owner_tools=owner_tools):
                approvals_mod.clear_card_sender()
                settings = nf.FakeSettings(
                    phase6_execution_enabled=True,
                    workspace_owner_tools_enabled=owner_tools,
                )
                names, warnings, _ = self.register(settings)
                self.assertEqual(names, [])
                self.assertFalse(approvals_mod.has_card_sender())
                self.assertIn("proposal_card_channel_unconfigured", warnings)

    def test_no_channel_with_owner_tools_does_not_claim_nothing_can_be_approved(self):
        settings = nf.FakeSettings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=True
        )
        _, warnings, _ = self.register(settings)
        event = warnings["proposal_card_channel_unconfigured"]
        self.assertEqual(event["approval_route"], "mcp")
        self.assertNotIn("no proposal can be approved", event["note"].lower())
        self.assertIn("approve_order", event["note"])

    def test_no_channel_without_owner_tools_says_nothing_can_be_approved(self):
        settings = nf.FakeSettings(
            phase6_execution_enabled=True, workspace_owner_tools_enabled=False
        )
        _, warnings, _ = self.register(settings)
        event = warnings["proposal_card_channel_unconfigured"]
        self.assertEqual(event["approval_route"], "none")
        self.assertIn("no proposal can be approved", event["note"].lower())
        self.assertIn("TELEGRAM_BOT_TOKEN", event["missing"])

    # -- The Phase 6 gate is upstream of all of it --------------------------- #

    def test_phase6_off_logs_nothing_and_registers_nothing(self):
        settings = nf.email_settings(
            phase6_execution_enabled=False, workspace_owner_tools_enabled=True
        )
        names, warnings, infos = self.register(settings)
        self.assertEqual(names, [])
        self.assertEqual(warnings, {})
        self.assertEqual(infos, {})


if __name__ == "__main__":
    unittest.main()
