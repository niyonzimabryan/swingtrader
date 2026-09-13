"""The approval card as an email, for both processes.

``portfolio.approvals.ApprovalCard`` is minted in two places — the workspace
(``propose_order``) and the bot (a deployment where the bot hosts the proposal
path) — and both want the same HTML email. Neither may import the other
(``tests/test_no_execute_scope.py`` asserts both directions), so the shared
sender lives here, where both can reach it.

What it deliberately does **not** do: carry an approval. The email has no
callback, and the card page it links to is read-only. Approval stays a signed,
expiring, single-use, owner-bound decision made out of band — the Telegram
callback handled in the bot process (Spec L §6.3), or, in a headless deployment,
the `approve_order` MCP owner tool, which *records* the decision for the runtime
poller to act on (Spec K §10). Email adds a way to *see* a proposal, never a way
to release one, and ``approval_route`` only decides which of the two the card
tells the reader to use.
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("notify_approval")


def chart_for(ticker: str, *, settings, session_factory=None) -> dict | None:
    """The last N daily bars for ``ticker``, captured into the card payload.

    Read **once**, when the card is minted, and stored with the card: the page
    re-renders the chart from these bars forever, so an email and the page it
    links to can never disagree about what the price did (see
    ``notify/cards/chart.py``).

    Returns ``None`` when the price plane has nothing for the name, which is a
    card without a chart rather than an error — and the card says so.
    """
    try:
        from database.models import PriceBar

        limit = int(getattr(settings, "card_chart_sessions", 60) or 60)
        factory = session_factory
        if factory is None:
            from database.db import get_session

            factory = get_session
        with factory() as session:
            rows = (
                session.query(PriceBar)
                .filter(PriceBar.ticker == str(ticker).upper())
                .order_by(PriceBar.session_date.desc())
                .limit(limit)
                .all()
            )
            bars = [
                {"date": row.session_date.isoformat(), "close": float(row.split_adjusted_close)}
                for row in reversed(rows)
            ]
    except Exception as exc:  # pragma: no cover - database/price-plane specific
        log.warning("card_chart_bars_unavailable", ticker=ticker, error=str(exc))
        return None
    if not bars:
        return None
    symbol = str(ticker).upper()
    return {
        "ticker": symbol,
        "title": f"{symbol} — last {len(bars)} sessions",
        "bars": bars,
        "as_of_note": (
            f"split-adjusted closes through {bars[-1]['date']}, from the stored price plane"
        ),
        "caption": (
            f"Split-adjusted closes, {bars[0]['date']} to {bars[-1]['date']}. Drawn from the "
            "bars stored with this card, so this page will always show what the email showed."
        ),
    }


def levels_from(proposal: dict) -> dict:
    """``entry``/``stop``/``target`` for the chart, read off the proposal row.

    ``target`` is absent unless the row carries one: Spec L §6 gives a proposal
    an entry and a protective stop, and drawing a target the system never chose
    would be inventing a number on a card (AGENTS.md §1.2).
    """
    levels = {}
    for name in ("entry", "stop", "target"):
        value = (proposal or {}).get(name)
        if value is None:
            continue
        try:
            levels[name] = float(value)
        except (TypeError, ValueError):
            continue
    return levels


class EmailCardSender:
    """Renders an ``ApprovalCard`` as an HTML email with a linked page.

    The card's ``detail`` is ``portfolio.proposals.proposal_payload(row)``, which
    is the same dict the MCP response carries — so the email, the page and the
    tool answer print the same numbers because they read the same object, not
    because three renderers were kept in step by hand.

    Never raises: ``portfolio.approvals.send_card``'s rule is that a channel
    failure must not lose the row, and this is a channel.
    """

    def __init__(
        self,
        settings,
        *,
        session_factory=None,
        channels=None,
        approval_route: str = "telegram",
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.channels = channels
        #: ``"telegram"`` or ``"mcp"`` — see ``notify.cards.proposal``. It
        #: changes the card's closing note and, headless, prints the owner-tool
        #: call with this proposal's uid in it. It changes nothing about what
        #: can approve: this sender delivers, and delivering is all it does.
        self.approval_route = str(approval_route or "telegram")

    def __call__(self, card) -> None:
        try:
            from notify.cards import deliver
            from notify.cards.proposal import build_payload
            from notify.context import proposal_context

            proposal = dict(getattr(card, "detail", None) or {})
            chart = chart_for(
                card.ticker, settings=self.settings, session_factory=self.session_factory
            )
            if chart:
                chart["levels"] = levels_from(proposal)
            payload = build_payload(
                proposal=proposal,
                approvable=bool(getattr(card, "approvable", False)),
                approval_route=self.approval_route,
                created_at_utc=utcnow_naive().isoformat(),
                chart=chart,
                # Best-effort, and absent rather than invented when it is not
                # there: the cited cohort answer with its warnings, the active
                # thesis with its invalidators, and the stored bear case.
                **proposal_context(
                    proposal, settings=self.settings, session_factory=self.session_factory
                ),
            )
            deliver(
                payload,
                settings=self.settings,
                channels=self.channels,
                session_factory=self.session_factory,
            )
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error(
                "proposal_card_email_failed",
                proposal_id=getattr(card, "proposal_id", None),
                error=str(exc),
            )


class FanOutCardSender:
    """Calls every registered card sender. One failure never stops the others.

    ``portfolio.approvals.send_card`` already guards the outer call, but it
    guards it *once*: without this, a Telegram timeout would swallow the email
    that was meant to follow it. A card that reached one channel and not the
    other is a much better outcome than one that reached neither.
    """

    def __init__(self, senders):
        self.senders = list(senders)

    def __call__(self, card) -> None:
        for sender in self.senders:
            try:
                sender(card)
            except Exception as exc:  # pragma: no cover - sender-specific
                log.error(
                    "proposal_card_sender_failed",
                    proposal_id=getattr(card, "proposal_id", None),
                    sender=type(sender).__name__,
                    error=str(exc),
                )


def register_email_card_sender(
    settings, *, session_factory=None, channels=None, approval_route: str = "mcp"
) -> bool:
    """Make the email card the *only* proposal channel. Returns whether it was.

    The headless counterpart to ``bot.handlers.proposals.register_bot_card_sender``,
    and it lives here rather than there because ``bot/handlers/proposals.py``
    imports ``telegram`` at module scope — importing it to register a channel
    that has nothing to do with Telegram would be the one import a headless
    process most obviously should not make.

    Returns ``False`` and registers nothing when no channel is configured. A
    proposal whose card could not be delivered stays ``proposed`` and simply
    cannot be approved, which is the safe direction (``portfolio.approvals``);
    registering a sender that silently drops every card would hide that.
    """
    from notify.registry import email_configured
    from portfolio import approvals as approvals_mod

    usable, missing = email_configured(settings)
    if not usable:
        log.warning(
            "proposal_card_email_unavailable_headless",
            missing=",".join(missing),
            note=(
                "Telegram is off and the email channel is not configured, so a "
                "proposal card has nowhere to go; proposals will be logged only "
                "and cannot be approved. See docs/NOTIFICATIONS.md."
            ),
        )
        return False
    approvals_mod.register_card_sender(
        EmailCardSender(
            settings,
            session_factory=session_factory,
            channels=channels,
            approval_route=approval_route,
        )
    )
    log.info("proposal_card_email_registered_headless", approval_route=approval_route)
    return True
