"""Broker position reconciliation for DB trade tracking."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_

from database.db import get_session
from database.models import Ticker, Trade
from utils.logger import get_logger

log = get_logger("position_reconciliation")

ACTIVE_TRADE_STATUSES = ("open", "pending_fill")


def reconcile_broker_positions(
    positions: list[dict],
    broker_name: str = "alpaca",
    broker_account_id: str | None = None,
    execution_mode: str = "paper",
    source: str = "broker_position_reconciliation",
) -> dict:
    """Create or update active Trade rows for positions already held at broker."""
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    broker = (broker_name or "alpaca").lower()
    account_id = broker_account_id or None

    with get_session() as session:
        for raw_position in positions or []:
            position = _normalize_position(raw_position)
            symbol = position.get("symbol")
            if not symbol:
                skipped.append("?")
                continue
            if position["qty"] <= 0 or position["entry_price"] <= 0:
                skipped.append(symbol)
                continue

            ticker = _get_or_create_ticker(session, symbol)
            trade = _find_active_trade(session, symbol, broker, account_id)

            if trade:
                if _update_trade_from_position(trade, position, broker, account_id, execution_mode, source):
                    updated.append(symbol)
                continue

            session.add(
                Trade(
                    ticker_id=ticker.id,
                    direction=position["direction"],
                    entry_price=position["entry_price"],
                    entry_date=datetime.utcnow(),
                    shares=position["shares"],
                    stop_loss=0,
                    target_1=0,
                    target_2=0,
                    position_pct=0,
                    status="open",
                    setup_type="broker_reconciled",
                    signal_scores="{}",
                    regime_at_entry="unknown",
                    broker=broker,
                    broker_account_id=account_id,
                    broker_order_strategy="broker_position_reconciliation",
                    execution_mode=execution_mode,
                    requested_notional=None,
                    filled_notional=position["notional"],
                    operator_notes=_reconciliation_note(source),
                )
            )
            created.append(symbol)

    if created or updated or skipped:
        log.info(
            "broker_positions_reconciled",
            broker=broker,
            created=created,
            updated=updated,
            skipped=skipped,
            source=source,
        )
    return {"created": created, "updated": updated, "skipped": skipped}


def _normalize_position(position: dict) -> dict:
    symbol = str(position.get("ticker") or position.get("symbol") or "").upper()
    raw_qty = _float_value(position.get("qty") or position.get("quantity") or position.get("shares")) or 0.0
    qty = abs(raw_qty)
    entry_price = _float_value(position.get("entry_price") or position.get("avg_entry_price") or position.get("average_price")) or 0.0
    market_value = _float_value(position.get("market_value"))
    notional = market_value if market_value is not None else qty * entry_price
    side = str(position.get("side") or "").lower()
    direction = "short" if side == "short" or raw_qty < 0 else "long"
    return {
        "symbol": symbol,
        "qty": qty,
        "shares": int(qty),
        "entry_price": entry_price,
        "direction": direction,
        "notional": abs(notional or 0.0),
    }


def _get_or_create_ticker(session, symbol: str) -> Ticker:
    ticker = session.query(Ticker).filter(Ticker.symbol == symbol).first()
    if ticker:
        return ticker
    ticker = Ticker(symbol=symbol, name=symbol, in_universe=False)
    session.add(ticker)
    session.flush()
    return ticker


def _find_active_trade(session, symbol: str, broker: str, broker_account_id: str | None) -> Trade | None:
    query = (
        session.query(Trade)
        .join(Ticker)
        .filter(Ticker.symbol == symbol, Trade.status.in_(ACTIVE_TRADE_STATUSES))
    )
    if broker == "alpaca":
        query = query.filter(or_(Trade.broker == "alpaca", Trade.broker.is_(None)))
    else:
        query = query.filter(Trade.broker == broker)
    if broker_account_id:
        query = query.filter(or_(Trade.broker_account_id == broker_account_id, Trade.broker_account_id.is_(None)))
    return query.order_by(Trade.entry_date.desc().nullslast(), Trade.created_at.desc()).first()


def _update_trade_from_position(
    trade: Trade,
    position: dict,
    broker: str,
    broker_account_id: str | None,
    execution_mode: str,
    source: str,
) -> bool:
    changed = False
    updates = {
        "status": "open",
        "direction": position["direction"],
        "entry_price": position["entry_price"],
        "shares": position["shares"],
        "filled_notional": position["notional"],
        "broker": broker,
        "execution_mode": execution_mode,
    }
    if broker_account_id:
        updates["broker_account_id"] = broker_account_id

    for field, value in updates.items():
        if getattr(trade, field) != value:
            setattr(trade, field, value)
            changed = True

    if not trade.entry_date:
        trade.entry_date = datetime.utcnow()
        changed = True

    note = _reconciliation_note(source)
    if note not in (trade.operator_notes or ""):
        trade.operator_notes = f"{trade.operator_notes or ''}|{note}".strip("|")
        changed = True

    return changed


def _reconciliation_note(source: str) -> str:
    return f"RECONCILED_FROM_BROKER_POSITION:{source}"


def _float_value(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
