"""
Order Manager - orchestrates the approval -> review -> execution flow.
"""

import asyncio
import json
from datetime import datetime
from execution.alpaca_client import AlpacaClient
from execution.brokers.alpaca import AlpacaBroker
from execution.brokers.base import BrokerOrderRequest, BrokerOrderReview
from execution.risk_manager import RiskManager
from execution.position_manager import PositionManager
from database.db import get_session
from database.models import Memo, OrderEvent, Trade, Ticker
from utils.logger import get_logger

log = get_logger("order_manager")


class OrderManager:
    def __init__(
        self,
        settings,
        alpaca: AlpacaClient,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        broker=None,
    ):
        self.settings = settings
        self.alpaca = alpaca
        self.broker = broker or AlpacaBroker(alpaca)
        self.risk = risk_manager
        self.position = position_manager
        # Serializes the cap-check→place→record critical section so two
        # concurrent operator approvals can't both clear the daily notional cap.
        self._place_lock = asyncio.Lock()

    async def execute_approved_trade(self, memo_id: int, force_place: bool = False) -> dict:
        """Review and optionally place a trade after operator approval."""
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
        broker = self.broker
        active_broker = getattr(broker, "active", broker)
        broker_name = getattr(active_broker, "name", "alpaca")
        account = await asyncio.to_thread(broker.get_account_info)
        positions = await asyncio.to_thread(broker.get_positions_detail)

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
            reasons = risk_result.get("reasons", ["Risk check blocked trade"])
            log.warning("trade_blocked_by_risk", ticker=ticker, reasons=reasons)
            return {"success": False, "error": " | ".join(reasons)}

        try:
            order_request = self._build_order_request(
                broker_name=broker_name,
                ticker=ticker,
                trade_params=trade_params,
                account=account,
                positions=positions,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

        limit_check = self._check_runtime_limits(broker_name, ticker, order_request, positions)
        if not limit_check["allowed"]:
            return {"success": False, "error": " | ".join(limit_check["reasons"])}
        if limit_check.get("request"):
            order_request = limit_check["request"]

        try:
            review = await asyncio.to_thread(broker.review_order, order_request)
        except Exception as e:
            return {"success": False, "error": f"Order review failed: {str(e)}"}

        self._record_order_event(
            memo_id=memo_id,
            broker=broker_name,
            account_id=self._account_id(active_broker),
            order_id="",
            event_type="review",
            status="approved" if review.approved else "rejected",
            notional=review.estimated_notional or order_request.requested_notional,
            raw_payload=review.raw,
        )

        if not review.approved:
            return {"success": False, "error": " | ".join(review.errors or ["Broker review rejected order"])}

        mode = str(getattr(self.settings, "execution_mode", "paper")).lower()
        if mode in ("review", "review_only"):
            trade_id = self._create_trade_record(
                ticker_id=ticker_id,
                memo_id=memo_id,
                trade_params=trade_params,
                signal_breakdown=signal_breakdown,
                regime_data=regime_data,
                review=review,
                status="reviewed",
                order_id="",
                stop_order_id="",
                order_strategy="review_only",
                filled_notional=None,
            )
            return {
                "success": True,
                "status": "reviewed",
                "review_only": True,
                "trade_id": trade_id,
                "ticker": ticker,
                "broker": broker_name,
                "warnings": review.warnings,
                "estimated_notional": review.estimated_notional,
                "risk_warnings": risk_result.get("warnings", []),
            }

        if mode == "live":
            if not bool(getattr(self.settings, "allow_live_trading", False)):
                return {
                    "success": False,
                    "error": "Live trading is disabled. Set ALLOW_LIVE_TRADING=true and use /mode live.",
                }
            if review.warnings and not force_place:
                return {
                    "success": True,
                    "status": "reviewed",
                    "requires_confirmation": True,
                    "trade_id": None,
                    "ticker": ticker,
                    "broker": broker_name,
                    "warnings": review.warnings,
                    "estimated_notional": review.estimated_notional,
                    "risk_warnings": risk_result.get("warnings", []),
                }
        elif mode != "paper":
            return {"success": False, "error": f"Unsupported execution mode: {mode}"}

        # Serialize placement: re-check the daily cap and place inside one lock
        # so a prior placement's "placed" event is committed before the next
        # approval re-reads the running total. Closes the cap-bypass race.
        async with self._place_lock:
            recheck = self._check_runtime_limits(broker_name, ticker, order_request, positions)
            if not recheck["allowed"]:
                return {"success": False, "error": " | ".join(recheck["reasons"])}

            try:
                placed = await asyncio.to_thread(broker.place_order, review)
            except Exception as e:
                return {"success": False, "error": f"Order submission failed: {str(e)}"}

            if not placed.success:
                return {"success": False, "error": placed.error or "Order submission failed"}

            status = "pending_fill" if placed.status in ("submitted", "new", "queued", "confirmed", "pending_fill", "filled") else placed.status
            entry_order_id = placed.order_id
            stop_order_id = placed.stop_order_id
            order_strategy = placed.order_strategy or "simple"
            trade_id = self._create_trade_record(
                ticker_id=ticker_id,
                memo_id=memo_id,
                trade_params=trade_params,
                signal_breakdown=signal_breakdown,
                regime_data=regime_data,
                review=review,
                status=status,
                order_id=entry_order_id,
                stop_order_id=stop_order_id,
                order_strategy=order_strategy,
                filled_notional=placed.filled_notional,
            )
            self._record_order_event(
                trade_id=trade_id,
                memo_id=memo_id,
                broker=broker_name,
                account_id=self._account_id(active_broker),
                order_id=entry_order_id,
                event_type="placed",
                status=status,
                notional=order_request.requested_notional,
                raw_payload=placed.raw,
            )

        log.info(
            "trade_submitted",
            ticker=ticker,
            broker=broker_name,
            order_id=entry_order_id,
            status=status,
        )

        return {
            "success": True,
            "status": status,
            "ticker": ticker,
            "broker": broker_name,
            "shares": order_request.quantity or trade_params.get("shares", 0),
            "entry_price": order_request.limit_price or trade_params.get("entry_price", 0),
            "stop_loss": order_request.stop_loss or trade_params.get("stop_loss", 0),
            "requested_notional": order_request.requested_notional,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "trade_id": trade_id,
            "risk_warnings": risk_result.get("warnings", []),
            "review_warnings": review.warnings,
        }

    def _build_order_request(
        self,
        broker_name: str,
        ticker: str,
        trade_params: dict,
        account: dict,
        positions: list[dict],
    ) -> BrokerOrderRequest:
        shares = float(trade_params.get("shares", 0) or 0)
        entry_price = float(trade_params.get("entry_price", 0) or 0)
        stop_loss = float(trade_params.get("stop_loss", 0) or 0)
        direction = str(trade_params.get("direction", "long") or "long").lower()
        if entry_price <= 0:
            raise ValueError("Invalid trade parameters (entry price <= 0)")

        if broker_name == "robinhood":
            if direction != "long":
                raise ValueError("Robinhood live trading is equities long-only in this integration. Use paper mode for shorts.")
            order_type = str(getattr(self.settings, "robinhood_order_type", "market")).lower()
            side = "buy"
            requested = self._desired_notional(trade_params, shares, entry_price)
            requested = min(requested, float(getattr(self.settings, "robinhood_max_order_notional", requested)))
            if order_type == "market":
                return BrokerOrderRequest(
                    symbol=ticker,
                    side=side,
                    order_type="market",
                    dollar_amount=round(requested, 2),
                    requested_notional=round(requested, 2),
                    direction=direction,
                    stop_loss=stop_loss,
                    target_1=trade_params.get("target_1", 0),
                    target_2=trade_params.get("target_2", 0),
                    market_hours=getattr(self.settings, "robinhood_market_hours", "regular_hours"),
                )
            qty = int(requested // entry_price)
            if qty <= 0:
                raise ValueError(
                    "Robinhood limit mode needs at least one whole share. Use ROBINHOOD_ORDER_TYPE=market for capped fractional/notional orders."
                )
            return BrokerOrderRequest(
                symbol=ticker,
                side=side,
                order_type="limit",
                quantity=qty,
                limit_price=entry_price,
                requested_notional=round(qty * entry_price, 2),
                direction=direction,
                stop_loss=stop_loss,
                target_1=trade_params.get("target_1", 0),
                target_2=trade_params.get("target_2", 0),
                market_hours=getattr(self.settings, "robinhood_market_hours", "regular_hours"),
            )

        if shares <= 0:
            raise ValueError("Invalid trade parameters (shares <= 0)")
        side = "sell" if direction == "short" else "buy"
        return BrokerOrderRequest(
            symbol=ticker,
            side=side,
            order_type="limit",
            quantity=shares,
            limit_price=entry_price,
            requested_notional=round(shares * entry_price, 2),
            direction=direction,
            stop_loss=stop_loss,
            target_1=trade_params.get("target_1", 0),
            target_2=trade_params.get("target_2", 0),
        )

    def _desired_notional(self, trade_params: dict, shares: float, entry_price: float) -> float:
        for key in ("dollar_amount", "notional", "requested_notional"):
            value = trade_params.get(key)
            if value:
                return float(value)
        if shares > 0:
            return float(shares) * entry_price
        return float(getattr(self.settings, "robinhood_max_order_notional", 5.0))

    def _check_runtime_limits(
        self,
        broker_name: str,
        ticker: str,
        request: BrokerOrderRequest,
        positions: list[dict],
    ) -> dict:
        if broker_name != "robinhood":
            return {"allowed": True, "request": request}

        reasons = []
        allowed_symbols = _csv_set(getattr(self.settings, "robinhood_allowed_symbols", ""))
        blocked_symbols = _csv_set(getattr(self.settings, "robinhood_blocked_symbols", ""))
        if allowed_symbols and ticker.upper() not in allowed_symbols:
            reasons.append(f"{ticker} is not in ROBINHOOD_ALLOWED_SYMBOLS.")
        if ticker.upper() in blocked_symbols:
            reasons.append(f"{ticker} is blocked by ROBINHOOD_BLOCKED_SYMBOLS.")

        max_positions = int(getattr(self.settings, "robinhood_max_open_positions", 3))
        if len(positions) >= max_positions and not any(p.get("ticker") == ticker.upper() for p in positions):
            reasons.append(f"Robinhood max open positions reached ({len(positions)}/{max_positions}).")

        # Enforce caps on the order's REAL dollar exposure (dollar_amount for
        # market, quantity*limit_price for limit). Fail closed — reject rather
        # than silently rewrite, which previously left limit orders uncapped and
        # corrupted the daily tally.
        max_order = float(getattr(self.settings, "robinhood_max_order_notional", 5.0))
        effective = self._effective_notional(request)
        if effective > max_order + 1e-9:
            reasons.append(
                f"Robinhood order notional ${effective:.2f} exceeds per-order cap ${max_order:.2f}."
            )

        daily_cap = float(getattr(self.settings, "robinhood_max_daily_notional", 10.0))
        used_today = self._daily_notional_used("robinhood")
        if effective + used_today > daily_cap + 1e-9:
            reasons.append(
                f"Robinhood daily notional cap would be exceeded "
                f"(${used_today:.2f} used + ${effective:.2f} new / ${daily_cap:.2f} cap)."
            )
        return {"allowed": not reasons, "reasons": reasons, "request": request}

    def _effective_notional(self, request: BrokerOrderRequest) -> float:
        """Real dollar exposure of an order: explicit dollar_amount, else
        quantity*limit_price, else the requested_notional estimate."""
        if request.dollar_amount is not None:
            return float(request.dollar_amount)
        if request.quantity and request.limit_price:
            return float(request.quantity) * float(request.limit_price)
        return float(request.requested_notional or 0.0)

    def _daily_notional_used(self, broker: str) -> float:
        today = datetime.utcnow().date()
        with get_session() as session:
            rows = (
                session.query(OrderEvent)
                .filter(OrderEvent.broker == broker)
                .filter(OrderEvent.event_type == "placed")
                .all()
            )
            total = 0.0
            for row in rows:
                if row.created_at and row.created_at.date() == today:
                    total += row.notional or 0.0
            return total

    def _create_trade_record(
        self,
        ticker_id: int,
        memo_id: int,
        trade_params: dict,
        signal_breakdown: dict,
        regime_data: dict,
        review: BrokerOrderReview,
        status: str,
        order_id: str,
        stop_order_id: str,
        order_strategy: str,
        filled_notional: float | None,
    ) -> int | None:
        broker_name = review.broker
        request = review.request
        shares = int(request.quantity or trade_params.get("shares", 0) or 0)
        entry_price = request.limit_price or trade_params.get("entry_price", 0)
        stop_loss = request.stop_loss or trade_params.get("stop_loss", 0)
        try:
            with get_session() as session:
                trade = Trade(
                    ticker_id=ticker_id,
                    memo_id=memo_id,
                    direction=request.direction or trade_params.get("direction", "long"),
                    entry_price=entry_price,
                    entry_date=None,
                    shares=shares,
                    stop_loss=stop_loss,
                    target_1=request.target_1 or trade_params.get("target_1", 0),
                    target_2=request.target_2 or trade_params.get("target_2", 0),
                    position_pct=trade_params.get("position_pct", 0),
                    status=status,
                    setup_type=trade_params.get("setup_type", ""),
                    signal_scores=json.dumps(signal_breakdown),
                    regime_at_entry=regime_data.get("regime", "neutral"),
                    alpaca_entry_order_id=order_id if broker_name == "alpaca" else None,
                    alpaca_stop_order_id=stop_order_id if broker_name == "alpaca" else None,
                    broker=broker_name,
                    broker_account_id=self._account_id(getattr(self.broker, "active", self.broker)),
                    broker_order_id=order_id,
                    broker_stop_order_id=stop_order_id,
                    broker_order_strategy=order_strategy,
                    order_review_json=json.dumps(review.raw),
                    execution_mode=str(getattr(self.settings, "execution_mode", "paper")).lower(),
                    requested_notional=request.requested_notional,
                    filled_notional=filled_notional,
                    operator_notes=f"ORDER_STRATEGY:{order_strategy}",
                )
                session.add(trade)
                session.flush()
                return trade.id
        except Exception as e:
            log.error("trade_record_failed", error=str(e))
            return None

    def _record_order_event(
        self,
        broker: str,
        event_type: str,
        status: str,
        raw_payload: dict,
        trade_id: int | None = None,
        memo_id: int | None = None,
        account_id: str | None = None,
        order_id: str | None = None,
        notional: float | None = None,
    ) -> None:
        try:
            with get_session() as session:
                session.add(
                    OrderEvent(
                        trade_id=trade_id,
                        memo_id=memo_id,
                        broker=broker,
                        account_id=account_id,
                        order_id=order_id,
                        event_type=event_type,
                        status=status,
                        notional=notional,
                        raw_payload=json.dumps(raw_payload or {}),
                    )
                )
        except Exception as e:
            log.error("order_event_record_failed", broker=broker, event_type=event_type, error=str(e))

    def _account_id(self, broker) -> str:
        return getattr(broker, "account_number", "") or ""


def _csv_set(value: str) -> set[str]:
    return {item.strip().upper() for item in (value or "").split(",") if item.strip()}
