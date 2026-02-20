"""
Proactive push notifications — order fills, stop triggers, regime changes, etc.
"""

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

    async def system_message(self, message: str):
        """Send a generic system notification."""
        text = f"ℹ️ {escape_md(message)}"
        await self.mq.send(self.chat_id, text)
