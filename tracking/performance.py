"""
Performance tracking — per-trade P&L and aggregate metrics.
"""

from database.db import get_session
from database.models import Trade
from utils.logger import get_logger

log = get_logger("performance")


def get_performance_summary() -> dict:
    """Compute aggregate performance metrics from closed trades."""
    with get_session() as session:
        closed = session.query(Trade).filter(Trade.status == "closed").all()

        if not closed:
            return {"total_trades": 0, "message": "No closed trades yet."}

        wins = [t for t in closed if (t.pnl_absolute or 0) > 0]
        losses = [t for t in closed if (t.pnl_absolute or 0) <= 0]

        total_pnl = sum(t.pnl_absolute or 0 for t in closed)
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win_pct = sum(t.pnl_pct or 0 for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t.pnl_pct or 0 for t in losses) / len(losses) if losses else 0

        total_gains = sum(t.pnl_absolute or 0 for t in wins)
        total_losses = abs(sum(t.pnl_absolute or 0 for t in losses))
        profit_factor = total_gains / total_losses if total_losses > 0 else float('inf')

        avg_hold = sum(
            (t.exit_date - t.entry_date).days
            for t in closed if t.exit_date and t.entry_date
        ) / len(closed) if closed else 0

        return {
            "total_trades": len(closed),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 1),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_holding_days": round(avg_hold, 1),
            "best_trade": max((t.pnl_pct or 0 for t in closed), default=0),
            "worst_trade": min((t.pnl_pct or 0 for t in closed), default=0),
        }
