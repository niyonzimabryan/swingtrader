"""
Proactive push notifications — order fills, stop triggers, regime changes, etc.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bot.message_queue import MessageQueue
from bot.formatters import escape_md
from utils.logger import get_logger

log = get_logger("notifications")


class NotificationManager:
    def __init__(self, message_queue: MessageQueue, chat_id: str):
        self.mq = message_queue
        self.chat_id = chat_id

    async def order_filled(self, ticker: str, shares: int, price: float, side: str, stop_loss: float, position_pct: float):
        """Notify operator of order fill."""
        emoji = "✅" if side == "buy" else "📤"
        text = (
            f"{emoji} *Order Filled*\n\n"
            f"{escape_md(side.upper())} `{shares}` shares of `{escape_md(ticker)}` @ `${price:,.2f}`\n"
            f"Stop\\-loss: `${stop_loss:,.2f}`\n"
            f"Position: `{position_pct:.1f}%` of portfolio"
        )
        await self.mq.send(self.chat_id, text)

    async def stop_triggered(self, ticker: str, shares: int, entry_price: float, exit_price: float, pnl_pct: float, pnl_abs: float):
        """Notify operator of stop-loss trigger."""
        text = (
            f"🛑 *Stop\\-Loss Triggered*\n\n"
            f"`{escape_md(ticker)}`: Sold `{shares}` shares @ `${exit_price:,.2f}`\n"
            f"Entry: `${entry_price:,.2f}` → Exit: `${exit_price:,.2f}`\n"
            f"P&L: 🔴 `{pnl_pct:+.2f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )
        await self.mq.send(self.chat_id, text)

    async def target_hit(self, ticker: str, target_num: int, exit_price: float, pnl_pct: float, pnl_abs: float, partial: bool = False):
        """Notify operator of profit target hit."""
        text = (
            f"🎯 *Target {target_num} Hit{'  (Partial Exit)' if partial else ''}*\n\n"
            f"`{escape_md(ticker)}` @ `${exit_price:,.2f}`\n"
            f"P&L: 🟢 `{pnl_pct:+.2f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )
        await self.mq.send(self.chat_id, text)

    async def regime_change(self, old_regime: str, new_regime: str, reasoning: str):
        """Notify operator of macro regime change."""
        emoji = "🟢" if new_regime == "risk-on" else "🟡" if new_regime == "neutral" else "🔴"
        text = (
            f"⚠️ *Regime Change*\n\n"
            f"`{escape_md(old_regime.upper())}` → {emoji} `{escape_md(new_regime.upper())}`\n\n"
            f"{escape_md(reasoning[:300])}"
        )
        await self.mq.send(self.chat_id, text)

    async def drawdown_warning(self, drawdown_pct: float, circuit_breaker_pct: float):
        """Notify operator of drawdown proximity to circuit breaker."""
        text = (
            f"⚠️ *Drawdown Warning*\n\n"
            f"Portfolio drawdown: `{drawdown_pct:.1f}%` from peak\n"
            f"Circuit breaker triggers at `{circuit_breaker_pct:.1f}%`\n"
            f"Consider reducing exposure\\."
        )
        await self.mq.send(self.chat_id, text)

    async def agent_failure(self, agent_name: str, error: str, next_retry: str = ""):
        """Notify operator of agent failure."""
        text = (
            f"⚠️ *Agent Failure*\n\n"
            f"Agent: `{escape_md(agent_name)}`\n"
            f"Error: {escape_md(error[:300])}\n"
        )
        if next_retry:
            text += f"Next retry: {escape_md(next_retry)}"
        await self.mq.send(self.chat_id, text)

    async def deep_research_update(self, ticker: str, message: str):
        """
        Notify operator of deep research progress/completion.
        Messages come pre-formatted from DeepResearchAgent.
        """
        await self.mq.send_plain(self.chat_id, message)

    async def deep_research_started(self, ticker: str, score: float):
        """Notify that deep research has been triggered."""
        text = (
            f"🔬 Deep research generating for {escape_md(ticker)}\\.\\.\\.\n"
            f"Score: `{score:.2f}` \\(threshold: 0\\.75\\)\n"
            f"Estimated time: 5\\-20 minutes"
        )
        await self.mq.send(self.chat_id, text)

    async def send_deep_research_pdf(self, ticker: str, pdf_path: str):
        """Send the deep research PDF report as a Telegram document."""
        caption = f"📄 Deep Research Report: {ticker}"
        await self.mq.send_document(self.chat_id, pdf_path, caption=caption)

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
                text += f"  {rec_emoji} `{escape_md(ticker)}` \\(`{score:.2f}`\\) — {escape_md(classification)}\n"
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

    async def system_message(self, message: str):
        """Send a generic system notification."""
        text = f"ℹ️ {escape_md(message)}"
        await self.mq.send(self.chat_id, text)
