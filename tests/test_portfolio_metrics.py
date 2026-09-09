"""Snapshot risk figures, the sync cadence, and the token-refresh log.

Spec L §3's risk block is the part most likely to be wrong in a way nobody
notices: a beta computed from twelve sessions renders exactly like one computed
from two hundred and fifty. So the tests here are mostly about **refusal** —
what the code declines to compute, and whether it says why.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace

from portfolio.metrics import (
    MIN_OBSERVATIONS,
    beta,
    compute_risk_metrics,
    correlation,
    inputs_hash,
    pairwise_correlations,
    portfolio_returns,
    realized_volatility,
)
from portfolio.paging import RecordingPager
from portfolio.schedule import (
    INTRADAY_JOB_ID,
    POST_CLOSE_JOB_ID,
    PRE_MARKET_JOB_ID,
    build_triggers,
    register,
    should_run_now,
)
from portfolio.snapshots import build_snapshot, snapshot_payload
from portfolio.sync import run_sync
from tests import portfoliofixture as fx
from tests.dbfixture import TestDatabase
from utils.market_hours import ET

SETTINGS = SimpleNamespace(
    portfolio_sync_enabled=True,
    portfolio_sync_interval_minutes=60,
    portfolio_sync_pre_market_hour=8,
    portfolio_sync_post_close_hour=16,
    portfolio_sync_post_close_minute=30,
)


def _series(values):
    return list(values)


class RiskMetricTests(unittest.TestCase):
    def test_beta_of_a_series_against_itself_is_one(self):
        market = [0.01, -0.02, 0.015, -0.005] * 10
        self.assertAlmostEqual(beta(market, market), 1.0, places=9)
        self.assertAlmostEqual(beta([2 * r for r in market], market), 2.0, places=9)

    def test_a_short_sample_is_null_with_a_reason_not_a_number(self):
        short = [0.01, -0.01, 0.02]
        self.assertIsNone(beta(short, short))
        self.assertIsNone(realized_volatility(short))

        metrics = compute_risk_metrics({"AMD": short}, {"AMD": 1.0}, short)
        self.assertIsNone(metrics.beta_60)
        self.assertIn("beta_60", metrics.notes)
        self.assertIn(str(MIN_OBSERVATIONS), metrics.notes["beta_60"])

    def test_a_constant_series_has_no_correlation_rather_than_zero(self):
        self.assertIsNone(correlation([0.01] * 30, list(range(30))))

    def test_max_and_average_pairwise_are_reported_with_n(self):
        base = [((-1) ** i) * 0.01 + i * 0.0001 for i in range(80)]
        returns = {
            "A": base,
            "B": [r * 1.01 for r in base],
            "C": [-r for r in base],
        }
        max_rho, avg_rho, names, pairs = pairwise_correlations(returns, ["A", "B", "C"])
        self.assertEqual(names, 3)
        self.assertEqual(pairs, 3)
        self.assertAlmostEqual(max_rho, 1.0, places=6)
        self.assertLess(avg_rho, max_rho)

    def test_portfolio_returns_truncate_to_the_shortest_series(self):
        combined = portfolio_returns({"A": [0.01] * 30, "B": [0.02] * 10}, {"A": 0.5, "B": 0.5})
        self.assertEqual(len(combined), 10)
        self.assertAlmostEqual(combined[0], 0.015, places=9)

    def test_every_figure_carries_its_lookback_and_n(self):
        market = [((-1) ** i) * 0.01 for i in range(300)]
        returns = {"A": [1.2 * r for r in market], "B": [0.8 * r for r in market]}
        metrics = compute_risk_metrics(returns, {"A": 0.6, "B": 0.4}, market).as_dict()
        self.assertEqual(metrics["beta_60_n"], 60)
        self.assertEqual(metrics["beta_250_n"], 250)
        self.assertEqual(metrics["correlation_lookback_sessions"], 60)
        self.assertEqual(metrics["correlation_names"], 2)
        self.assertIsNotNone(metrics["realized_volatility_n"])

    def test_an_empty_book_reports_why_rather_than_zeroes(self):
        metrics = compute_risk_metrics({}, {}, [])
        self.assertIsNone(metrics.beta_60)
        self.assertIn("portfolio_returns", metrics.notes)

    def test_inputs_hash_is_stable_and_order_independent(self):
        left = {"b": 2, "a": [1, 2, 3]}
        right = {"a": [1, 2, 3], "b": 2}
        self.assertEqual(inputs_hash(left), inputs_hash(right))
        self.assertNotEqual(inputs_hash(left), inputs_hash({"a": [1, 2, 4], "b": 2}))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("portfolio_snapshot")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        from database.db import get_session

        with get_session() as session:
            run_sync(session, fx.two_account_broker(), pager=RecordingPager(), now=fx.NOW)

    def test_a_snapshot_records_exposure_and_the_risk_block(self):
        from database.db import get_session

        market = [((-1) ** i) * 0.01 for i in range(300)]
        returns = {"AMD": [1.3 * r for r in market], "TDW": [0.7 * r for r in market]}
        with get_session() as session:
            row = build_snapshot(
                session,
                on_date=date(2026, 9, 9),
                now=fx.NOW,
                returns_by_symbol=returns,
                benchmark_returns=market,
                sectors={"AMD": "semiconductors", "TDW": "energy"},
            )
            payload = snapshot_payload(row)

        self.assertEqual(payload["snapshot_date"], "2026-09-09")
        self.assertGreater(payload["gross_exposure"], 0)
        self.assertEqual(payload["risk"]["beta_60"]["sessions"], 60)
        self.assertIsNotNone(payload["risk"]["beta_60"]["value"])
        self.assertEqual(payload["risk"]["pairwise_correlation"]["names"], 2)
        self.assertTrue(payload["inputs_hash"])

        with get_session() as session:
            sectors = json.loads(
                build_snapshot(
                    session,
                    on_date=date(2026, 9, 9),
                    now=fx.NOW,
                    sectors={"AMD": "semiconductors", "TDW": "energy"},
                ).sector_weights_json
            )
        self.assertIn("semiconductors", sectors)

    def test_re_running_a_date_replaces_its_row_rather_than_duplicating_it(self):
        from sqlalchemy import select

        from database.db import get_session
        from database.models import PortfolioSnapshot

        with get_session() as session:
            build_snapshot(session, on_date=date(2026, 9, 9), now=fx.NOW)
            build_snapshot(session, on_date=date(2026, 9, 9), now=fx.NOW)
        with get_session() as session:
            rows = list(session.execute(select(PortfolioSnapshot)).scalars())
        self.assertEqual(len(rows), 1)

    def test_a_snapshot_without_returns_stores_nulls_and_a_reason(self):
        from database.db import get_session

        with get_session() as session:
            payload = snapshot_payload(build_snapshot(session, on_date=date(2026, 9, 9), now=fx.NOW))
        self.assertIsNone(payload["risk"]["beta_60"]["value"])
        self.assertIn("portfolio_returns", payload["risk"]["notes"])


class SyncCadenceTests(unittest.TestCase):
    """Spec L §4: hourly in market hours, once pre-market, once after the close."""

    def test_the_three_triggers_are_built_in_eastern_time(self):
        triggers = build_triggers(SETTINGS)
        self.assertEqual(
            set(triggers), {INTRADAY_JOB_ID, PRE_MARKET_JOB_ID, POST_CLOSE_JOB_ID}
        )
        rendered = {job: str(trigger) for job, trigger in triggers.items()}
        self.assertIn("hour='9-15'", rendered[INTRADAY_JOB_ID])
        self.assertIn("day_of_week='mon-fri'", rendered[INTRADAY_JOB_ID])
        self.assertIn("hour='8'", rendered[PRE_MARKET_JOB_ID])
        self.assertIn("minute='30'", rendered[POST_CLOSE_JOB_ID])

    def test_a_sub_hourly_interval_becomes_a_minute_step(self):
        faster = SimpleNamespace(**{**SETTINGS.__dict__, "portfolio_sync_interval_minutes": 15})
        self.assertIn("minute='*/15'", str(build_triggers(faster)[INTRADAY_JOB_ID]))

    def test_a_market_holiday_is_skipped_at_fire_time(self):
        # Thanksgiving 2026 is a Thursday, so the weekday cron would fire.
        self.assertFalse(should_run_now(datetime(2026, 11, 26, 10, 0, tzinfo=ET)))
        self.assertTrue(should_run_now(datetime(2026, 11, 27, 10, 0, tzinfo=ET)))

    def test_nothing_is_scheduled_while_the_flag_is_off(self):
        scheduler = _RecordingScheduler()
        off = SimpleNamespace(**{**SETTINGS.__dict__, "portfolio_sync_enabled": False})
        self.assertEqual(register(scheduler, off, lambda **kw: None), [])
        self.assertEqual(scheduler.jobs, [])

    def test_the_jobs_register_when_the_flag_is_on(self):
        scheduler = _RecordingScheduler()
        registered = register(scheduler, SETTINGS, lambda **kw: None)
        self.assertEqual(len(registered), 3)
        self.assertEqual({job["id"] for job in scheduler.jobs}, set(registered))
        for job in scheduler.jobs:
            self.assertTrue(job["replace_existing"])


class TokenRefreshLogTests(unittest.TestCase):
    """Spec L §5.1: every refresh attempt is logged with timestamps.

    One data point so far says an *idle* token store did not survive June to
    September. Whether a continuously refreshing service survives is unknown,
    and the 30-day unattended probe cannot run without this record existing
    first.
    """

    def setUp(self):
        import tempfile

        from cryptography.fernet import Fernet

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(
            token_encryption_key=Fernet.generate_key().decode("ascii"),
            data_dir=self.tmp.name,
            database_url=f"sqlite:///{self.tmp.name}/x.db",
        )

    def _storage(self):
        from pathlib import Path

        from database.token_store import EncryptedFileTokenStorage

        return EncryptedFileTokenStorage(self.settings, Path(self.tmp.name) / "robinhood_token.enc")

    def test_a_bootstrap_and_a_refresh_are_recorded_and_distinguished(self):
        import asyncio

        from mcp.shared.auth import OAuthToken

        storage = self._storage()
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="a1", refresh_token="r1", token_type="Bearer", expires_in=600)
            )
        )
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="a2", refresh_token="r2", token_type="Bearer", expires_in=600)
            )
        )

        entries = storage.refresh_log()
        self.assertEqual([e["kind"] for e in entries], ["bootstrap", "refresh"])
        refresh = entries[1]
        self.assertIsNotNone(refresh["at"])
        self.assertIsNotNone(refresh["previous_expires_at"])
        self.assertIsNotNone(refresh["new_expires_at"])
        self.assertNotEqual(refresh["previous_expires_at"], refresh["new_expires_at"])
        self.assertTrue(refresh["has_refresh_token"])

        status = storage.status()
        self.assertEqual(status["refresh_count"], 1)
        self.assertEqual(status["last_refresh_at"], refresh["at"])

    def test_the_log_never_contains_a_token(self):
        import asyncio

        from mcp.shared.auth import OAuthToken

        storage = self._storage()
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="secret-access", refresh_token="secret-refresh", token_type="Bearer", expires_in=600)
            )
        )
        rendered = json.dumps(storage.refresh_log())
        self.assertNotIn("secret-access", rendered)
        self.assertNotIn("secret-refresh", rendered)


class _RecordingScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, *, id, name, kwargs=None, replace_existing=False):
        self.jobs.append(
            {
                "func": func,
                "trigger": trigger,
                "id": id,
                "name": name,
                "kwargs": kwargs,
                "replace_existing": replace_existing,
            }
        )


if __name__ == "__main__":
    unittest.main()
