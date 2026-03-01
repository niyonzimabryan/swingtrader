"""
Order Manager — orchestrates the approval→execution flow.
Connects Telegram approval to Alpaca order submission.
"""

import json
from datetime import datetime
from execution.alpaca_client import AlpacaClient
from execution.risk_manager import RiskManager
from execution.position_manager import PositionManager
from database.db import get_session
from database.models import Memo, Trade, Ticker
from utils.logger import get_logger

log = get_logger("order_manager")


class OrderManager:
    def __init__(self, settings, alpaca: AlpacaClient, risk_manager: RiskManager, position_manager: PositionManager):
        self.settings = settings
        self.alpaca = alpaca
        self.risk = risk_manager
        self.position = position_manager

    async def execute_approved_trade(self, memo_id: int) -> dict:
        """
        Execute a trade after operator approval.
        1. Load memo from DB
        2. Run risk checks
        3. Submit limit buy
        4. On fill, submit stop-loss
        5. Create Trade record
        """
        # Load memo
        with get_session() as session:
            memo = session.query(Memo).filter_by(id=memo_id).first()
            if not memo:
                return {"success": False, "error": "Memo not found"}

            ticker = memo.ticker.symbol if memo.ticker else None
            ticker_id = memo.ticker_id
            trade_params = memo.trade_params_dict
            signal_breakdown = memo.signal_breakdown_dict
            composite_score = memo.composite_score
            classification = memo.classification

        if not ticker:
            return {"success": False, "error": "No ticker associated with memo"}

        # Get portfolio state for risk checks
        account = self.alpaca.get_account_info()
        positions = self.alpaca.get_positions_detail()

        from config.tickers import UNIVERSE
        sector_exposure = {}
        total_value = 0
        for pos in positions:
            sector = UNIVERSE.get(pos["ticker"], "Unknown")
            mv = pos.get("market_value", 0)
            sector_exposure[sector] = sector_exposure.get(sector, 0) + mv / account.get("equity", 1)
            total_value += mv

        portfolio_state = {
            "equity": account.get("equity", self.settings.portfolio_value),
            "cash": account.get("cash", self.settings.portfolio_value),
            "pnl_today": account.get("pnl_today", 0),
            "pnl_today_pct": account.get("pnl_today_pct", 0),
            "position_count": len(positions),
            "positions": positions,
            "sector_exposure": sector_exposure,
            "total_exposure_pct": total_value / account.get("equity", 1) if account.get("equity", 0) > 0 else 0,
        }

        # Get regime
        from agents.macro_agent import MacroRegimeAgent
        regime_data = {"regime": "neutral", "position_size_multiplier": 1.0, "max_positions": 5, "max_exposure": 0.60}
        # Use trade_params from memo if available
        regime_data["position_size_multiplier"] = trade_params.get("regime_multiplier", 1.0)

        # Risk check
        risk_result = self.risk.full_risk_check(
            ticker, portfolio_state, regime_data,
            {"position_pct": trade_params.get("position_pct", 5) / 100, "setup_type": ""},
        )

        if not risk_result["allowed"]:
            log.warning("trade_blocked_by_risk", ticker=ticker, reasons=risk_result["reasons"])
            return {"success": False, "error": " | ".join(risk_result["reasons"])}

        # Submit entry order (direction-aware)
        shares = trade_params.get("shares", 0)
        entry_price = trade_params.get("entry_price", 0)
        stop_loss = trade_params.get("stop_loss", 0)
        direction = trade_params.get("direction", "long")

        if shares <= 0 or entry_price <= 0:
            return {"success": False, "error": "Invalid trade parameters (shares or price <= 0)"}

        try:
            if direction == "short":
                entry_order_id = self.alpaca.submit_limit_short_entry(ticker, shares, entry_price)
            else:
                entry_order_id = self.alpaca.submit_limit_buy(ticker, shares, entry_price)
        except Exception as e:
            return {"success": False, "error": f"Order submission failed: {str(e)}"}

        # Submit stop-loss (direction-aware)
        stop_order_id = ""
        try:
            stop_order_id = self.alpaca.submit_stop_loss(ticker, shares, stop_loss, direction=direction)
        except Exception as e:
            log.error("stop_loss_placement_failed", ticker=ticker, error=str(e))

        # Create Trade record
        try:
            with get_session() as session:
                trade = Trade(
                    ticker_id=ticker_id,
                    memo_id=memo_id,
                    direction=direction,
                    entry_price=entry_price,
                    entry_date=datetime.utcnow(),
                    shares=shares,
                    stop_loss=stop_loss,
                    target_1=trade_params.get("target_1", 0),
                    target_2=trade_params.get("target_2", 0),
                    position_pct=trade_params.get("position_pct", 0),
                    status="open",
                    setup_type=trade_params.get("setup_type", ""),
                    signal_scores=json.dumps(signal_breakdown),
                    regime_at_entry=regime_data.get("regime", "neutral"),
                    alpaca_entry_order_id=entry_order_id,
                    alpaca_stop_order_id=stop_order_id,
                )
                session.add(trade)
        except Exception as e:
            log.error("trade_record_failed", ticker=ticker, error=str(e))

        log.info(
            "trade_executed",
            ticker=ticker, shares=shares, entry=entry_price,
            stop=stop_loss, order_id=entry_order_id,
        )

        return {
            "success": True,
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "risk_warnings": risk_result.get("warnings", []),
        }
