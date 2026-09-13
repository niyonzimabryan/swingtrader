"""Human-facing delivery: channels, HTML cards, and the signed card page.

Bryan is the only reader this system has, and he reads email. Every message the
system sends a human — an approval card for a proposal, a scan memo, a Strategy
Lab scorecard, a page — is a :class:`~notify.channel.Notification` here, handed
to whichever channels are configured. Email (Resend) is one; Telegram is the
other, unchanged, and still the only channel that can carry an *approvable*
card because the callback it renders arrives in the bot process.

**Why this package is not under ``bot/``.** The workspace mints proposal cards
and pages, and the workspace's import closure may never reach ``bot/``,
``execution/`` or ``orchestrator/`` (Spec K §8, Spec L §6.1, asserted by
``tests/test_no_execute_scope.py``). So ``notify`` imports ``config``,
``database``, ``portfolio`` and ``utils`` and nothing else first-party, and both
processes import it.

Three rules hold everywhere in here:

1. **A channel never raises.** ``send`` returns a bool. A delivery failure must
   not unwind the row it was reporting — the same rule
   ``portfolio.approvals.send_card`` already states, for the same reason.
2. **The renderer computes no statistic.** Every number a card prints is read
   out of the payload it was handed, formatted, and printed with whatever
   staleness flag came with it (AGENTS.md §1.2, §1.3). ``notify/cards`` has no
   arithmetic on a reported figure anywhere in it, deliberately.
3. **Nothing here places, approves, or sizes anything.** It renders and it
   delivers. The approval callback is still Telegram's, still handled in the bot
   process, and still single-use, expiring and owner-bound.
"""

from notify.channel import (  # noqa: F401
    KIND_ALERT,
    KIND_DIGEST,
    KIND_PAGE,
    KIND_PROPOSAL,
    KIND_SCAN_MEMO,
    KIND_SCORECARD,
    Channel,
    Notification,
)
from notify.registry import broadcast, configured_channels  # noqa: F401

__all__ = [
    "Channel",
    "Notification",
    "KIND_ALERT",
    "KIND_PROPOSAL",
    "KIND_SCAN_MEMO",
    "KIND_SCORECARD",
    "KIND_PAGE",
    "KIND_DIGEST",
    "broadcast",
    "configured_channels",
]
