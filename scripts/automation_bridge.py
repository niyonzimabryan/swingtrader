"""CLI bridge for local automation.

This module exposes read/scan operations only. It does not bypass Telegram
approval, broker review, or order risk checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from config.settings import Settings
from database.db import get_session, init_db
from database.models import Memo, OrderEvent, PipelineRun, Trade
from execution.brokers import create_brokers
from tracking.attribution import get_signal_attribution


def main() -> int:
    argv = [arg for arg in sys.argv[1:] if arg != "--json"]
    parser = argparse.ArgumentParser(description="Swing Trader automation bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("positions")
    sub.add_parser("orders")
    sub.add_parser("attribution")
    sub.add_parser("scan")
    memos = sub.add_parser("memos")
    memos.add_argument("--latest", type=int, default=10)
    memo = sub.add_parser("memo")
    memo.add_argument("--id", type=int, required=True)
    runs = sub.add_parser("runs")
    runs.add_argument("--latest", type=int, default=10)

    args = parser.parse_args(argv)
    settings = Settings()
    init_db(settings.database_url)

    if args.command == "status":
        payload = _status(settings)
    elif args.command == "positions":
        payload = {"positions": _broker(settings).get_positions_detail()}
    elif args.command == "orders":
        payload = {"orders": _broker(settings).get_orders()}
    elif args.command == "attribution":
        payload = get_signal_attribution()
    elif args.command == "memos":
        payload = {"memos": _latest_memos(args.latest)}
    elif args.command == "memo":
        payload = _memo(args.id)
    elif args.command == "runs":
        payload = {"runs": _latest_runs(args.latest)}
    elif args.command == "scan":
        from orchestrator.pipeline import TradingPipeline

        pipeline = TradingPipeline(settings)
        pipeline.run_full_scan()
        payload = {"status": "scan_complete", "timestamp": datetime.utcnow().isoformat()}
    else:
        raise SystemExit(f"Unknown command: {args.command}")

    print(json.dumps(payload, default=str, indent=2, sort_keys=True))
    return 0


def _broker(settings):
    _, _, _, router = create_brokers(settings)
    return router


def _status(settings) -> dict:
    broker = _broker(settings)
    return {
        "broker": getattr(broker.active, "name", "unknown"),
        "primary_broker": settings.broker_primary,
        "execution_mode": settings.execution_mode,
        "allow_live_trading": settings.allow_live_trading,
        "account": broker.get_account_info(),
        "positions": broker.get_positions_detail(),
        "risk": {
            "max_concurrent_positions": settings.max_concurrent_positions,
            "max_position_pct": settings.max_position_pct,
            "daily_loss_halt_pct": settings.daily_loss_halt_pct,
            "drawdown_circuit_breaker_pct": settings.drawdown_circuit_breaker_pct,
            "robinhood_max_order_notional": settings.robinhood_max_order_notional,
            "robinhood_max_daily_notional": settings.robinhood_max_daily_notional,
            "robinhood_max_open_positions": settings.robinhood_max_open_positions,
        },
    }


def _latest_memos(limit: int) -> list[dict]:
    with get_session() as session:
        rows = session.query(Memo).order_by(Memo.created_at.desc()).limit(limit).all()
        return [_memo_row(row) for row in rows]


def _memo(memo_id: int) -> dict:
    with get_session() as session:
        row = session.query(Memo).filter_by(id=memo_id).first()
        if not row:
            return {"error": "memo_not_found", "id": memo_id}
        return _memo_row(row, include_text=True)


def _memo_row(row: Memo, include_text: bool = False) -> dict:
    payload = {
        "id": row.id,
        "ticker": row.ticker.symbol if row.ticker else None,
        "score": row.composite_score,
        "classification": row.classification,
        "direction": row.direction,
        "status": row.status,
        "created_at": row.created_at,
        "responded_at": row.responded_at,
        "trade_params": row.trade_params_dict,
    }
    if include_text:
        payload["full_text"] = row.full_text
        payload["memo_data"] = row.memo_data_dict
    return payload


def _latest_runs(limit: int) -> list[dict]:
    with get_session() as session:
        rows = session.query(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit).all()
        return [
            {
                "run_id": r.run_id,
                "trigger_source": r.trigger_source,
                "status": r.status,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "scanned_count": r.scanned_count,
                "screened_count": r.screened_count,
                "researched_count": r.researched_count,
                "memos_generated": r.memos_generated,
                "duration_s": r.duration_s,
                "errors": _loads(r.errors_json, []),
                "metadata": _loads(r.metadata_json, {}),
            }
            for r in rows
        ]


def _loads(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback


if __name__ == "__main__":
    raise SystemExit(main())
