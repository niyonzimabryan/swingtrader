"""
Weekly Performance Report — Sunday evening, uses Sonnet (~$0.03).
Gathers trade data from DB, portfolio state from Alpaca,
sends structured data to Sonnet for narrative analysis.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from database.db import get_session
from database.models import Trade, Memo, Ticker
from bot.formatters import escape_md
from utils.logger import get_logger

log = get_logger("weekly_report")
ET = ZoneInfo("America/New_York")
SONNET_MODEL = "claude-sonnet-4-6"


class WeeklyReport:
    def __init__(self, alpaca, notification_manager, settings):
        self.alpaca = alpaca
        self.nm = notification_manager
        self.settings = settings
        self._anthropic = None

    def _get_anthropic(self):
        """Lazy-init Anthropic client."""
        if self._anthropic is None:
            from utils.anthropic_client import AnthropicClient
            self._anthropic = AnthropicClient(self.settings.anthropic_api_key)
        return self._anthropic

    async def send_report(self):
        """Generate and send the weekly performance report."""
        try:
            data = self._gather_data()
            narrative = self._generate_narrative(data)
            text = self._format_message(data, narrative)
            if text and self.nm:
                await self.nm.mq.send(self.nm.chat_id, text)
                log.info("weekly_report_sent")
        except Exception as e:
            log.error("weekly_report_failed", error=str(e))

    def _gather_data(self) -> dict:
        """Gather all performance data for the past week."""
        now = datetime.now(ET)
        # Week = last Monday through Friday
        days_since_monday = now.weekday()
        week_end = now.replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(days=max(0, days_since_monday - 4))
        week_start = week_end - timedelta(days=4)
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        week_start_utc = week_start.astimezone(ZoneInfo("UTC"))
        week_end_utc = week_end.astimezone(ZoneInfo("UTC"))

        # Portfolio state
        account = self.alpaca.get_account_info()
        positions = self.alpaca.get_positions_detail()

        with get_session() as session:
            # Trades opened this week
            trades_opened = session.query(Trade).filter(
                Trade.entry_date >= week_start_utc,
                Trade.entry_date <= week_end_utc,
            ).all()

            # Trades closed this week
            trades_closed = session.query(Trade).filter(
                Trade.exit_date >= week_start_utc,
                Trade.exit_date <= week_end_utc,
                Trade.status == "closed",
            ).all()

            # Memos generated this week
            memos = session.query(Memo).filter(
                Memo.generated_at >= week_start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            ).all()

            # All open trades
            open_trades = session.query(Trade).filter(
                Trade.status == "open",
            ).all()

            # Compute metrics
            wins = [t for t in trades_closed if t.realized_pnl and t.realized_pnl > 0]
            losses = [t for t in trades_closed if t.realized_pnl and t.realized_pnl <= 0]
            total_closed = len(trades_closed)
            win_rate = len(wins) / total_closed * 100 if total_closed > 0 else 0

            realized_pnl = sum(t.realized_pnl or 0 for t in trades_closed)

            # Memo breakdown
            approved_count = sum(1 for m in memos if m.operator_action == "approve")
            passed_count = sum(1 for m in memos if m.operator_action == "pass")
            watchlisted_count = sum(1 for m in memos if m.operator_action == "watchlist")
            total_memos = len(memos)

            # Average scores
            approved_scores = [m.composite_score for m in memos if m.operator_action == "approve" and m.composite_score]
            passed_scores = [m.composite_score for m in memos if m.operator_action == "pass" and m.composite_score]
            avg_approved_score = sum(approved_scores) / len(approved_scores) if approved_scores else 0
            avg_passed_score = sum(passed_scores) / len(passed_scores) if passed_scores else 0

            # Position health
            position_data = []
            for trade in open_trades:
                ticker_sym = trade.ticker.symbol if trade.ticker else "?"
                direction = trade.direction or "long"
                pos = next((p for p in positions if p["ticker"] == ticker_sym), None)
                if pos:
                    if direction == "short":
                        pnl_pct = (trade.entry_price - pos["current_price"]) / trade.entry_price * 100
                    else:
                        pnl_pct = (pos["current_price"] - trade.entry_price) / trade.entry_price * 100
                else:
                    pnl_pct = 0
                days_held = (datetime.utcnow() - trade.entry_date).days if trade.entry_date else 0
                position_data.append({
                    "ticker": ticker_sym,
                    "direction": direction,
                    "pnl_pct": round(pnl_pct, 2),
                    "days_held": days_held,
                    "max_days": self.settings.max_holding_days,
                    "t1_hit": trade.t1_hit or False,
                    "stop_loss": trade.stop_loss,
                })

            # Closed trade details for narrative
            closed_details = []
            for t in trades_closed:
                ticker_sym = t.ticker.symbol if t.ticker else "?"
                closed_details.append({
                    "ticker": ticker_sym,
                    "direction": t.direction or "long",
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price or 0,
                    "realized_pnl": t.realized_pnl or 0,
                    "exit_reason": t.exit_reason or "unknown",
                    "days_held": (t.exit_date - t.entry_date).days if t.exit_date and t.entry_date else 0,
                })

        return {
            "week_start": week_start.strftime("%b %d"),
            "week_end": week_end.strftime("%b %d, %Y"),
            "equity": account.get("equity", 0),
            "trades_opened": len(trades_opened),
            "trades_closed": total_closed,
            "win_rate": win_rate,
            "wins": len(wins),
            "losses": len(losses),
            "realized_pnl": realized_pnl,
            "total_memos": total_memos,
            "approved": approved_count,
            "passed": passed_count,
            "watchlisted": watchlisted_count,
            "avg_approved_score": avg_approved_score,
            "avg_passed_score": avg_passed_score,
            "open_positions": len(position_data),
            "positions": position_data,
            "closed_details": closed_details,
            "profitable_positions": sum(1 for p in position_data if p["pnl_pct"] > 0),
        }

    def _generate_narrative(self, data: dict) -> str:
        """Use Sonnet to generate 'What Worked' / 'What Didn't' narrative."""
        client = self._get_anthropic()

        system_prompt = (
            "You are a trading performance analyst. Given weekly trading data, "
            "produce a brief narrative with two sections:\n"
            "1. WHAT WORKED (2-3 sentences about positive patterns)\n"
            "2. WHAT DIDN'T (2-3 sentences about areas for improvement)\n\n"
            "Be specific, reference tickers and numbers. If there's not enough data "
            "(e.g., no closed trades), say so honestly. Keep total response under 200 words."
        )

        user_prompt = f"""Weekly trading performance data:

Portfolio equity: ${data['equity']:,.0f}
Trades opened: {data['trades_opened']} | Closed: {data['trades_closed']}
Win rate: {data['win_rate']:.0f}% ({data['wins']}W / {data['losses']}L)
Realized P&L: ${data['realized_pnl']:+,.2f}
Memos: {data['total_memos']} total | {data['approved']} approved, {data['passed']} passed, {data['watchlisted']} watchlisted
Avg approved score: {data['avg_approved_score']:.2f} | Avg passed score: {data['avg_passed_score']:.2f}

Open positions:
{json.dumps(data['positions'], indent=2)}

Closed trades this week:
{json.dumps(data['closed_details'], indent=2)}

Generate the WHAT WORKED and WHAT DIDN'T sections."""

        try:
            response = client.analyze(
                model=SONNET_MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=512,
                temperature=0.4,
            )
            return response.strip()
        except Exception as e:
            log.error("sonnet_narrative_failed", error=str(e))
            return "Narrative generation unavailable this week."

    def _format_message(self, data: dict, narrative: str) -> str:
        """Format the full weekly report as MarkdownV2."""
        text = f"📈 *WEEKLY PERFORMANCE — {escape_md(data['week_start'])} to {escape_md(data['week_end'])}*\n\n"

        # Results
        text += f"*RESULTS*\n"
        text += f"  Portfolio: `${data['equity']:,.0f}`\n"
        text += f"  Realized P&L: `${data['realized_pnl']:+,.2f}`\n"
        text += f"  Trades opened: `{data['trades_opened']}` \\| Closed: `{data['trades_closed']}`\n"
        if data["trades_closed"] > 0:
            text += f"  Win rate: `{data['wins']}/{data['trades_closed']}` \\({data['win_rate']:.0f}%\\)\n"
        text += "\n"

        # Signal quality
        text += f"*SIGNAL QUALITY*\n"
        text += f"  Memos generated: `{data['total_memos']}`\n"
        if data["total_memos"] > 0:
            text += f"  Approved: `{data['approved']}` \\| Passed: `{data['passed']}` \\| Watchlisted: `{data['watchlisted']}`\n"
            approval_rate = data["approved"] / data["total_memos"] * 100 if data["total_memos"] > 0 else 0
            text += f"  Approval rate: `{approval_rate:.0f}%`\n"
            if data["avg_approved_score"] > 0:
                text += f"  Avg approved score: `{data['avg_approved_score']:.2f}`\n"
        text += "\n"

        # Sonnet narrative
        for line in narrative.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Bold section headers
            if line.upper().startswith("WHAT WORKED") or line.upper().startswith("1."):
                text += f"*WHAT WORKED*\n"
            elif line.upper().startswith("WHAT DIDN'T") or line.upper().startswith("2."):
                text += f"\n*WHAT DIDN'T*\n"
            else:
                text += f"  {escape_md(line)}\n"
        text += "\n"

        # Position health
        if data["positions"]:
            text += f"*POSITION HEALTH*\n"
            text += f"  {data['profitable_positions']} of {data['open_positions']} positions profitable\n"
            avg_days = sum(p["days_held"] for p in data["positions"]) / len(data["positions"]) if data["positions"] else 0
            text += f"  Avg holding period: `{avg_days:.0f}` days\n"
            past_max = sum(1 for p in data["positions"] if p["days_held"] >= p["max_days"])
            if past_max > 0:
                text += f"  ⚠️ {past_max} position\\(s\\) past max hold\n"
            else:
                text += f"  No positions past max hold\n"

        return text
