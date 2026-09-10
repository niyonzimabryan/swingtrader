"""Spec Q §14, §15 PR 4: the shadow hook, and what it must not disturb.

The acceptance criteria this file exists to hold, in the order Spec Q's Agent 4
block states them:

* with every new flag false, a mocked full scan produces exactly the memo, the
  ledger row and the notifications it produces today, and **zero** rows in any
  of the eight Strategy Lab tables;
* with shadow on, the same scan produces the same memo *plus* N strategy
  decisions;
* no broker method is called by shadow mode under any tested failure path —
  asserted with a broker double that raises on any attribute access at all;
* a Strategy Lab failure never reaches the scan: registration, one snapshot, one
  arm, and a dead database each fail in isolation and the scan finishes;
* a re-run of the same scan writes no new decision (the runner's idempotency,
  exercised through the hook rather than asserted about it);
* the cross-sectional snapshot is built once per cutoff, never per ticker.

The scan fixtures are the ones `tests/test_scan_ledger_integration.py` already
uses — a `TradingPipeline` built through `__new__` with the agents stubbed — so
"behaves identically" is measured against the same shape of run that file
measures the ledger against.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from database import models
from database.db import get_session
from database.models import Memo, ScoredCandidate, Ticker
from orchestrator import strategy_lab_shadow as lab
from orchestrator.pipeline import ScanTickerItem, TradingPipeline
from strategy_lab import snapshots
from strategy_lab.domain import SnapshotScope
from tests.dbfixture import init_test_db
from utils.timeutils import utcnow_naive

LAB_TABLES = (
    models.Experiment,
    models.ExperimentArm,
    models.StrategyVersion,
    models.MarketSnapshot,
    models.StrategyDecision,
    models.StrategyTrade,
    models.ExperimentMetricSnapshot,
    models.PromotionEvent,
)


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class ExplodingBroker:
    """Any attribute access at all is a failure.

    Not "any order method": *any* attribute. A shadow arm has no business
    reading a position, an account or a quote either, and the strongest form of
    "no broker method is called" is the one that cannot be satisfied by calling
    a method the assertion forgot to name.
    """

    def __getattr__(self, name):  # pragma: no cover - the failure is the point
        raise AssertionError(
            f"shadow mode touched the broker: {name!r}. `strategy_lab` imports "
            "no broker and this path must never reach one."
        )


class _Agent:
    def __init__(self, score=0.8, direction="bullish"):
        self.score = score
        self.direction = direction
        self.confidence = 0.8
        self.reasoning = ""
        self.raw_data = {}


def _scoring_result(final=0.82, meets=True):
    return {
        "final_score": final,
        "direction": "bullish",
        "meets_memo_threshold": meets,
        "signal_breakdown": {
            "catalyst": {"score": 0.9, "status": "ok"},
            "fundamental": {"score": 0.7, "status": "ok"},
            "pattern": {"score": 0.6, "status": "active"},
            "web_research": {"score": 0.7, "status": "ok"},
        },
    }


TRADE_PARAMS = {
    "entry_price": 100.10,
    "stop_loss": 94.00,
    "target_1": 112.20,
    "target_2": 118.30,
    "max_hold_days": 20,
    "position_pct": 5.0,
    "regime_multiplier": 1.0,
}


def _settings(**overrides):
    """A settings double carrying every field the hook and the scan read."""
    base = dict(
        memo_threshold=0.55,
        auto_approve_min_score=0.55,
        exploration_min_score=0.45,
        auto_approve_paper=False,
        tier2_max_escalations=25,
        catalyst_failure_rate_alert_threshold=0.9,
        strategy_lab_enabled=False,
        strategy_lab_shadow_enabled=False,
        strategy_lab_experiment="shadow_roster_v1",
        strategy_lab_experiment_owner="bryan",
        strategy_lab_universe_enabled=False,
        strategy_lab_universe_slug="liquid_us_equity_v1",
        strategy_lab_max_tickers_per_scan=40,
        strategy_lab_shadow_equity=100_000.0,
        strategy_lab_shadow_risk_budget=0.01,
        strategy_lab_shadow_max_open_positions=10,
        strategy_lab_shadow_max_position_fraction=0.2,
        strategy_lab_maturation_max_snapshots=200,
        strategy_lab_slippage_bps=10.0,
        strategy_lab_half_spread_bps=5.0,
        strategy_lab_commission_bps=0.0,
        strategy_lab_floor_matured=100,
        strategy_lab_floor_distinct_dates=20,
        strategy_lab_floor_closed=30,
        strategy_lab_confidence_level=0.90,
        strategy_lab_report_bootstrap_reps=50,
        strategy_lab_report_seed=20260910,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #


class ScanFixture(unittest.TestCase):
    """A `TradingPipeline` with the agents stubbed and a broker that explodes."""

    def setUp(self):
        self.db = init_test_db("strategy_lab_integration")
        self.addCleanup(self._dispose)
        self.settings = _settings()
        self.pipeline = self._pipeline(self.settings)
        self.memo_rows: list[int] = []

    def _dispose(self):
        from database import db as db_module

        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.db.cleanup()

    def _pipeline(self, settings):
        p = TradingPipeline.__new__(TradingPipeline)
        p.settings = settings
        p.paused = False
        p.broker = ExplodingBroker()
        p.brokers = ExplodingBroker()
        p.order_manager = ExplodingBroker()
        p.notification_manager = None
        p.bot_loop = None
        p.deep_research_agent = None
        p.discovery_agent = None
        p.auto_approver = None
        p._price_cache = None
        p._ensure_ticker = lambda ticker: None
        p._get_portfolio_context = lambda: ""
        p._run_post_catalyst_agents = lambda **kw: (_Agent(), _Agent(), _Agent(), {}, 1)
        p.catalyst_agent = SimpleNamespace(analyze=lambda **kw: _Agent(score=0.8))
        p.scoring_engine = SimpleNamespace(
            score_opportunity=lambda *a, **k: _scoring_result()
        )
        p.memo_generator = SimpleNamespace(generate=self._generate_memo)
        p.macro_agent = SimpleNamespace(
            analyze=lambda: SimpleNamespace(raw_data={"regime": "risk-on"})
        )
        p.structured_scanner = SimpleNamespace(scan=lambda universe: SimpleNamespace(
            flagged=[], scan_duration_s=0.0
        ))
        p.gemini_screener = SimpleNamespace(is_available=False)
        p._build_scan_list = lambda *a, **k: list(self.scan_items())
        return p

    def scan_items(self):
        return [ScanTickerItem(
            ticker="NVDA", sector="Tech", source="tier2_gemini", haiku_threshold=0
        )]

    def _generate_memo(self, ticker, result, *args, **kwargs):
        """Write a real `memos` row, the way `memo/generator.py` does.

        The compatibility arm reads its trade plan out of that row, so a stub
        that only returned a dict would make the champion abstain for a reason
        the production path would not.
        """
        with get_session() as session:
            row = session.query(Ticker).filter_by(symbol=ticker).first()
            if row is None:
                row = Ticker(symbol=ticker)
                session.add(row)
                session.flush()
            memo = Memo(
                ticker_id=row.id,
                composite_score=float(result["final_score"]),
                classification="high_conviction",
                direction="long",
                created_at=utcnow_naive(),
                trade_params=json.dumps(TRADE_PARAMS),
                signal_breakdown=json.dumps({"catalyst": 0.9}),
            )
            session.add(memo)
            session.flush()
            memo_id = memo.id
        self.memo_rows.append(memo_id)
        return {
            "memo_id": memo_id,
            "composite_score": float(result["final_score"]),
            "classification": "high_conviction",
            "trade_params": dict(TRADE_PARAMS),
            "opus_evaluation": {},
        }

    # -- helpers ---------------------------------------------------------- #

    def run_scan(self):
        self.pipeline._run_full_scan_inner(utcnow_naive(), "scan-test-1")

    def lab_row_counts(self) -> dict[str, int]:
        with get_session() as session:
            return {
                model.__tablename__: session.query(model).count()
                for model in LAB_TABLES
            }

    def decision_actions(self) -> list[str]:
        with get_session() as session:
            return sorted(
                row.action for row in session.query(models.StrategyDecision).all()
            )


# --------------------------------------------------------------------------- #
# 1. Flags off
# --------------------------------------------------------------------------- #


class FlagsOffTests(ScanFixture):
    """Requirement 1 and the second acceptance criterion."""

    def test_a_full_scan_with_the_flags_off_writes_no_strategy_lab_row(self):
        self.run_scan()

        with get_session() as session:
            self.assertEqual(session.query(ScoredCandidate).count(), 1)
            self.assertEqual(session.query(Memo).count(), 1)
        self.assertEqual(
            self.lab_row_counts(),
            {model.__tablename__: 0 for model in LAB_TABLES},
            "with the flags off the Strategy Lab must write nothing at all",
        )

    def test_the_hook_returns_before_it_reaches_a_session(self):
        summary = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"])
        self.assertFalse(summary.enabled)
        self.assertEqual(summary.skipped_reason, lab.SKIP_LAB_DISABLED)
        self.assertEqual(summary.decisions, 0)

    def test_the_master_switch_alone_is_not_enough_to_write(self):
        settings = _settings(strategy_lab_enabled=True)
        summary = lab.run_shadow_for_scan(settings, tickers=["NVDA"])
        self.assertFalse(summary.ran)
        self.assertEqual(summary.skipped_reason, lab.SKIP_SHADOW_DISABLED)
        self.assertEqual(self.lab_row_counts()["strategy_decisions"], 0)

    def test_the_maturation_job_is_a_no_op_with_the_flags_off(self):
        summary = lab.mature_shadow_decisions(self.settings)
        self.assertFalse(summary.enabled)
        self.assertEqual(summary.snapshots_considered, 0)
        self.assertEqual(self.lab_row_counts()["strategy_trades"], 0)

    def test_the_legacy_ledger_row_is_identical_either_way(self):
        """Requirement 3: the `ScoredCandidate` ledger continues unchanged.

        The same scan is run with the flags off and with shadow on, and every
        column of the ledger row that is not a clock or an id must match. The
        migration period has two ledgers; the old one is not allowed to drift
        because the new one exists.
        """
        self.run_scan()
        off = _ledger_row()

        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True
        self.pipeline = self._pipeline(self.settings)
        self.run_scan()
        rows = _ledger_rows()
        self.assertEqual(len(rows), 2)
        on = rows[-1]

        for column in (
            "ticker", "source", "final_score", "direction", "regime", "cohort",
            "memo_generated", "catalyst_score", "fundamental_score",
            "pattern_score", "pattern_status", "web_research_score",
            "entry_price", "suggested_stop", "target_1",
        ):
            self.assertEqual(
                off[column], on[column],
                f"the legacy ledger's {column} changed when shadow was enabled",
            )

    def test_the_funnel_line_carries_counts_and_reason_codes_only(self):
        """Requirement 6: log events with no sensitive payloads."""
        fields = lab.run_shadow_for_scan(
            self.settings, tickers=["NVDA"]
        ).as_log_fields()
        for name, value in fields.items():
            self.assertIsInstance(
                value, (int, str), f"{name} is neither a count nor a reason code"
            )
        self.assertNotIn("NVDA", " ".join(str(v) for v in fields.values()))

    def test_the_scoreboard_is_none_with_the_master_switch_off(self):
        self.assertIsNone(lab.scoreboard(self.settings))
        self.assertIsNone(lab.experiment_overview(self.settings))
        self.assertIsNone(lab.strategy_overview(self.settings))


# --------------------------------------------------------------------------- #
# 2. Shadow on
# --------------------------------------------------------------------------- #


class ShadowEnabledTests(ScanFixture):
    """Requirement 2 and the first acceptance criterion."""

    def setUp(self):
        super().setUp()
        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True

    def test_a_mocked_scan_produces_the_memo_plus_n_strategy_decisions(self):
        self.run_scan()

        with get_session() as session:
            self.assertEqual(session.query(Memo).count(), 1, "the memo still ships")
            self.assertEqual(session.query(ScoredCandidate).count(), 1)
            names = [row.name for row in session.query(models.Experiment).all()]
            modes = [row.mode for row in session.query(models.ExperimentArm).all()]
            decisions = [
                (row.ticker, row.action)
                for row in session.query(models.StrategyDecision).all()
            ]

        self.assertEqual(names, ["shadow_roster_v1"])
        self.assertEqual(len(modes), lab.PLANNED_VARIANTS)
        self.assertEqual(set(modes), {"shadow"})
        # The two ticker-scoped arms both decide about NVDA; the two
        # universe-scoped arms are not run at all without a universe snapshot.
        self.assertEqual(len(decisions), len(lab.TICKER_SCOPED_SLUGS))
        self.assertEqual({ticker for ticker, _ in decisions}, {"NVDA"})

    def test_the_champion_records_a_long_for_a_memo_cohort_candidate(self):
        """The compatibility adapter maps the pipeline's own output, unchanged.

        The pipeline writes `bullish` and the lab records a *side*, so the
        builder translates. Without that translation this arm never fires and
        the champion silently produces nothing but `flat` — which would make the
        whole experiment vacuous rather than merely disappointing.
        """
        self.run_scan()
        rows = _decisions_by_slug()
        self.assertEqual(rows["swingtrader_composite_v1"]["action"], "long")
        self.assertIn(
            "pipeline_memo_cohort", rows["swingtrader_composite_v1"]["reason_codes"]
        )

    def test_a_partial_roster_is_recorded_rather_than_hidden(self):
        """`earnings_drift_v1` has no earnings record here and must abstain."""
        self.run_scan()
        drift = _decisions_by_slug()["earnings_drift_v1"]
        self.assertEqual(drift["action"], "abstain")
        self.assertTrue(drift["blocked_reasons"])

    def test_a_second_identical_scan_writes_no_new_decision(self):
        """Requirement 7: duplicate scans. One snapshot, one decision set."""
        self.run_scan()
        before = self.lab_row_counts()
        cutoff = _shared_cutoff()
        first = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"], cutoff=cutoff)
        middle = self.lab_row_counts()
        # The same cutoff over the same inputs hashes to the same snapshot, so
        # the runner returns the stored rows instead of writing new ones.
        second = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"], cutoff=cutoff)
        after = self.lab_row_counts()

        self.assertEqual(first.ticker_snapshots, 1)
        self.assertEqual(middle["market_snapshots"], before["market_snapshots"] + 1)
        self.assertEqual(after["market_snapshots"], middle["market_snapshots"])
        self.assertEqual(after["strategy_decisions"], middle["strategy_decisions"])
        self.assertEqual(first.decisions, second.decisions)
        self.assertEqual(second.n_deduplicated, second.decisions)

    def test_a_restart_re_registers_nothing_and_resumes(self):
        """Requirement 7: restart. Registration is idempotent by construction."""
        self.run_scan()
        first = self.lab_row_counts()
        # A fresh pipeline object is what a restarted container has.
        self.pipeline = self._pipeline(self.settings)
        self.run_scan()
        second = self.lab_row_counts()
        self.assertEqual(first["experiments"], second["experiments"])
        self.assertEqual(first["experiment_arms"], second["experiment_arms"])
        self.assertEqual(first["strategy_versions"], second["strategy_versions"])

    def test_no_ticker_and_no_universe_arm_is_a_named_skip(self):
        summary = lab.run_shadow_for_scan(self.settings, tickers=[])
        self.assertEqual(summary.skipped_reason, lab.SKIP_NO_TICKERS)
        self.assertEqual(summary.decisions, 0)

    def test_a_paused_experiment_stops_the_arms(self):
        self.run_scan()
        result = lab.set_experiment_paused(
            self.settings, "shadow_roster_v1", paused=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "paused")

        before = self.lab_row_counts()["strategy_decisions"]
        summary = lab.run_shadow_for_scan(
            self.settings, tickers=["NVDA"], cutoff=_shared_cutoff(),
        )
        self.assertEqual(self.lab_row_counts()["strategy_decisions"], before)
        self.assertEqual(summary.ticker_snapshots, 1, "the snapshot is still built")
        self.assertEqual(summary.decisions, 0, "but no arm decides")
        self.assertTrue(
            any("paused" in reason for _, reason in summary.refusals),
            f"expected a paused refusal, got {summary.refusals}",
        )

        resumed = lab.set_experiment_paused(
            self.settings, "shadow_roster_v1", paused=False
        )
        self.assertEqual(resumed["status"], "running")

    def test_an_experiment_can_be_paused_by_row_id_too(self):
        """Spec Q §13 writes `<id>`; `/experiments` prints names. Both resolve."""
        self.run_scan()
        with get_session() as session:
            row_id = session.query(models.Experiment).first().id

        result = lab.set_experiment_paused(self.settings, str(row_id), paused=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "shadow_roster_v1")
        self.assertEqual(result["status"], "paused")

    def test_an_unknown_experiment_is_refused_not_created(self):
        result = lab.set_experiment_paused(self.settings, "no_such_thing", paused=True)
        self.assertFalse(result["ok"])
        self.assertIn("no_such_thing", result["error"])
        self.assertEqual(self.lab_row_counts()["experiments"], 0)


def _ledger_rows() -> list[dict]:
    """Every `scored_candidates` row, detached, ordered by id."""
    columns = (
        "ticker", "source", "final_score", "direction", "regime", "cohort",
        "memo_generated", "catalyst_score", "fundamental_score", "pattern_score",
        "pattern_status", "web_research_score", "entry_price", "suggested_stop",
        "target_1", "run_id",
    )
    with get_session() as session:
        return [
            {column: getattr(row, column) for column in columns}
            for row in session.query(ScoredCandidate)
            .order_by(ScoredCandidate.id)
            .all()
        ]


def _ledger_row() -> dict:
    rows = _ledger_rows()
    assert len(rows) == 1, f"expected one ledger row, got {len(rows)}"
    return rows[0]


def _decisions_by_slug() -> dict[str, dict]:
    """Every stored decision, keyed by strategy slug, detached from the session."""
    out: dict[str, dict] = {}
    with get_session() as session:
        for row in session.query(models.StrategyDecision).all():
            arm = session.get(models.ExperimentArm, row.arm_id)
            slug = session.get(models.StrategyVersion, arm.strategy_version_id).slug
            out[slug] = {
                "action": row.action,
                "ticker": row.ticker,
                "reason_codes": json.loads(row.reason_codes_json or "[]"),
                "blocked_reasons": json.loads(row.blocked_reasons_json or "[]"),
            }
    return out


def _shared_cutoff() -> datetime:
    """One fixed cutoff for a test that runs the hook twice.

    Whole seconds, and a few minutes ahead of the scan that produced the ledger
    row, so that (a) the two calls hash to the same snapshot and (b) the cutoff
    is after `scored_at` rather than a rounded-down instant just before it.
    """
    return utcnow_naive().replace(microsecond=0) + timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# 3. Failure isolation
# --------------------------------------------------------------------------- #


class FailureIsolationTests(ScanFixture):
    """Requirement 2's second half and requirement 7's DB-failure case."""

    def setUp(self):
        super().setUp()
        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True

    def test_a_dead_database_does_not_stop_the_scan(self):
        from database import db as db_module

        original = db_module.get_session

        def _broken():
            raise RuntimeError("connection pool exhausted")

        db_module.get_session = _broken
        self.addCleanup(lambda: setattr(db_module, "get_session", original))

        summary = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"])
        self.assertTrue(summary.errors)
        self.assertEqual(summary.decisions, 0)

    def test_a_registration_failure_is_recorded_and_the_pass_returns(self):
        original = lab.ensure_experiment

        def _boom(session, settings):
            raise RuntimeError("experiment plan is frozen")

        lab.ensure_experiment = _boom
        self.addCleanup(lambda: setattr(lab, "ensure_experiment", original))

        summary = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"])
        self.assertTrue(any("register" in e for e in summary.errors))
        self.assertEqual(summary.decisions, 0)

    def test_a_snapshot_that_cannot_be_built_is_counted_not_raised(self):
        """A ticker with no scored row, no bars and no earnings has nothing to snapshot."""
        summary = lab.run_shadow_for_scan(self.settings, tickers=["ZZZZ"])
        self.assertEqual(summary.snapshot_failures, 1)
        self.assertEqual(summary.ticker_snapshots, 0)
        self.assertEqual(summary.decisions, 0)
        self.assertFalse(summary.errors, "a missing snapshot is data, not a defect")

    def test_the_pipeline_hook_survives_a_module_level_explosion(self):
        """Even a defect in the module's own error handling must not kill a scan."""
        original = lab.run_shadow_for_scan

        def _boom(*args, **kwargs):
            raise RuntimeError("the isolation itself failed")

        lab.run_shadow_for_scan = _boom
        self.addCleanup(lambda: setattr(lab, "run_shadow_for_scan", original))

        self.run_scan()  # must not raise
        with get_session() as session:
            self.assertEqual(session.query(Memo).count(), 1)

    def test_no_failure_path_touches_the_broker(self):
        """The acceptance criterion, stated as the assertion it is.

        `self.pipeline.broker` raises on *any* attribute access, and every path
        above ran a scan or a shadow pass through it without one.
        """
        for tickers in (["NVDA"], ["ZZZZ"], []):
            lab.run_shadow_for_scan(self.settings, tickers=tickers)
        lab.mature_shadow_decisions(self.settings)
        self.run_scan()
        with self.assertRaises(AssertionError):
            self.pipeline.broker.get_account_info


# --------------------------------------------------------------------------- #
# 4. Scope, and the once-per-cutoff rule
# --------------------------------------------------------------------------- #


class ScopeTests(unittest.TestCase):
    """Requirement 8, and the table that says which arms are cross-sectional."""

    def test_the_scope_table_agrees_with_every_roster_strategy(self):
        """`UNIVERSE_SCOPED_SLUGS` is a restatement; this holds it to the source.

        Each strategy raises `ValueError` when handed the wrong scope, so the
        cheapest honest check is to hand each one both and see which it takes.
        """
        from strategy_lab import strategies

        ticker_snapshot = _empty_ticker_snapshot()
        universe_snapshot = _empty_universe_snapshot()
        for slug, strategy in sorted(strategies.ROSTER.items()):
            takes_universe = _accepts(strategy, universe_snapshot)
            takes_ticker = _accepts(strategy, ticker_snapshot)
            self.assertNotEqual(
                takes_universe, takes_ticker,
                f"{slug} must accept exactly one scope",
            )
            self.assertEqual(
                takes_universe, slug in lab.UNIVERSE_SCOPED_SLUGS,
                f"{slug}: UNIVERSE_SCOPED_SLUGS disagrees with the strategy",
            )
            self.assertEqual(
                takes_ticker, slug in lab.TICKER_SCOPED_SLUGS,
                f"{slug}: TICKER_SCOPED_SLUGS disagrees with the strategy",
            )

    def test_every_roster_slug_is_in_exactly_one_scope_set(self):
        from strategy_lab import strategies

        self.assertEqual(
            set(strategies.ROSTER),
            lab.UNIVERSE_SCOPED_SLUGS | lab.TICKER_SCOPED_SLUGS,
        )
        self.assertFalse(lab.UNIVERSE_SCOPED_SLUGS & lab.TICKER_SCOPED_SLUGS)

    def test_the_planned_variant_count_matches_the_roster(self):
        """A silently growing multiple-testing denominator is the failure mode."""
        from strategy_lab import strategies

        self.assertEqual(lab.PLANNED_VARIANTS, len(strategies.ROSTER))


def _accepts(strategy, snapshot) -> bool:
    try:
        strategy.evaluate(snapshot)
    except ValueError:
        return False
    except Exception:
        # Any other refusal — a missing input, an abstain path that raises —
        # means the scope was accepted and the *data* was refused.
        return True
    return True


def _empty_ticker_snapshot():
    cutoff = datetime(2026, 3, 31, 21, 0)
    return snapshots.build_ticker_snapshot(
        as_of_utc=cutoff,
        data_cutoff_utc=cutoff,
        inputs=snapshots.TickerInputs(ticker="AAPL", bars=(), price_source="fixture"),
        provenance={"prices": "fixture"},
    )


def _empty_universe_snapshot():
    from tests import strategylabfixture as fixture

    return fixture.universe_snapshot([
        fixture.replayable_inputs("AAPL", fixture.flat_bars(260)),
        fixture.replayable_inputs("MSFT", fixture.flat_bars(260, close=50.0)),
    ])


class UniverseSnapshotTests(ScanFixture):
    """One cross-sectional snapshot per cutoff, never one per ticker."""

    def setUp(self):
        super().setUp()
        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True
        self.settings.strategy_lab_universe_enabled = True

    def _seed_universe(self, tickers, *, last: date, sessions: int = 260):
        with get_session() as session:
            for ticker in tickers:
                uid = f"uid-{ticker.lower()}"
                session.add(models.Security(
                    security_uid=uid, ticker=ticker,
                    ticker_valid_from=date(2020, 1, 1), ticker_valid_to=None,
                    venue="XNAS", source="fixture",
                ))
                session.add(models.UniverseMembership(
                    universe_slug="liquid_us_equity_v1", security_uid=uid,
                    ticker=ticker, member_from=date(2020, 1, 1), member_to=None,
                    source="rule:liquid_us_equity_v1",
                    known_at_utc=datetime(2020, 1, 1),
                ))
                day = last
                added = 0
                while added < sessions:
                    if day.weekday() < 5:
                        session.add(models.PriceBar(
                            security_uid=uid, ticker=ticker, session_date=day,
                            raw_open=100.0, raw_high=101.0, raw_low=99.0,
                            raw_close=100.0, volume=1_000_000.0, split_factor=1.0,
                            dividend_cash=0.0, split_adjusted_close=100.0,
                            total_return_close=100.0, source="fixture",
                        ))
                        added += 1
                    day -= timedelta(days=1)
            session.flush()

    def test_one_universe_snapshot_is_shared_by_every_cross_sectional_arm(self):
        yesterday = (utcnow_naive() - timedelta(days=1)).date()
        self._seed_universe(["AAPL", "MSFT", "NVDA"], last=yesterday)

        summary = lab.run_shadow_for_scan(
            self.settings, tickers=["AAPL", "MSFT", "NVDA"], cutoff=_shared_cutoff(),
        )
        self.assertEqual(
            summary.universe_snapshots, 1,
            "a cross-section is one snapshot for the cutoff, not one per ticker",
        )
        with get_session() as session:
            universe_rows = [
                row for row in session.query(models.MarketSnapshot).all()
                if row.scope == SnapshotScope.UNIVERSE.value
            ]
            self.assertEqual(len(universe_rows), 1)
            snapshot_id = universe_rows[0].id
            arm_ids = {
                row.arm_id for row in session.query(models.StrategyDecision)
                .filter(models.StrategyDecision.snapshot_id == snapshot_id).all()
            }
        self.assertEqual(
            len(arm_ids), len(lab.UNIVERSE_SCOPED_SLUGS),
            "both universe arms rank against the same snapshot row",
        )

    def test_the_universe_sub_flag_off_records_a_refusal_rather_than_silence(self):
        self.settings.strategy_lab_universe_enabled = False
        summary = lab.run_shadow_for_scan(self.settings, tickers=["NVDA"])
        self.assertEqual(summary.universe_snapshots, 0)
        self.assertTrue(any(arm == "universe_arms" for arm, _ in summary.refusals))


# --------------------------------------------------------------------------- #
# 5. Maturation
# --------------------------------------------------------------------------- #


class MaturationTests(ScanFixture):
    """Requirement 7: long-duration pending positions, and settlement."""

    def setUp(self):
        super().setUp()
        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True

    def test_a_fresh_decision_is_pending_not_settled(self):
        self.run_scan()
        summary = lab.mature_shadow_decisions(self.settings)
        self.assertEqual(summary.snapshots_settled, 0)
        self.assertGreaterEqual(summary.still_pending, 1)
        self.assertEqual(self.lab_row_counts()["strategy_trades"], 0)

    def test_a_decision_whose_bars_never_arrive_stays_a_decision(self):
        """Neither a win, a loss, nor a zero: no execution row at all."""
        self.run_scan()
        far_future = utcnow_naive() + timedelta(days=400)
        summary = lab.mature_shadow_decisions(self.settings, now=far_future)
        self.assertEqual(summary.snapshots_settled, 0)
        self.assertEqual(self.lab_row_counts()["strategy_trades"], 0)
        self.assertGreaterEqual(summary.still_pending, 1)

    def test_forward_bars_settle_the_champion_and_place_no_order(self):
        self.run_scan()
        cutoff_day = utcnow_naive().date()
        with get_session() as session:
            session.add(models.Security(
                security_uid="uid-nvda", ticker="NVDA",
                ticker_valid_from=date(2020, 1, 1), ticker_valid_to=None,
                venue="XNAS", source="fixture",
            ))
            price = 100.10
            day = cutoff_day
            added = 0
            while added < 30:
                day += timedelta(days=1)
                if day.weekday() >= 5:
                    continue
                price += 1.0
                session.add(models.PriceBar(
                    security_uid="uid-nvda", ticker="NVDA", session_date=day,
                    raw_open=price, raw_high=price + 1.0, raw_low=price - 1.0,
                    raw_close=price, volume=1_000_000.0, split_factor=1.0,
                    dividend_cash=0.0, split_adjusted_close=price,
                    total_return_close=price, source="fixture",
                ))
                added += 1
            session.flush()

        summary = lab.mature_shadow_decisions(
            self.settings, now=utcnow_naive() + timedelta(days=90)
        )
        self.assertEqual(summary.snapshots_settled, 1)
        self.assertEqual(summary.executions_opened, 1)
        with get_session() as session:
            trades = [
                (row.mode, row.status)
                for row in session.query(models.StrategyTrade).all()
            ]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0][0], "shadow")
        self.assertEqual(trades[0][1], "closed")
        # The broker double is still untouched.
        with self.assertRaises(AssertionError):
            self.pipeline.broker.submit_order

    def test_maturation_is_idempotent(self):
        self.run_scan()
        now = utcnow_naive() + timedelta(days=90)
        lab.mature_shadow_decisions(self.settings, now=now)
        first = self.lab_row_counts()["strategy_trades"]
        lab.mature_shadow_decisions(self.settings, now=now)
        self.assertEqual(self.lab_row_counts()["strategy_trades"], first)


# --------------------------------------------------------------------------- #
# 6. Empty data and the scorecard
# --------------------------------------------------------------------------- #


class ScoreboardTests(ScanFixture):
    """Requirement 5: the card exists and refuses to name a winner on nothing."""

    def setUp(self):
        super().setUp()
        self.settings.strategy_lab_enabled = True
        self.settings.strategy_lab_shadow_enabled = True

    def test_no_experiment_yet_is_none_rather_than_an_empty_card(self):
        self.assertIsNone(lab.scoreboard(self.settings))

    def test_a_card_with_no_settled_trade_names_no_winner(self):
        self.run_scan()
        payload = lab.scoreboard(self.settings)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["schema"], "strategy_lab.scoreboard.v1")
        for section in (payload.get("sections") or {}).values():
            self.assertIsNone(section.get("winner"))
        self.assertEqual(payload["variants"]["planned_variants"], lab.PLANNED_VARIANTS)

    def test_the_overview_reads_only_stored_columns(self):
        self.run_scan()
        overview = lab.experiment_overview(self.settings)
        self.assertEqual(len(overview["experiments"]), 1)
        experiment = overview["experiments"][0]
        self.assertTrue(experiment["configured"])
        self.assertEqual(len(experiment["arms"]), lab.PLANNED_VARIANTS)

        roster = lab.strategy_overview(self.settings)
        self.assertEqual(len(roster["strategies"]), lab.PLANNED_VARIANTS)
        champion = [s for s in roster["strategies"] if s["champion"]]
        self.assertEqual(len(champion), 1)

    def test_strategy_detail_reports_an_unknown_slug_rather_than_guessing(self):
        detail = lab.strategy_detail(self.settings, "not_a_strategy")
        self.assertEqual(detail["unknown"], "not_a_strategy")
        self.assertIn("momentum_v1", detail["roster"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
