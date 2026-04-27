"""
Daily Digest — 5 PM ET automated summary.
Pure math, no AI. Summarizes portfolio state, today's activity, position health.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from database.db import get_session
from database.models import Trade, Memo, Ticker
from bot.formatters import escape_md
from utils.logger import get_logger

log = get_logger("daily_digest")
ET = ZoneInfo("America/New_York")


class DailyDigest:
    def __init__(self, alpaca, notification_manager, settings):
        self.alpaca = alpaca
        self.nm = notification_manager
        self.settings = settings

    async def send_digest(self):
        """Generate and send the daily digest."""
        try:
            text = self._build_digest()
            if text and self.nm:
                await self.nm.mq.send(self.nm.chat_id, text)
                log.info("daily_digest_sent")
        except Exception as e:
            log.error("daily_digest_failed", error=str(e))

    def _build_digest(self) -> str:
        """Build the full digest message. Returns MarkdownV2 string."""
        now = datetime.now(ET)
        date_str = now.strftime("%b %d, %Y")

        # Portfolio data from Alpaca
        account = self.alpaca.get_account_info()
        positions = self.alpaca.get_positions_detail()

        equity = account.get("equity", 0)
        pnl_today = account.get("pnl_today", 0)
        pnl_today_pct = account.get("pnl_today_pct", 0)

        # Today's DB activity
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = today_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        with get_session() as session:
            # Memos generated today
            memos_today = session.query(Memo).filter(
                Memo.created_at >= today_utc
            ).all()

            # Trades opened today
            trades_opened = session.query(Trade).filter(
                Trade.entry_date >= today_utc,
                Trade.status.in_(["open", "closed"]),
            ).all()

            # Trades closed today
            trades_closed = session.query(Trade).filter(
                Trade.exit_date >= today_utc,
                Trade.status == "closed",
            ).all()

            # All open trades for position status
            open_trades = session.query(Trade).filter(
                Trade.status == "open",
            ).all()

            memos_today_count = len(memos_today)
            trades_opened_count = len(trades_opened)
            open_trades_count = len(open_trades)
            stops_today = sum(1 for t in trades_closed if t.exit_reason == "stop_loss")
            targets_today = sum(1 for t in trades_closed if t.exit_reason and "target" in t.exit_reason)

            # Build position status lines
            position_lines = []
            profitable_count = 0
            for trade in open_trades:
                ticker_symbol = trade.ticker.symbol if trade.ticker else "?"
                direction = trade.direction or "long"

                # Find matching Alpaca position
                pos = next((p for p in positions if p["ticker"] == ticker_symbol), None)
                if pos:
                    current_price = pos["current_price"]
                    if direction == "short":
                        pnl_pct = (trade.entry_price - current_price) / trade.entry_price * 100
                    else:
                        pnl_pct = (current_price - trade.entry_price) / trade.entry_price * 100
                else:
                    pnl_pct = 0

                days_held = (datetime.utcnow() - trade.entry_date).days if trade.entry_date else 0
                max_days = self.settings.max_holding_days

                if pnl_pct > 0:
                    profitable_count += 1
                    emoji = "✅"
                else:
                    emoji = "🔴"

                # Status note
                notes = []
                if trade.t1_hit:
                    notes.append("T1 hit")
                if days_held >= max_days - 2:
                    notes.append("time running")
                if trade.stop_loss > 0:
                    if direction == "long" and pos and pos["current_price"] <= trade.stop_loss * 1.02:
                        notes.append("near stop")
                    elif direction == "short" and pos and pos["current_price"] >= trade.stop_loss * 0.98:
                        notes.append("near stop")

                dir_label = "S" if direction == "short" else "L"
                note_str = f" — {', '.join(notes)}" if notes else ""
                position_lines.append(
                    f"  {emoji} `{escape_md(ticker_symbol)}` \\({dir_label}\\): "
                    f"`{pnl_pct:+.1f}%` \\(day {days_held}/{max_days}\\){escape_md(note_str)}"
                )

            alerts = self._generate_alerts(open_trades, positions)
        # Open P&L
        total_open_pnl = sum(p.get("pnl_abs", 0) for p in positions)

        # Build message
        pnl_emoji = "🟢" if pnl_today >= 0 else "🔴"
        text = f"📊 *DAILY DIGEST — {escape_md(date_str)}*\n\n"

        # Portfolio section
        text += f"*PORTFOLIO*\n"
        text += f"  Equity: `${equity:,.0f}` \\({pnl_emoji} `{pnl_today_pct:+.2f}%` today\\)\n"
        text += f"  Open P&L: `${total_open_pnl:+,.0f}` across {len(positions)} positions\n"
        if open_trades_count:
            text += f"  Win rate: {profitable_count}/{open_trades_count} positions in profit\n"
        text += "\n"

        # Activity section
        text += f"*TODAY'S ACTIVITY*\n"
        text += f"  New memos: `{memos_today_count}`\n"
        text += f"  Trades executed: `{trades_opened_count}`\n"
        text += f"  Stops triggered: `{stops_today}`\n"
        text += f"  Targets hit: `{targets_today}`\n"
        text += "\n"

        # Position status
        if position_lines:
            text += f"*POSITION STATUS*\n"
            text += "\n".join(position_lines) + "\n\n"

        # Alerts section
        if alerts:
            text += f"*ALERTS*\n"
            for alert in alerts:
                text += f"  {escape_md(alert)}\n"

        return text

    def _generate_alerts(self, open_trades: list, positions: list) -> list[str]:
        """Generate alert lines for the digest."""
        alerts = []

        for trade in open_trades:
            ticker_symbol = trade.ticker.symbol if trade.ticker else "?"
            direction = trade.direction or "long"
            pos = next((p for p in positions if p["ticker"] == ticker_symbol), None)

            if not pos:
                continue

            current_price = pos["current_price"]

            # Stop breach check
            if direction == "long" and current_price <= trade.stop_loss:
                alerts.append(f"⚠️ {ticker_symbol} past stop-loss — close or verify stop order")
            elif direction == "short" and trade.stop_loss > 0 and current_price >= trade.stop_loss:
                alerts.append(f"⚠️ {ticker_symbol} past stop-loss — close or verify stop order")

            # Target approaching
            if trade.target_1 > 0 and not trade.t1_hit:
                if direction == "long":
                    dist = (trade.target_1 - current_price) / trade.target_1 * 100
                else:
                    dist = (current_price - trade.target_1) / trade.target_1 * 100
                if 0 < dist <= 3:
                    alerts.append(f"📈 {ticker_symbol} within {dist:.1f}% of T1 — prepare exit plan")

            # Time running
            if trade.entry_date:
                days_held = (datetime.utcnow() - trade.entry_date).days
                remaining = self.settings.max_holding_days - days_held
                if 0 < remaining <= 3:
                    alerts.append(f"⏰ {ticker_symbol} has {remaining} trading days remaining")

        return alerts
