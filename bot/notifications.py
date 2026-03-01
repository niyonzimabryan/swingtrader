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

    # ── Position Monitor Alerts ──

    async def position_stop_breached(
        self, ticker: str, current_price: float, stop_price: float,
        pnl_pct: float, pnl_abs: float, direction: str, trade_id: int,
    ):
        """Alert: price has breached the stop-loss level."""
        from bot.keyboards import position_stop_keyboard
        dir_label = "SHORT" if direction == "short" else "LONG"
        text = (
            f"🔴 *STOP BREACHED: {escape_md(ticker)}* \\({escape_md(dir_label)}\\)\n\n"
            f"Price: `${current_price:,.2f}` — Stop: `${stop_price:,.2f}`\n"
            f"P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n\n"
            f"If Alpaca stop order didn't fire, close manually now\\."
        )
        keyboard = position_stop_keyboard(ticker, trade_id)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_target_approaching(
        self, ticker: str, target_num: int, current_price: float,
        target_price: float, distance_pct: float, pnl_pct: float,
        pnl_abs: float, trade_id: int,
    ):
        """Alert: price approaching a profit target."""
        from bot.keyboards import position_target_keyboard
        text = (
            f"📈 *{escape_md(ticker)} approaching T{target_num}*\n\n"
            f"Current: `${current_price:,.2f}` \\| T{target_num}: `${target_price:,.2f}` "
            f"\\({distance_pct:.1f}% away\\)\n"
            f"Open P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)"
        )
        keyboard = position_target_keyboard(ticker, trade_id, target_num)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_target_hit(
        self, ticker: str, target_num: int, current_price: float,
        target_price: float, pnl_pct: float, pnl_abs: float,
        entry_price: float, trade_id: int,
    ):
        """Alert: profit target hit with action buttons."""
        from bot.keyboards import position_target_hit_keyboard
        text = (
            f"🎯 *{escape_md(ticker)} hit T{target_num}\\!*\n\n"
            f"Current: `${current_price:,.2f}` \\| T{target_num} was: `${target_price:,.2f}`\n"
            f"Open P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
        )
        if target_num == 1:
            text += (
                f"\nRecommended: Sell 50%, move stop to breakeven "
                f"\\(`${entry_price:,.2f}`\\)"
            )
        keyboard = position_target_hit_keyboard(ticker, trade_id, target_num)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_time_expiring(
        self, ticker: str, days_held: int, max_days: int,
        pnl_pct: float, pnl_abs: float, trade_id: int,
    ):
        """Alert: position approaching max holding period."""
        from bot.keyboards import position_time_keyboard
        remaining = max_days - days_held
        text = (
            f"⏰ *{escape_md(ticker)}: {days_held} of {max_days} max hold days*\n\n"
            f"Current P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
            f"This position expires in {remaining} trading days\\."
        )
        keyboard = position_time_keyboard(ticker, trade_id)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_time_expired(
        self, ticker: str, days_held: int, max_days: int,
        pnl_pct: float, pnl_abs: float, trade_id: int,
    ):
        """Alert: position at or past max holding period."""
        from bot.keyboards import position_time_expired_keyboard
        text = (
            f"🕐 *{escape_md(ticker)}: MAX HOLD REACHED \\({days_held} days\\)*\n\n"
            f"Current P&L: `{pnl_pct:+.1f}%` \\(`${pnl_abs:+,.2f}`\\)\n"
            f"System will auto\\-close at next market open unless overridden\\."
        )
        keyboard = position_time_expired_keyboard(ticker, trade_id)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_profit_giveback(
        self, ticker: str, peak_pnl_pct: float, current_pnl_pct: float,
        giveback_pct: float, trade_id: int,
    ):
        """Alert: position giving back gains from peak."""
        from bot.keyboards import position_giveback_keyboard
        text = (
            f"📉 *{escape_md(ticker)} giving back gains*\n\n"
            f"Peak: `{peak_pnl_pct:+.1f}%` → Current: `{current_pnl_pct:+.1f}%` "
            f"\\(gave back {giveback_pct:.1f}%\\)\n"
            f"Consider: trailing stop or partial profit\\-taking"
        )
        keyboard = position_giveback_keyboard(ticker, trade_id)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    # ── Portfolio Threshold Alerts ──

    async def portfolio_strong_day(self, pnl_today_pct: float):
        """Alert: portfolio up >2% in a single day."""
        text = (
            f"🟢 *Strong day: {escape_md(f'+{pnl_today_pct:.1f}')}%*\n\n"
            f"Consider taking partial profits on extended positions\\."
        )
        await self.mq.send(self.chat_id, text)

    async def portfolio_rough_day(self, pnl_today_pct: float):
        """Alert: portfolio down >2% in a single day."""
        text = (
            f"🔴 *Rough day: {escape_md(f'{pnl_today_pct:.1f}')}%*\n\n"
            f"All stops are active\\. No action needed unless a stop triggers\\."
        )
        await self.mq.send(self.chat_id, text)

    async def portfolio_drawdown_warning(self, drawdown_pct: float):
        """Alert: portfolio drawdown from peak >5%."""
        text = (
            f"⚠️ *Portfolio drawdown: {escape_md(f'-{drawdown_pct:.1f}')}% from peak*\n\n"
            f"Review all positions\\. Consider reducing exposure\\."
        )
        await self.mq.send(self.chat_id, text)

    async def portfolio_circuit_breaker(self, drawdown_pct: float):
        """Alert: portfolio drawdown >10% — circuit breaker."""
        text = (
            f"🚨 *CIRCUIT BREAKER: {escape_md(f'-{drawdown_pct:.1f}')}% drawdown*\n\n"
            f"System halting new trades for 5 days per risk rules\\.\n"
            f"Review all positions immediately\\."
        )
        await self.mq.send(self.chat_id, text)

    # ── Position Threshold Alerts ──

    async def position_big_gain(self, ticker: str, pnl_pct: float, trade_id: int):
        """Alert: single position up >10%."""
        from bot.keyboards import position_target_keyboard
        text = (
            f"🚀 *{escape_md(ticker)} up {escape_md(f'+{pnl_pct:.1f}')}%*\n\n"
            f"Consider partial profit\\-taking or tightening stop\\."
        )
        keyboard = position_target_keyboard(ticker, trade_id, 1)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)

    async def position_near_stop(self, ticker: str, pnl_pct: float, stop_price: float, trade_id: int):
        """Alert: single position down >5%, approaching stop."""
        from bot.keyboards import position_stop_keyboard
        text = (
            f"⚠️ *{escape_md(ticker)} down {escape_md(f'{pnl_pct:.1f}')}%*\n\n"
            f"Approaching stop\\-loss at `${stop_price:,.2f}`\\.\n"
            f"Verify stop order is active on Alpaca\\."
        )
        keyboard = position_stop_keyboard(ticker, trade_id)
        await self.mq.send(self.chat_id, text, reply_markup=keyboard)
