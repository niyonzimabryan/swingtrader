#!/usr/bin/env python3
"""Run one portfolio sync, or register it on a scheduler.

    python -m scripts.portfolio_sync --dry-run
    python -m scripts.portfolio_sync
    python -m scripts.portfolio_sync --force-mass-deletion

This is the seam. ``portfolio/`` may not import ``execution/`` — the workspace
service reads the ledger, and Spec L §6 forbids any agent-reachable path from
reaching a broker — so the module that knows about both is this one, and the
workspace does not import it.

``--dry-run`` fetches and reports without writing, which is how to check a
broker connection without touching the ledger.
"""

from __future__ import annotations

import argparse
import json
import sys

from utils.logger import get_logger, setup_logging

log = get_logger("portfolio_sync")


def build_broker(settings, *, fake: bool = False):
    """The adapter the sync reads from.

    Imported inside the function, not at module scope: ``scripts/`` is on the
    import-graph walker's first-party list, and a top-level broker import here
    would put ``execution`` in the closure of anything that imported this
    module for any reason.
    """
    if fake:
        from execution.brokers.fake import FakeBroker

        return FakeBroker()
    from execution.brokers.robinhood import RobinhoodMCPBroker

    return RobinhoodMCPBroker(settings)


def build_pager(settings):
    """The out-of-band page. Structured log today (Spec K §7 keeps Telegram).

    Deliberately not wired to the Telegram bot from here: the bot is a separate
    process and importing it to page would start a second one. The log line is
    what Railway alerts on; a Telegram-backed pager is a one-line substitution
    when the alert path is moved.
    """
    from portfolio.paging import log_pager

    return log_pager


def run_once(
    settings,
    *,
    fake: bool = False,
    dry_run: bool = False,
    force_mass_deletion: bool = False,
    reason: str = "manual",
    snapshot: bool = False,
) -> dict:
    from database.db import get_session, init_db
    from database import db as db_module
    from portfolio import reconcile
    from portfolio.sync import run_sync

    if db_module.SessionLocal is None:
        init_db(settings.database_url)

    broker = build_broker(settings, fake=fake)

    if dry_run:
        snapshot = broker.fetch_ledger_snapshot()
        return {
            "dry_run": True,
            "broker": snapshot.broker,
            "error": snapshot.error,
            "accounts": [
                {
                    "account": a.account.external_account_id,
                    "agent_placeable": a.account.agent_placeable,
                    "account_type": a.account.account_type,
                    "holdings": len(a.holdings),
                    "tax_lots": len(a.tax_lots),
                    "orders": len(a.orders),
                    "has_cash": a.cash is not None,
                    "error": a.error,
                }
                for a in snapshot.accounts
            ],
        }

    threshold = float(getattr(settings, "portfolio_mass_deletion_threshold", 0.5) or 0.5)
    with get_session() as session:
        result = run_sync(
            session,
            broker,
            pager=build_pager(settings),
            threshold=threshold,
            force_mass_deletion=force_mass_deletion,
            reconciler=reconcile.detect_mismatches,
        )
    payload = result.as_dict()
    payload["reason"] = reason
    if snapshot:
        payload["snapshot"] = write_snapshot(sync_id=result.sync_id)
    return payload


def write_snapshot(*, sync_id: str = "") -> dict:
    """The daily rollup, computed after the close (Spec L §4.4).

    Return series come from the local ``price_data`` table. Where a name has no
    price history the risk figures are stored null with the reason rather than
    computed from whatever was available.
    """
    from database.db import get_session
    from portfolio import returns as returns_module
    from portfolio.ledger import current_accounts, current_holdings
    from portfolio.snapshots import build_snapshot, snapshot_payload

    with get_session() as session:
        accounts = current_accounts(session)
        holdings = current_holdings(session, [a.id for a in accounts])
        symbols = {h.symbol for h in holdings if h.instrument_type == "equity"}
        row = build_snapshot(
            session,
            sync_id=sync_id,
            returns_by_symbol=returns_module.returns_for(session, symbols),
            benchmark_returns=returns_module.benchmark_returns(session),
        )
        return snapshot_payload(row)


def register_on(scheduler, settings) -> list[str]:
    """Attach the Spec L §4 cadence to a running APScheduler.

    Called by the bot process. The callable it registers checks the holiday
    table at fire time, because a cron expression cannot express "not
    Thanksgiving".
    """
    from portfolio import schedule

    def _job(reason: str = "scheduled"):
        if not schedule.should_run_now():
            log.info("portfolio_sync_skipped", reason="market_closed", trigger=reason)
            return None
        # The daily snapshot is computed once, after the close — not on every
        # intraday run, where it would be a partial day's rollup written under
        # today's date.
        return run_once(
            settings,
            reason=reason,
            snapshot=(reason == schedule.POST_CLOSE_JOB_ID),
        )

    return schedule.register(scheduler, settings, _job)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one portfolio ledger sync.")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="also write the daily portfolio_snapshots row (the post-close job does this)",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="use the in-memory fake broker instead of Robinhood",
    )
    parser.add_argument(
        "--force-mass-deletion",
        action="store_true",
        help=(
            "write a sync that would drop more than the configured fraction of "
            "known holdings. Only after a human has confirmed the positions are "
            "actually gone."
        ),
    )
    args = parser.parse_args(argv)

    setup_logging()
    from config.settings import Settings

    settings = Settings()
    if not settings.portfolio_sync_enabled and not (args.dry_run or args.fake):
        print(
            "PORTFOLIO_SYNC_ENABLED is false. Set it true to write to the "
            "ledger, or pass --dry-run to check the broker connection without "
            "writing.",
            file=sys.stderr,
        )
        return 2

    payload = run_once(
        settings,
        fake=args.fake,
        dry_run=args.dry_run,
        force_mass_deletion=args.force_mass_deletion,
        snapshot=args.snapshot,
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0 if (payload.get("ok") or payload.get("dry_run")) else 1


if __name__ == "__main__":
    sys.exit(main())
