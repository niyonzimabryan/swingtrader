"""The daily ``portfolio_snapshots`` row (Spec L §4.4).

Computed after the close, from the ledger and from stored daily returns. Zero
model calls — Spec K §5 requires every scheduled job to be pure Python, and a
statistic a model produced is not a statistic.

The row is what Spec N reads when it reports "this setup would add to an
exposure you already have", so it stores the exposure picture *and* the risk
figures that a sector code cannot express: beta over two windows, realized
volatility, and the maximum and average pairwise correlation among the largest
positions. Each carries its lookback and its ``n``; a figure whose sample is too
short is stored **null with a reason** in ``metrics_notes_json``.

One row per date, replaced in place on a re-run. That is the one deliberate
exception to the append-only rule, and it is not a position: a snapshot is a
derived summary of rows that *are* kept append-only, so recomputing one
destroys nothing that cannot be rebuilt.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select

from database.models import PortfolioSnapshot
from portfolio.exposure import aggregate_exposure
from portfolio.ledger import current_accounts, current_cash, current_holdings, exposure_tags
from portfolio.metrics import compute_risk_metrics, inputs_hash
from portfolio.settlement import settlement_view
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("portfolio_snapshot")

#: How many of the largest positions are listed on the row.
TOP_POSITIONS = 10


def build_snapshot(
    session,
    *,
    on_date: date | None = None,
    now: datetime | None = None,
    returns_by_symbol: dict | None = None,
    benchmark_returns=None,
    sectors: dict | None = None,
    sync_id: str = "",
) -> PortfolioSnapshot:
    """Compute and store the snapshot for ``on_date``.

    ``returns_by_symbol`` and ``benchmark_returns`` are passed in rather than
    read here: the price store is a separate concern, and a snapshot that could
    silently compute a beta from a half-filled series would be worse than one
    that reports why it did not.
    """
    now = now or utcnow_naive()
    on_date = on_date or now.date()

    accounts = current_accounts(session, enabled_only=True)
    labels = {a.id: (a.label or f"{a.broker}:{a.external_account_id}") for a in accounts}
    account_ids = [a.id for a in accounts]
    holdings = current_holdings(session, account_ids)
    cash_rows = current_cash(session, account_ids)

    settled = sum(float(r.settled_cash or 0.0) for r in cash_rows)
    unsettled = sum(float(r.unsettled_cash or 0.0) for r in cash_rows)
    spendable = sum(
        settlement_view(
            settled_cash=r.settled_cash,
            unsettled_cash=r.unsettled_cash,
            pending_settlements=r.pending_settlements,
            as_of_date=on_date,
        ).available
        for r in cash_rows
    )

    exposure = aggregate_exposure(
        holdings,
        cash_total=settled + unsettled,
        sectors=sectors,
        tags=exposure_tags(session),
        account_labels=labels,
    )

    metrics = compute_risk_metrics(
        returns_by_symbol or {}, exposure.weights, benchmark_returns or []
    )

    largest = [row.as_dict() for row in exposure.by_symbol[:TOP_POSITIONS]]
    payload = {
        "date": on_date.isoformat(),
        "holdings": [
            (h.symbol, h.instrument_type, h.quantity, h.market_value) for h in holdings
        ],
        "cash": [(r.settled_cash, r.unsettled_cash) for r in cash_rows],
        "returns_symbols": sorted(returns_by_symbol or {}),
        "benchmark_n": len(benchmark_returns or []),
    }

    row = session.execute(
        select(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_date == on_date)
    ).scalar_one_or_none()
    if row is None:
        row = PortfolioSnapshot(snapshot_date=on_date, created_at=now)
        session.add(row)

    row.as_of_utc = now
    row.sync_id = sync_id
    row.total_value = exposure.total_value
    row.cash_total = settled + unsettled
    row.settled_cash = spendable
    row.unsettled_cash = unsettled
    row.gross_exposure = exposure.gross_exposure
    row.net_exposure = exposure.net_exposure
    row.sector_weights_json = json.dumps(exposure.by_sector, sort_keys=True)
    row.name_weights_json = json.dumps(exposure.weights, sort_keys=True)
    row.largest_positions_json = json.dumps(largest, sort_keys=True)
    row.largest_position_weight = exposure.largest_position_weight
    row.concentration_hhi = exposure.concentration_hhi
    row.beta_60 = metrics.beta_60
    row.beta_60_n = metrics.beta_60_n
    row.beta_250 = metrics.beta_250
    row.beta_250_n = metrics.beta_250_n
    row.realized_volatility = metrics.realized_volatility
    row.realized_volatility_n = metrics.realized_volatility_n
    row.max_pairwise_correlation = metrics.max_pairwise_correlation
    row.avg_pairwise_correlation = metrics.avg_pairwise_correlation
    row.correlation_lookback_sessions = metrics.correlation_lookback_sessions
    row.correlation_names = metrics.correlation_names
    row.metrics_notes_json = json.dumps(metrics.notes, sort_keys=True)
    row.inputs_hash = inputs_hash(payload)
    session.flush()

    log.info(
        "portfolio_snapshot_written",
        date=on_date.isoformat(),
        total_value=row.total_value,
        gross_exposure=row.gross_exposure,
        beta_60=row.beta_60,
        max_pairwise_correlation=row.max_pairwise_correlation,
        inputs_hash=row.inputs_hash,
    )
    return row


def snapshot_payload(row: PortfolioSnapshot) -> dict:
    """The row as a dict, with every figure beside its lookback and ``n``."""
    return {
        "snapshot_date": row.snapshot_date.isoformat(),
        "as_of_utc": row.as_of_utc.isoformat() if row.as_of_utc else None,
        "total_value": row.total_value,
        "cash_total": row.cash_total,
        "settled_cash": row.settled_cash,
        "unsettled_cash": row.unsettled_cash,
        "gross_exposure": row.gross_exposure,
        "net_exposure": row.net_exposure,
        "largest_position_weight": row.largest_position_weight,
        "concentration_hhi": row.concentration_hhi,
        "risk": {
            "beta_60": {"value": row.beta_60, "sessions": 60, "n": row.beta_60_n},
            "beta_250": {"value": row.beta_250, "sessions": 250, "n": row.beta_250_n},
            "realized_volatility": {
                "value": row.realized_volatility,
                "n": row.realized_volatility_n,
                "annualized": True,
            },
            "pairwise_correlation": {
                "max": row.max_pairwise_correlation,
                "average": row.avg_pairwise_correlation,
                "lookback_sessions": row.correlation_lookback_sessions,
                "names": row.correlation_names,
            },
            "notes": json.loads(row.metrics_notes_json or "{}"),
        },
        "inputs_hash": row.inputs_hash,
    }
