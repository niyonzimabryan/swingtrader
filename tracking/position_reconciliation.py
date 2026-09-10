"""Broker position reconciliation for DB trade tracking.

Two reconciliations live here, and they answer different questions.

:func:`reconcile_broker_positions` is the original one: *the broker holds this,
does a ``trades`` row exist for it?* It creates or updates rows so a position
opened outside the system is still tracked. It is best-effort by design.

:func:`reconcile_executions` is the Spec Q §12 one, and it runs the comparison
the other way round: *this execution believes it holds a position — does the
broker agree?* Four answers, and only one of them is "fine":

``matched``               the broker's quantity equals the execution's.
``missing_at_broker``     the execution filled but the broker shows nothing.
``quantity_mismatch``     both hold, at different sizes (a partial fill that
                          was never adjusted, or a manual sale).
``unexpected_at_broker``  the broker holds a name that no non-terminal
                          execution accounts for.

It **reports**; it does not transition anything. The §12 machine is in
``strategy_lab/execution.py`` and the transitions are applied by
``execution/strategy_lifecycle.py``, so this module stays free of the state
machine and importable from anywhere — which is what lets the monitor call it
without dragging the execution package in behind it.

Robinhood positions opened through the Phase 6/Phase 5 path used to be outside
reconciliation entirely: the monitors filter to Alpaca, and nothing compared a
``strategy_trades`` row against the broker at all. That is the gap this closes.
A flow this function cannot evaluate — a mode it does not know, an execution
whose ticker cannot be resolved — is reported as ``unsupported`` and fails
closed, because "I could not check" and "it is fine" must never be the same
answer here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from utils.timeutils import utcnow_naive

from sqlalchemy import or_

from database.db import get_session
from database.models import StrategyDecision, StrategyTrade, Ticker, Trade
from strategy_lab.domain import TERMINAL_EXECUTION_STATES, ExecutionState
from utils.logger import get_logger

log = get_logger("position_reconciliation")

ACTIVE_TRADE_STATUSES = ("open", "pending_fill")

#: Execution states in which the system believes it holds shares at the broker.
#: ``partially_filled`` is included: a partial fill is a real position.
POSITION_BEARING_STATES: tuple[str, ...] = (
    ExecutionState.PARTIALLY_FILLED.value,
    ExecutionState.FILLED.value,
    ExecutionState.PROTECTION_PENDING.value,
    ExecutionState.PROTECTED.value,
    ExecutionState.PROTECTION_FAILED.value,
    ExecutionState.CLOSING.value,
)


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
                    entry_date=utcnow_naive(),
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
        trade.entry_date = utcnow_naive()
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


# --------------------------------------------------------------------------- #
# Spec Q §12 reconciliation: does the broker agree with the execution ledger?
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionFinding:
    """One disagreement (or agreement) between an execution and the broker."""

    kind: str
    execution_id: str = ""
    ticker: str = ""
    expected_quantity: float = 0.0
    broker_quantity: float = 0.0
    detail: str = ""

    @property
    def is_mismatch(self) -> bool:
        return self.kind != "matched"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "execution_id": self.execution_id,
            "ticker": self.ticker,
            "expected_quantity": self.expected_quantity,
            "broker_quantity": self.broker_quantity,
            "detail": self.detail,
        }


@dataclass
class ExecutionReconciliation:
    """Everything one reconciliation pass found, grouped by kind."""

    findings: list[ExecutionFinding] = field(default_factory=list)

    @property
    def mismatches(self) -> list[ExecutionFinding]:
        return [f for f in self.findings if f.is_mismatch]

    def of_kind(self, kind: str) -> list[ExecutionFinding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict:
        return {"findings": [f.as_dict() for f in self.findings], "ok": self.ok}


MATCHED = "matched"
MISSING_AT_BROKER = "missing_at_broker"
QUANTITY_MISMATCH = "quantity_mismatch"
UNEXPECTED_AT_BROKER = "unexpected_at_broker"
UNSUPPORTED = "unsupported"

#: Whole shares, so equality is exact rather than a float comparison; the
#: tolerance exists only for adapters that report a quantity as a float.
_QUANTITY_TOLERANCE = 1e-6


def reconcile_executions(
    session,
    *,
    positions,
    mode: str,
    known_tickers=None,
) -> ExecutionReconciliation:
    """Compare every position-bearing execution in ``mode`` against ``positions``.

    ``positions`` is the adapter's ``get_positions_detail()`` output, normalized
    here with the same helper the legacy path uses. ``known_tickers`` narrows the
    "unexpected at broker" check to names this system is responsible for; without
    it, every unrelated holding in a shared account would read as a mismatch, and
    a reconciliation that cries wolf gets muted, which is worse than not running.

    Reports only. The caller applies the §12 transitions.
    """
    report = ExecutionReconciliation()
    by_symbol: dict[str, float] = {}
    for raw in positions or []:
        normalized = _normalize_position(raw)
        if not normalized["symbol"]:
            continue
        by_symbol[normalized["symbol"]] = by_symbol.get(normalized["symbol"], 0.0) + normalized["qty"]

    rows = (
        session.query(StrategyTrade)
        .filter(StrategyTrade.mode == mode)
        .filter(StrategyTrade.status.in_(POSITION_BEARING_STATES))
        .order_by(StrategyTrade.id.asc())
        .all()
    )

    accounted: set[str] = set()
    for trade in rows:
        decision = session.get(StrategyDecision, trade.decision_id)
        ticker = (getattr(decision, "ticker", "") or "").upper()
        if not ticker:
            # Fail closed: an execution whose ticker cannot be resolved cannot
            # be checked, and "cannot be checked" is not "fine".
            report.findings.append(
                ExecutionFinding(
                    kind=UNSUPPORTED,
                    execution_id=trade.execution_id,
                    detail=(
                        f"execution {trade.execution_id} has no resolvable ticker "
                        f"(decision {trade.decision_id}), so its broker position "
                        "cannot be reconciled."
                    ),
                )
            )
            continue
        accounted.add(ticker)
        expected = float(trade.quantity or 0.0)
        actual = by_symbol.get(ticker, 0.0)
        if actual <= 0:
            report.findings.append(
                ExecutionFinding(
                    kind=MISSING_AT_BROKER,
                    execution_id=trade.execution_id,
                    ticker=ticker,
                    expected_quantity=expected,
                    broker_quantity=0.0,
                    detail=(
                        f"execution {trade.execution_id} is {trade.status} and "
                        f"expects {expected:g} shares of {ticker}, but the broker "
                        "reports no position."
                    ),
                )
            )
        elif abs(actual - expected) > _QUANTITY_TOLERANCE:
            report.findings.append(
                ExecutionFinding(
                    kind=QUANTITY_MISMATCH,
                    execution_id=trade.execution_id,
                    ticker=ticker,
                    expected_quantity=expected,
                    broker_quantity=actual,
                    detail=(
                        f"execution {trade.execution_id} expects {expected:g} "
                        f"shares of {ticker}; the broker reports {actual:g}."
                    ),
                )
            )
        else:
            report.findings.append(
                ExecutionFinding(
                    kind=MATCHED,
                    execution_id=trade.execution_id,
                    ticker=ticker,
                    expected_quantity=expected,
                    broker_quantity=actual,
                )
            )

    watched = {t.upper() for t in (known_tickers or ())}
    for symbol, quantity in sorted(by_symbol.items()):
        if symbol in accounted or (watched and symbol not in watched):
            continue
        report.findings.append(
            ExecutionFinding(
                kind=UNEXPECTED_AT_BROKER,
                ticker=symbol,
                broker_quantity=quantity,
                detail=(
                    f"the broker reports {quantity:g} shares of {symbol} that no "
                    "non-terminal execution accounts for."
                ),
            )
        )

    if report.mismatches:
        log.warning(
            "execution_reconciliation_mismatch",
            mode=mode,
            kinds=sorted({f.kind for f in report.mismatches}),
            count=len(report.mismatches),
        )
    return report


def open_execution_tickers(session, *, mode: str) -> set[str]:
    """Every ticker a non-terminal execution in ``mode`` is responsible for."""
    rows = (
        session.query(StrategyTrade)
        .filter(StrategyTrade.mode == mode)
        .filter(StrategyTrade.status.notin_([s.value for s in TERMINAL_EXECUTION_STATES]))
        .all()
    )
    out: set[str] = set()
    for trade in rows:
        decision = session.get(StrategyDecision, trade.decision_id)
        ticker = (getattr(decision, "ticker", "") or "").upper()
        if ticker:
            out.add(ticker)
    return out
