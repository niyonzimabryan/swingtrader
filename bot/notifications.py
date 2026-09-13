"""Proactive push notifications — order fills, stop triggers, regime changes, etc.

**One manager, two sinks.** Every method here used to end in
``await self.mq.send(self.chat_id, text)``: a Telegram message, formatted
MarkdownV2, straight onto the bot's outbound queue. With ``TELEGRAM_ENABLED``
false there is no queue and no chat, so each method now builds an :class:`Alert`
— the message in *both* forms, plus the rows a card wants — and hands it to
whichever sink the process was wired with:

``TelegramSink``
    the queue, exactly as before. Same text, same parse mode, same inline
    keyboard, same ``send_plain``/``send_document`` for the two methods that
    used them. With Telegram on, nothing about delivery changes — including the
    email cards ``#81`` added alongside it (see :meth:`email_card`), which stay
    where they were and are still the *only* email a Telegram-on deployment
    sends.
``NotifySink``
    ``notify/`` — every configured channel except Telegram (see
    ``notify.registry.non_telegram_channels``; the credentials usually stay set
    after the switch, so "not configured" is the wrong test for "do not use").
    The alert is rendered by the same card renderer everything else uses, so a
    fill and a scan memo look like they came from the same system, and the send
    is recorded in ``notifications_sent`` like every other delivery.

Two rules the split has to preserve, and does:

1. **The Telegram wording is untouched.** ``Alert.telegram_text`` is the string
   the method built before this change, character for character. The card's
   ``rows`` are the same figures formatted for a table rather than for
   MarkdownV2 — the manager formats, it never computes (AGENTS.md §1.2).
2. **A keyboard is built lazily and only by Telegram.** ``Alert.keyboard`` is a
   *callable*, so ``bot.keyboards`` — which constructs ``telegram`` objects — is
   never imported in a headless process.

Where a card kind already exists for what a method is reporting (the scan
summary is a scan-memo card, the digest and the weekly report are digest cards),
the method passes that payload through as ``Alert.card_payload`` rather than
inventing a second layout for the same thing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from bot.formatters import escape_md
from notify.channel import KIND_ALERT
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("notifications")


# --------------------------------------------------------------------------- #
# What a notification is, before a channel gets hold of it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Alert:
    """One notification, in every form a sink might need.

    ``event``
        a stable slug (``"order_filled"``, ``"position_near_stop"``). It is the
        card's ``ref`` and lands in ``notifications_sent.kind``/``ref``, so
        "was I told about this" is a query rather than a memory.
    ``telegram_text``
        the MarkdownV2 body, exactly as it was before the sinks existed.
    ``rows`` / ``body``
        the card's content. ``rows`` are ``{"label", "value"}`` dicts, already
        formatted; ``body`` is the closing sentence.
    ``keyboard``
        ``callable() -> InlineKeyboardMarkup``, or ``None``. Called by the
        Telegram sink and by nothing else.
    ``plain``
        send through ``send_plain`` (no parse mode), for the one method whose
        text arrives pre-formatted from an agent.
    ``card_payload``
        a complete ``notify.cards`` payload, when the thing being reported
        already has a card kind. Overrides ``rows``/``body`` entirely.
    """

    event: str
    subject: str
    title: str
    telegram_text: str = ""
    headline: str = ""
    verdict: dict = field(default_factory=dict)
    rows: tuple = ()
    body: str = ""
    keyboard: object = None
    plain: bool = False
    attachments: tuple = ()
    card_payload: dict | None = None
    kind: str = KIND_ALERT


def _row(label: str, value, note: str = "", tone: str = "") -> dict:
    row = {"label": label, "value": value}
    if note:
        row["note"] = note
    if tone:
        row["tone"] = tone
    return row


# --------------------------------------------------------------------------- #
# The sinks
# --------------------------------------------------------------------------- #


class TelegramSink:
    """The bot's outbound message queue. What every notification did before."""

    name = "telegram"

    def __init__(self, message_queue, chat_id: str):
        self.mq = message_queue
        self.chat_id = chat_id

    async def deliver(self, alert: Alert) -> None:
        for attachment in alert.attachments:
            await self.mq.send_document(
                self.chat_id,
                attachment.get("path", ""),
                caption=attachment.get("caption", ""),
            )
        if not alert.telegram_text:
            return
        if alert.plain:
            await self.mq.send_plain(self.chat_id, alert.telegram_text)
            return
        keyboard = alert.keyboard() if callable(alert.keyboard) else alert.keyboard
        await self.mq.send(self.chat_id, alert.telegram_text, reply_markup=keyboard)


class NotifySink:
    """``notify/`` — every configured channel that is not Telegram.

    Never raises, and never lets a delivery block the event loop: the channels
    post over HTTPS, and a scan that finished is not allowed to wait ten seconds
    on an email about it. The work goes to a thread; failures log and stop
    there, because a notification is a report about work that already happened.
    """

    name = "notify"

    def __init__(self, settings, *, channels=None, session_factory=None):
        self.settings = settings
        self._channels = channels
        self.session_factory = session_factory

    def channels(self) -> list:
        if self._channels is not None:
            return list(self._channels)
        from notify.registry import non_telegram_channels

        return non_telegram_channels(self.settings, session_factory=self.session_factory)

    def payload_for(self, alert: Alert) -> dict:
        if alert.card_payload is not None:
            return dict(alert.card_payload)
        from notify.cards.alert import build_alert_payload

        return build_alert_payload(
            event=alert.event,
            subject=alert.subject,
            title=alert.title,
            headline=alert.headline,
            verdict=alert.verdict,
            rows=alert.rows,
            body=alert.body,
            created_at_utc=utcnow_naive().isoformat(),
        )

    def send(self, alert: Alert) -> dict:
        """Blocking. Public so a test can drive it without an event loop."""
        try:
            from notify.cards import deliver

            channels = self.channels()
            if not channels:
                log.warning(
                    "notification_no_channel", event=alert.event, subject=alert.subject
                )
                return {}
            detail = {}
            if alert.attachments:
                detail["attachments"] = [
                    {"path": a.get("path", ""), "filename": a.get("filename", "")}
                    for a in alert.attachments
                ]
            return deliver(
                self.payload_for(alert),
                settings=self.settings,
                channels=channels,
                session_factory=self.session_factory,
                detail=detail or None,
            )
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error("notification_delivery_failed", event=alert.event, error=str(exc))
            return {}

    async def deliver(self, alert: Alert) -> None:
        await asyncio.to_thread(self.send, alert)


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


class NotificationManager:
    def __init__(self, message_queue=None, chat_id: str = "", settings=None, sink=None):
        self.mq = message_queue
        self.chat_id = chat_id
        self.settings = settings
        #: The sink. Given one, it is used as-is (a test, or a process that
        #: built its own); otherwise Telegram when there is a queue to send on,
        #: and ``notify/`` when there is not. The signature is unchanged and
        #: positional, so every existing construction call still means what it
        #: meant.
        self.sink = sink or (
            TelegramSink(message_queue, chat_id)
            if message_queue is not None
            else NotifySink(settings)
        )

    @property
    def telegram(self) -> bool:
        """Whether this manager delivers to Telegram. Read by the two reports."""
        return isinstance(self.sink, TelegramSink)

    async def _emit(self, alert: Alert) -> None:
        await self.sink.deliver(alert)

    # -- the email card, alongside Telegram ------------------------------- #

    def email_card(self, payload: dict) -> dict:
        """Deliver ``payload`` as an HTML email card. Telegram is untouched.

        Every caller of this has *already* sent its Telegram message through the
        queue, so this sends to the email channel only — handing it every
        configured channel would post the same digest to Telegram twice. With
        ``NOTIFY_EMAIL_ENABLED`` off there is no email channel and this is a
        no-op, which is why every call site can make it unconditionally.

        Never raises. A notification is a report about work that already
        happened; failing to send it must not fail the work.
        """
        if self.settings is None:
            return {}
        try:
            from notify.cards import deliver
            from notify.registry import email_channels

            channels = email_channels(self.settings)
            if not channels:
                return {}
            return deliver(payload, settings=self.settings, channels=channels)
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error("notification_email_card_failed", kind=payload.get("kind"), error=str(exc))
            return {}

    async def send_report(self, text: str) -> None:
        """The Telegram half of a report whose email half is a digest card.

        ``DailyDigest`` and ``WeeklyReport`` build one MarkdownV2 string and
        then call :meth:`email_card` with the digest card built from it. They
        used to reach through this object for ``self.nm.mq.send(...)``; headless
        there is no queue, and the digest card *is* the delivery. So the
        Telegram send lives here, where it can be the sink's business, and the
        two reports are unchanged in every other respect.
        """
        if not self.telegram:
            return
        await self.sink.deliver(Alert(event="report", subject="", title="", telegram_text=text))

    async def order_filled(self, ticker: str, shares: int, price: float, side: str, stop_loss: float, position_pct: float):
        """Notify operator of order fill."""
        emoji = "✅" if side == "buy" else "📤"
        text = (
            f"{emoji} *Order Filled*\n\n"
            f"{escape_md(side.upper())} `{shares}` shares of `{escape_md(ticker)}` @ `${price:,.2f}`\n"
            f"Stop\\-loss: `${stop_loss:,.2f}`\n"
            f"Position: `{position_pct:.1f}%` of portfolio"
        )
        await self._emit(
            Alert(
                event="order_filled",
                subject=f"[FILLED] {ticker} — {side.upper()} {shares} @ ${price:,.2f}",
                title=ticker,
                headline=f"{side.upper()} {shares} shares @ ${price:,.2f}",
                verdict={"label": "filled", "tone": "ok"},
                rows=(
                    _row("side", side.upper()),
                    _row("shares", shares),
                    _row("fill price", f"${price:,.2f}"),
                    _row("stop-loss", f"${stop_loss:,.2f}"),
                    _row("position", f"{position_pct:.1f}% of portfolio"),
                ),
                telegram_text=text,
            )
        )

    async def stop_triggered(self, ticker: str, shares: int, entry_price: float, exit_price: float, pnl_pct: float, pnl_abs: float):
        """Notify operator of stop-loss trigger."""
        text = (
            f"🛑 *Stop\\-Loss Triggered*\n\n"
            f"`{escape_md(ticker)}`: Sold `{shares}` shares @ `${exit_price:,.2f}`\n"
            f"Entry: `${entry_price:,.2f}` → Exit: `${exit_price:,.2f}`\n"
            f"P&L: 🔴 `{pnl_pct:+.2f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )
        await self._emit(
            Alert(
                event="stop_triggered",
                subject=f"[STOP] {ticker} — sold {shares} @ ${exit_price:,.2f}",
                title=ticker,
                headline=f"stop-loss triggered, {shares} shares out at ${exit_price:,.2f}",
                verdict={"label": "stopped out", "tone": "bad"},
                rows=(
                    _row("shares", shares),
                    _row("entry", f"${entry_price:,.2f}"),
                    _row("exit", f"${exit_price:,.2f}"),
                    _row("P&L", f"{pnl_pct:+.2f}% (${pnl_abs:+,.2f})", tone="bad"),
                ),
                telegram_text=text,
            )
        )

    async def target_hit(self, ticker: str, target_num: int, exit_price: float, pnl_pct: float, pnl_abs: float, partial: bool = False):
        """Notify operator of profit target hit."""
        text = (
            f"🎯 *Target {target_num} Hit{'  (Partial Exit)' if partial else ''}*\n\n"
            f"`{escape_md(ticker)}` @ `${exit_price:,.2f}`\n"
            f"P&L: 🟢 `{pnl_pct:+.2f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )
        await self._emit(
            Alert(
                event="target_hit",
                subject=f"[TARGET {target_num}] {ticker} @ ${exit_price:,.2f}",
                title=ticker,
                headline=f"target {target_num} hit{' (partial exit)' if partial else ''}",
                verdict={"label": f"target {target_num}", "tone": "ok"},
                rows=(
                    _row("target", target_num),
                    _row("exit", f"${exit_price:,.2f}"),
                    _row("partial exit", "yes" if partial else "no"),
                    _row("P&L", f"{pnl_pct:+.2f}% (${pnl_abs:+,.2f})", tone="ok"),
                ),
                telegram_text=text,
            )
        )

    async def regime_change(self, old_regime: str, new_regime: str, reasoning: str):
        """Notify operator of macro regime change."""
        emoji = "🟢" if new_regime == "risk-on" else "🟡" if new_regime == "neutral" else "🔴"
        text = (
            f"⚠️ *Regime Change*\n\n"
            f"`{escape_md(old_regime.upper())}` → {emoji} `{escape_md(new_regime.upper())}`\n\n"
            f"{escape_md(reasoning[:300])}"
        )
        await self._emit(
            Alert(
                event="regime_change",
                subject=f"[REGIME] {old_regime.upper()} → {new_regime.upper()}",
                title="regime change",
                headline=f"{old_regime.upper()} → {new_regime.upper()}",
                verdict={"label": new_regime.upper(), "tone": "warn"},
                rows=(_row("was", old_regime.upper()), _row("now", new_regime.upper())),
                body=reasoning[:300],
                telegram_text=text,
            )
        )

    async def drawdown_warning(self, drawdown_pct: float, circuit_breaker_pct: float):
        """Notify operator of drawdown proximity to circuit breaker."""
        text = (
            f"⚠️ *Drawdown Warning*\n\n"
            f"Portfolio drawdown: `{drawdown_pct:.1f}%` from peak\n"
            f"Circuit breaker triggers at `{circuit_breaker_pct:.1f}%`\n"
            f"Consider reducing exposure\\."
        )
        await self._emit(
            Alert(
                event="drawdown_warning",
                subject=f"[DRAWDOWN] {drawdown_pct:.1f}% from peak",
                title="drawdown warning",
                headline=f"{drawdown_pct:.1f}% from peak",
                verdict={"label": "drawdown", "tone": "warn"},
                rows=(
                    _row("drawdown", f"{drawdown_pct:.1f}% from peak", tone="warn"),
                    _row("circuit breaker", f"{circuit_breaker_pct:.1f}%"),
                ),
                body="Consider reducing exposure.",
                telegram_text=text,
            )
        )

    async def agent_failure(self, agent_name: str, error: str, next_retry: str = ""):
        """Notify operator of agent failure."""
        text = (
            f"⚠️ *Agent Failure*\n\n"
            f"Agent: `{escape_md(agent_name)}`\n"
            f"Error: {escape_md(error[:300])}\n"
        )
        if next_retry:
            text += f"Next retry: {escape_md(next_retry)}"
        rows = [_row("agent", agent_name), _row("error", error[:300], tone="bad")]
        if next_retry:
            rows.append(_row("next retry", next_retry))
        await self._emit(
            Alert(
                event="agent_failure",
                subject=f"[AGENT FAILED] {agent_name}",
                title="agent failure",
                headline=agent_name,
                verdict={"label": "failed", "tone": "bad"},
                rows=tuple(rows),
                telegram_text=text,
            )
        )

    async def deep_research_update(self, ticker: str, message: str):
        """
        Notify operator of deep research progress/completion.
        Messages come pre-formatted from DeepResearchAgent.
        """
        await self._emit(
            Alert(
                event="deep_research_update",
                subject=f"[RESEARCH] {ticker}",
                title=ticker,
                headline="deep research update",
                body=message,
                telegram_text=message,
                plain=True,
            )
        )

    async def deep_research_started(self, ticker: str, score: float):
        """Notify that deep research has been triggered."""
        text = (
            f"🔬 Deep research generating for {escape_md(ticker)}\\.\\.\\.\n"
            f"Score: `{score:.2f}` \\(threshold: 0\\.75\\)\n"
            f"Estimated time: 5\\-20 minutes"
        )
        await self._emit(
            Alert(
                event="deep_research_started",
                subject=f"[RESEARCH] {ticker} — deep research started",
                title=ticker,
                headline="deep research generating",
                rows=(
                    _row("score", f"{score:.2f}", note="threshold: 0.75"),
                    _row("estimated time", "5-20 minutes"),
                ),
                telegram_text=text,
            )
        )

    async def send_deep_research_pdf(self, ticker: str, pdf_path: str):
        """Deliver the deep research PDF: a Telegram document, or an attachment.

        **Attachment, not a link**, headless — and the choice is forced rather
        than preferred. A signed card link would have to be served by the
        workspace, and the PDF is written to the *bot* container's filesystem,
        which the workspace process cannot read: the link would 404 on every
        card. So the file travels with the message. Over
        ``notify.resend.MAX_ATTACHMENT_BYTES`` it is dropped with a log line and
        the email still arrives, because the summary is worth more than nothing.
        """
        caption = f"📄 Deep Research Report: {ticker}"
        await self._emit(
            Alert(
                event="deep_research_pdf",
                subject=f"[RESEARCH] {ticker} — deep research report",
                title=ticker,
                headline="deep research report",
                body=(
                    "The full report is attached as a PDF. It was generated by this "
                    "system's research agent; nothing in it places or approves anything."
                ),
                attachments=(
                    {"path": pdf_path, "filename": f"{ticker}_deep_research.pdf", "caption": caption},
                ),
            )
        )

    async def scan_complete(
        self,
        scan_type: str,
        duration_s: float,
        total_scanned: int,
        escalated: int,
        memos_generated: int,
        memo_details: list = None,
    ):
        """Notify operator of scan completion with summary."""
        mins = int(duration_s // 60)
        secs = int(duration_s % 60)
        # The scan-memo card (#81) is the card kind for this, in both modes: with
        # Telegram on it goes out *alongside* the queue message exactly as it
        # did before, and headless it is the delivery.
        from notify.cards.memo import build_scan_payload

        payload = build_scan_payload(
            scan_type=scan_type,
            duration_text=f"{mins}m {secs}s",
            total_scanned=total_scanned,
            escalated=escalated,
            memos_generated=memos_generated,
            memos=memo_details or [],
            as_of_utc=utcnow_naive().isoformat(),
        )

        if not self.telegram:
            await self._emit(
                Alert(
                    event="scan_complete",
                    subject=str(payload.get("subject") or f"[SCAN] {scan_type}"),
                    title=scan_type,
                    card_payload=payload,
                    kind=str(payload.get("kind") or KIND_ALERT),
                )
            )
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        text = (
            f"*Scan Complete \\({escape_md(scan_type)}\\)*\n\n"
            f"Duration: `{mins}m {secs}s`\n"
            f"Scanned: `{total_scanned}` tickers\n"
            f"Escalated to Sonnet: `{escalated}`\n"
            f"Memos generated: `{memos_generated}`\n"
        )
        keyboard = None
        if memo_details:
            rows = []
            for md in memo_details:
                score = md.get("score", 0)
                ticker = md.get("ticker", "?")
                classification = md.get("classification", "")
                memo_id = md.get("memo_id", 0)
                opus_rec = md.get("opus_recommendation", "")
                rec_emoji = {"proceed": "✅", "reduce_size": "⚠️", "watchlist": "👀", "pass": "❌"}.get(opus_rec, "")
                auto_note = " 🤖 auto\\-executed \\(paper\\)" if md.get("auto_executed") else ""
                text += f"  {rec_emoji} `{escape_md(ticker)}` \\(`{score:.2f}`\\) — {escape_md(classification)}{auto_note}\n"
                if memo_id:
                    rows.append([
                        InlineKeyboardButton(
                            f"{rec_emoji} {ticker} ({score:.2f}) — View Memo",
                            callback_data=f"viewmemo_{memo_id}",
                        )
                    ])
            if rows:
                keyboard = InlineKeyboardMarkup(rows)
        if memos_generated == 0:
            text += "\nNo opportunities met the memo threshold\\."
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)
        # ...and the same summary as an HTML email card, when the flag is on.
        # Telegram above is untouched: this PR adds a channel, it does not move
        # one. The card is built from `memo_details` — the scan's own scores and
        # classifications — and re-ranks nothing.
        self.email_card(payload)

    async def system_message(self, message: str):
        """Send a generic system notification."""
        text = f"ℹ️ {escape_md(message)}"
        await self._emit(
            Alert(
                event="system_message",
                subject=f"[SYSTEM] {str(message).splitlines()[0][:120] if message else 'notice'}",
                title="system message",
                body=str(message),
                telegram_text=text,
            )
        )

    # ── Position Monitor Alerts ──

    async def position_stop_breached(
        self, ticker: str, current_price: float, stop_price: float,
        pnl_pct: float, pnl_abs: float, direction: str, trade_id: int,
    ):
        """Alert: price has breached the stop-loss level."""
        dir_label = "SHORT" if direction == "short" else "LONG"
        text = (
            f"🔴 *STOP BREACHED: {escape_md(ticker)}* \\({escape_md(dir_label)}\\)\n\n"
            f"Price: `${current_price:,.2f}` — Stop: `${stop_price:,.2f}`\n"
            f"P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n\n"
            f"If Alpaca stop order didn't fire, close manually now\\."
        )

        def keyboard():
            from bot.keyboards import position_stop_keyboard

            return position_stop_keyboard(ticker, trade_id)

        await self._emit(
            Alert(
                event="position_stop_breached",
                subject=f"[STOP BREACHED] {ticker} ({dir_label})",
                title=ticker,
                headline=f"stop breached — {dir_label}",
                verdict={"label": "stop breached", "tone": "bad"},
                rows=(
                    _row("price", f"${current_price:,.2f}"),
                    _row("stop", f"${stop_price:,.2f}"),
                    _row("P&L", f"{pnl_pct:+.1f}% (${pnl_abs:+,.2f})", tone="bad"),
                    _row("trade id", trade_id),
                ),
                body="If the Alpaca stop order didn't fire, close manually now.",
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_target_approaching(
        self, ticker: str, target_num: int, current_price: float,
        target_price: float, distance_pct: float, pnl_pct: float,
        pnl_abs: float, trade_id: int,
    ):
        """Alert: price approaching a profit target."""
        text = (
            f"📈 *{escape_md(ticker)} approaching T{target_num}*\n\n"
            f"Current: `${current_price:,.2f}` \\| T{target_num}: `${target_price:,.2f}` "
            f"\\({distance_pct:.1f}% away\\)\n"
            f"Open P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )

        def keyboard():
            from bot.keyboards import position_target_keyboard

            return position_target_keyboard(ticker, trade_id, target_num)

        await self._emit(
            Alert(
                event="position_target_approaching",
                subject=f"[T{target_num} NEAR] {ticker} — {distance_pct:.1f}% away",
                title=ticker,
                headline=f"approaching T{target_num}",
                verdict={"label": f"near T{target_num}", "tone": "ok"},
                rows=(
                    _row("current", f"${current_price:,.2f}"),
                    _row(f"T{target_num}", f"${target_price:,.2f}", note=f"{distance_pct:.1f}% away"),
                    _row("open P&L", f"{pnl_pct:+.1f}% (${pnl_abs:+,.2f})"),
                    _row("trade id", trade_id),
                ),
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_target_hit(
        self, ticker: str, target_num: int, current_price: float,
        target_price: float, pnl_pct: float, pnl_abs: float,
        entry_price: float, trade_id: int,
    ):
        """Alert: profit target hit with action buttons."""
        text = (
            f"🎯 *{escape_md(ticker)} hit T{target_num}\\!*\n\n"
            f"Current: `${current_price:,.2f}` \\| T{target_num} was: `${target_price:,.2f}`\n"
            f"Open P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
        )
        recommendation = ""
        if target_num == 1:
            recommendation = f"Recommended: Sell 50%, move stop to breakeven (${entry_price:,.2f})"
            text += (
                f"\nRecommended: Sell 50%, move stop to breakeven "
                f"\\(`${entry_price:,.2f}`\\)"
            )

        def keyboard():
            from bot.keyboards import position_target_hit_keyboard

            return position_target_hit_keyboard(ticker, trade_id, target_num)

        await self._emit(
            Alert(
                event="position_target_hit",
                subject=f"[T{target_num} HIT] {ticker} @ ${current_price:,.2f}",
                title=ticker,
                headline=f"hit T{target_num}",
                verdict={"label": f"T{target_num} hit", "tone": "ok"},
                rows=(
                    _row("current", f"${current_price:,.2f}"),
                    _row(f"T{target_num} was", f"${target_price:,.2f}"),
                    _row("open P&L", f"{pnl_pct:+.1f}% (${pnl_abs:+,.2f})", tone="ok"),
                    _row("trade id", trade_id),
                ),
                body=recommendation,
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_time_expiring(
        self, ticker: str, days_held: int, max_days: int,
        pnl_pct: float, pnl_abs: float, trade_id: int,
    ):
        """Alert: position approaching max holding period."""
        remaining = max_days - days_held
        text = (
            f"⏰ *{escape_md(ticker)}: {days_held} of {max_days} max hold days*\n\n"
            f"Current P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
            f"This position expires in {remaining} trading days\\."
        )

        def keyboard():
            from bot.keyboards import position_time_keyboard

            return position_time_keyboard(ticker, trade_id)

        await self._emit(
            Alert(
                event="position_time_expiring",
                subject=f"[EXPIRING] {ticker} — {days_held} of {max_days} hold days",
                title=ticker,
                headline=f"{days_held} of {max_days} max hold days",
                verdict={"label": "expiring", "tone": "warn"},
                rows=(
                    _row("days held", f"{days_held} of {max_days}"),
                    _row("expires in", f"{remaining} trading days"),
                    _row("P&L", f"{pnl_pct:+.1f}% (${pnl_abs:+,.2f})"),
                    _row("trade id", trade_id),
                ),
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_time_expired(
        self, ticker: str, days_held: int, max_days: int,
        pnl_pct: float, pnl_abs: float, trade_id: int,
    ):
        """Alert: position at or past max holding period."""
        text = (
            f"🕐 *{escape_md(ticker)}: MAX HOLD REACHED \\({days_held} days\\)*\n\n"
            f"Current P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
            f"System will auto\\-close at next market open unless overridden\\."
        )

        def keyboard():
            from bot.keyboards import position_time_expired_keyboard

            return position_time_expired_keyboard(ticker, trade_id)

        await self._emit(
            Alert(
                event="position_time_expired",
                subject=f"[MAX HOLD] {ticker} — {days_held} days",
                title=ticker,
                headline=f"max hold reached ({days_held} days)",
                verdict={"label": "max hold", "tone": "warn"},
                rows=(
                    _row("days held", f"{days_held} of {max_days}"),
                    _row("P&L", f"{pnl_pct:+.1f}% (${pnl_abs:+,.2f})"),
                    _row("trade id", trade_id),
                ),
                body="System will auto-close at next market open unless overridden.",
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_profit_giveback(
        self, ticker: str, peak_pnl_pct: float, current_pnl_pct: float,
        giveback_pct: float, trade_id: int,
    ):
        """Alert: position giving back gains from peak."""
        text = (
            f"📉 *{escape_md(ticker)} giving back gains*\n\n"
            f"Peak: `{peak_pnl_pct:+.1f}%` → Current: `{current_pnl_pct:+.1f}%` "
            f"\\(gave back {giveback_pct:.1f}%\\)\n"
            f"Consider: trailing stop or partial profit\\-taking"
        )

        def keyboard():
            from bot.keyboards import position_giveback_keyboard

            return position_giveback_keyboard(ticker, trade_id)

        await self._emit(
            Alert(
                event="position_profit_giveback",
                subject=f"[GIVEBACK] {ticker} — gave back {giveback_pct:.1f}%",
                title=ticker,
                headline="giving back gains",
                verdict={"label": "giveback", "tone": "warn"},
                rows=(
                    _row("peak", f"{peak_pnl_pct:+.1f}%"),
                    _row("current", f"{current_pnl_pct:+.1f}%"),
                    _row("gave back", f"{giveback_pct:.1f}%", tone="warn"),
                    _row("trade id", trade_id),
                ),
                body="Consider: trailing stop or partial profit-taking.",
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    # ── Portfolio Threshold Alerts ──

    async def portfolio_strong_day(self, pnl_today_pct: float):
        """Alert: portfolio up >2% in a single day."""
        text = (
            f"🟢 *Strong day: {escape_md(f'+{pnl_today_pct:.1f}')}%*\n\n"
            f"Consider taking partial profits on extended positions\\."
        )
        await self._emit(
            Alert(
                event="portfolio_strong_day",
                subject=f"[PORTFOLIO] strong day: +{pnl_today_pct:.1f}%",
                title="strong day",
                headline=f"+{pnl_today_pct:.1f}%",
                verdict={"label": "strong day", "tone": "ok"},
                rows=(_row("today", f"+{pnl_today_pct:.1f}%", tone="ok"),),
                body="Consider taking partial profits on extended positions.",
                telegram_text=text,
            )
        )

    async def portfolio_rough_day(self, pnl_today_pct: float):
        """Alert: portfolio down >2% in a single day."""
        text = (
            f"🔴 *Rough day: {escape_md(f'{pnl_today_pct:.1f}')}%*\n\n"
            f"All stops are active\\. No action needed unless a stop triggers\\."
        )
        await self._emit(
            Alert(
                event="portfolio_rough_day",
                subject=f"[PORTFOLIO] rough day: {pnl_today_pct:.1f}%",
                title="rough day",
                headline=f"{pnl_today_pct:.1f}%",
                verdict={"label": "rough day", "tone": "bad"},
                rows=(_row("today", f"{pnl_today_pct:.1f}%", tone="bad"),),
                body="All stops are active. No action needed unless a stop triggers.",
                telegram_text=text,
            )
        )

    async def portfolio_drawdown_warning(self, drawdown_pct: float):
        """Alert: portfolio drawdown from peak >5%."""
        text = (
            f"⚠️ *Portfolio drawdown: {escape_md(f'-{drawdown_pct:.1f}')}% from peak*\n\n"
            f"Review all positions\\. Consider reducing exposure\\."
        )
        await self._emit(
            Alert(
                event="portfolio_drawdown_warning",
                subject=f"[PORTFOLIO] drawdown: -{drawdown_pct:.1f}% from peak",
                title="portfolio drawdown",
                headline=f"-{drawdown_pct:.1f}% from peak",
                verdict={"label": "drawdown", "tone": "warn"},
                rows=(_row("drawdown", f"-{drawdown_pct:.1f}% from peak", tone="warn"),),
                body="Review all positions. Consider reducing exposure.",
                telegram_text=text,
            )
        )

    async def portfolio_circuit_breaker(self, drawdown_pct: float):
        """Alert: portfolio drawdown >10% — circuit breaker."""
        text = (
            f"🚨 *CIRCUIT BREAKER: {escape_md(f'-{drawdown_pct:.1f}')}% drawdown*\n\n"
            f"System halting new trades for 5 days per risk rules\\.\n"
            f"Review all positions immediately\\."
        )
        await self._emit(
            Alert(
                event="portfolio_circuit_breaker",
                subject=f"[CIRCUIT BREAKER] -{drawdown_pct:.1f}% drawdown",
                title="circuit breaker",
                headline=f"-{drawdown_pct:.1f}% drawdown",
                verdict={"label": "halted", "tone": "bad"},
                rows=(_row("drawdown", f"-{drawdown_pct:.1f}%", tone="bad"),),
                body=(
                    "System halting new trades for 5 days per risk rules. "
                    "Review all positions immediately."
                ),
                telegram_text=text,
            )
        )

    # ── Position Threshold Alerts ──

    async def position_big_gain(self, ticker: str, pnl_pct: float, trade_id: int):
        """Alert: single position up >10%."""
        text = (
            f"🚀 *{escape_md(ticker)} up {escape_md(f'+{pnl_pct:.1f}')}%*\n\n"
            f"Consider partial profit\\-taking or tightening stop\\."
        )

        def keyboard():
            from bot.keyboards import position_target_keyboard

            return position_target_keyboard(ticker, trade_id, 1)

        await self._emit(
            Alert(
                event="position_big_gain",
                subject=f"[GAIN] {ticker} up +{pnl_pct:.1f}%",
                title=ticker,
                headline=f"up +{pnl_pct:.1f}%",
                verdict={"label": "big gain", "tone": "ok"},
                rows=(_row("P&L", f"+{pnl_pct:.1f}%", tone="ok"), _row("trade id", trade_id)),
                body="Consider partial profit-taking or tightening stop.",
                keyboard=keyboard,
                telegram_text=text,
            )
        )

    async def position_near_stop(self, ticker: str, pnl_pct: float, stop_price: float, trade_id: int):
        """Alert: single position down >5%, approaching stop."""
        text = (
            f"⚠️ *{escape_md(ticker)} down {escape_md(f'{pnl_pct:.1f}')}%*\n\n"
            f"Approaching stop\\-loss at `${stop_price:,.2f}`\\.\n"
            f"Verify stop order is active on Alpaca\\."
        )

        def keyboard():
            from bot.keyboards import position_stop_keyboard

            return position_stop_keyboard(ticker, trade_id)

        await self._emit(
            Alert(
                event="position_near_stop",
                subject=f"[NEAR STOP] {ticker} down {pnl_pct:.1f}%",
                title=ticker,
                headline=f"down {pnl_pct:.1f}%",
                verdict={"label": "near stop", "tone": "warn"},
                rows=(
                    _row("P&L", f"{pnl_pct:.1f}%", tone="warn"),
                    _row("stop", f"${stop_price:,.2f}"),
                    _row("trade id", trade_id),
                ),
                body="Verify the stop order is active on Alpaca.",
                keyboard=keyboard,
                telegram_text=text,
            )
        )
