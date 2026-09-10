"""The PR 3 acceptance fixture, and the CLI's determinism (Spec Q §10, §16, §19).

One fixture registers the champion and its three challengers, replays the
eligible arms historically, shadows all four, matures the trades, and produces a
labelled scoreboard. The assertions are the acceptance criteria:

* the compatibility arm is **refused** a historical replay, because its snapshot
  freezes an LLM's conclusion and Spec Q §7A prohibits reconstructing that at a
  historical T — and it is still shadowed forward, into the exploratory section;
* clean and ``archival_reconstructed`` evidence land in two sections that are
  never combined;
* no arm is called a winner — every one of them is far below its floors, and
  ``insufficient_evidence`` is the honest label for a four-trade tournament;
* running the CLI twice over the same database produces byte-identical JSON and
  Markdown.

The fixture is deliberately small. It is an *acceptance* test of the pipeline's
shape, not a claim about any strategy: with four matured trades the only correct
verdict is the one the card prints.
"""

from __future__ import annotations

import functools
import json
import tempfile
import unittest
from pathlib import Path

from scripts import strategy_lab_scoreboard as scoreboard
from strategy_lab import metrics, registry, replay, runner, shadow, strategies
from strategy_lab.domain import ExecutionMode, ExperimentSpec
from tests import strategylabfixture as fx
from tests.dbfixture import TestDatabase

EXPERIMENT = "q1_2026_roster"
COSTS = replay.CostAssumptions(slippage_bps=10.0, half_spread_bps=5.0)
CUTOFF = fx.Q1_2026_CLOSE


@functools.lru_cache(maxsize=1)
def dip_bars():
    """A year of gains, then a 9.5% three-session slide: the reversal setup.

    Close 117.65 against an SMA(200) of 111.03 and RSI(2) below 1, which is
    exactly the Spec Q §7 rule — computed from the series, not asserted here.
    """
    closes = [80.0 + i * (50.0 / 256) for i in range(257)]
    last = closes[-1]
    closes += [last * 0.97, last * 0.94, last * 0.905]
    return fx.bars_from_closes(closes)


def an_experiment() -> ExperimentSpec:
    return ExperimentSpec(
        name=EXPERIMENT,
        hypothesis=(
            "at least one challenger beats the composite champion after costs "
            "over the same opportunity set"
        ),
        universe_spec="liquid_us_equity_v1",
        primary_metric="mean_net_pct",
        benchmarks=("cash_no_trade", "spy_total_return"),
        guardrail_metrics=("max_drawdown_pct", "turnover_trades_per_year"),
        end_criteria={"min_matured_decisions": 100, "min_calendar_days": 60},
        start_criteria={"arms_registered": 4},
        preregistration={"analysis_plan": "Spec Q §10 primary metrics"},
        owner="bryan",
        planned_variants=4,
        data_cutoff_utc=CUTOFF,
    )


class AcceptanceTestCase(unittest.TestCase):
    """Registers the roster once per class and drives it end to end.

    The fixture is built in ``setUpClass``, not ``setUp``. Driving the whole
    pipeline — four arms, three snapshots, a 55-name universe, eight runs and
    the shadow fills — costs about two seconds on SQLite and considerably more
    on Postgres, and every assertion below except one reads the *result* of that
    run rather than changing it. Rebuilding it per test method would multiply
    the cost by the number of tests for no extra coverage, and this repository's
    CI already runs the suite twice, once per engine.

    The one test that writes, ``test_a_second_identical_pass_writes_no_new_rows``,
    re-runs the pipeline deliberately — and its whole claim is that doing so
    writes nothing, so it cannot disturb a later test in the class.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("strategy_lab_scoreboard")
        cls.addClassCleanup(cls.db.cleanup)
        from database.db import get_session, init_db

        init_db(cls.db.url)
        cls._ctx = get_session()
        cls.session = cls._ctx.__enter__()
        cls.addClassCleanup(lambda: cls._ctx.__exit__(None, None, None))
        cls.versions = strategies.build_versions()
        cls.registered = runner.register_experiment(
            cls.session,
            an_experiment(),
            [
                runner.ArmPlan(slug, ExecutionMode.SHADOW, risk_budget=0.01)
                for slug in (
                    "swingtrader_composite_v1", "earnings_drift_v1",
                    "momentum_v1", "short_term_reversal_v1",
                )
            ],
        )
        runner.start_experiment(cls.session, EXPERIMENT)
        cls.reports = cls._run_every_arm()

    # -- the fixture ------------------------------------------------------- #

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _snapshots():
        """Three snapshots: one per scope, and one deliberately exploratory.

        Cached: they are immutable value objects and every test in this file
        wants the same three, so rebuilding 55 x 260 bars per test would be
        pure cost.
        """
        universe = fx.universe_snapshot(
            [fx.replayable_inputs("DIP001", dip_bars())]
            + [
                fx.replayable_inputs(f"T{i:03d}", fx.ramp_bars(100.0, 100.0 + i, 260))
                for i in range(54)
            ]
        )
        earnings = fx.ticker_snapshot(fx.replayable_inputs(
            "MSFT",
            fx.flat_bars(260, 100.0),
            earnings=fx.earnings_record("MSFT", reported_eps=1.2, consensus_eps=1.0),
        ))
        # A composite result makes the whole snapshot `not_point_in_time`: a
        # model's conclusion is not reconstructible at a historical T.
        composite = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0), composite=fx.composite_result("AAPL"),
        ))
        return universe, earnings, composite

    @classmethod
    def _run_every_arm(cls):
        universe, earnings, composite = cls._snapshots()
        cls.universe, cls.earnings, cls.composite = universe, earnings, composite
        arms = {
            "momentum_v1": (universe, cls.versions["momentum_v1"]),
            "short_term_reversal_v1": (universe, cls.versions["short_term_reversal_v1"]),
            "earnings_drift_v1": (earnings, cls.versions["earnings_drift_v1"]),
            "swingtrader_composite_v1": (
                composite, cls.versions["swingtrader_composite_v1"]
            ),
        }
        reports = {}
        for slug, (snapshot, version) in arms.items():
            arm = cls.registered.arm(slug)
            # Every replayable arm is asked for a historical replay first; the
            # ones that cannot support one say so and are shadowed forward
            # instead, which is the split Spec Q §10 requires.
            historical = runner.run_snapshot(
                cls.session, snapshot, [arm.arm_id], historical=True
            )
            forward = runner.run_snapshot(cls.session, snapshot, [arm.arm_id])
            reports[slug] = (historical, forward)

            run = forward.arm_runs[0]
            longs = [
                decision_id
                for decision_id, decision in zip(run.decision_ids, run.decisions)
                if decision.action.value == "long"
            ]
            if not longs:
                continue
            bars = {
                decision.ticker: fx.rising_forward(95, start=100.0, step=0.4)
                for decision in run.decisions
            }
            shadow.execute_arm(
                cls.session, arm.arm_id, version, snapshot, longs, bars,
                context=shadow.PortfolioContext(
                    as_of_utc=CUTOFF, equity=100_000.0,
                    max_open_positions=20, max_daily_notional=500_000.0,
                ),
                costs=COSTS,
            )
        cls.session.commit()
        return reports

    # -- helpers ----------------------------------------------------------- #

    def card(self, **kwargs):
        by_arm, read_warnings = scoreboard.collect(self.session, EXPERIMENT)
        ledger = runner.variant_ledger(self.session, EXPERIMENT)
        inputs = scoreboard.ScoreboardInputs(
            experiment=EXPERIMENT,
            cutoff_utc=CUTOFF,
            floors=metrics.EvidenceFloors(),
            gate=metrics.RankingGate(),
            costs=COSTS,
        )
        refusals = [
            (label, reason)
            for historical, _ in self.reports.values()
            for label, reason in historical.refusals
        ]
        return scoreboard.build_scorecard(
            inputs, by_arm, ledger,
            uncertainty=scoreboard.ComparablesUncertainty(reps=50),
            multiplicity=scoreboard.ComparablesMultiplicity(reps=50),
            refusals=refusals,
            read_warnings=read_warnings,
            **kwargs,
        )


class RosterAcceptanceTests(AcceptanceTestCase):
    def test_all_four_arms_are_registered_and_active(self):
        self.assertEqual(len(self.registered.arms), 4)
        ledger = runner.variant_ledger(self.session, EXPERIMENT)
        self.assertEqual(ledger.planned_variants, 4)
        self.assertEqual(ledger.n_tried, 4)
        self.assertEqual(ledger.undeclared, 0)

    def test_every_arm_recorded_decisions_for_its_own_scope(self):
        for slug, (_, forward) in self.reports.items():
            with self.subTest(slug):
                run = forward.arm_runs[0]
                self.assertTrue(run.ran, run.refused)
                self.assertGreater(len(run.decision_ids), 0)

    def test_the_universe_arms_persist_one_decision_per_constituent(self):
        for slug in ("momentum_v1", "short_term_reversal_v1"):
            with self.subTest(slug):
                run = self.reports[slug][1].arm_runs[0]
                self.assertEqual(
                    len(run.decision_ids), len(self.universe.constituents)
                )
                self.assertEqual(
                    run.n_long + run.n_flat + run.n_abstain,
                    len(self.universe.constituents),
                )

    def test_the_compatibility_arm_is_refused_a_historical_replay(self):
        """Spec Q §7A: an LLM's conclusion is not reconstructible at time T."""
        historical, forward = self.reports["swingtrader_composite_v1"]
        self.assertFalse(historical.arm_runs[0].ran)
        self.assertIn(
            "historically_replayable=False", historical.arm_runs[0].refused
        )
        self.assertTrue(forward.arm_runs[0].ran)

    def test_the_replayable_arms_pass_the_historical_guard(self):
        for slug in ("earnings_drift_v1", "momentum_v1", "short_term_reversal_v1"):
            with self.subTest(slug):
                historical, _ = self.reports[slug]
                self.assertTrue(historical.arm_runs[0].ran, historical.refusals)

    def test_trades_matured_and_were_closed_with_their_costs(self):
        closed = 0
        for arm in self.registered.arms:
            for row in registry.executions_for_arm(self.session, arm.arm_id):
                if row.status == "closed":
                    closed += 1
                    self.assertIsNotNone(row.realized_pnl)
                    self.assertIsNotNone(row.costs)
                    self.assertGreater(row.notional, 0)
        self.assertGreater(closed, 0)

    def test_a_second_identical_pass_writes_no_new_rows(self):
        before = {
            arm.arm_id: len(registry.executions_for_arm(self.session, arm.arm_id))
            for arm in self.registered.arms
        }
        self._run_every_arm()
        after = {
            arm.arm_id: len(registry.executions_for_arm(self.session, arm.arm_id))
            for arm in self.registered.arms
        }
        self.assertEqual(before, after)


class ScorecardTests(AcceptanceTestCase):
    def test_the_card_separates_clean_evidence_from_reconstructed(self):
        payload = self.card()
        sections = payload["sections"]
        self.assertIn(replay.EVIDENCE_CLEAN, sections)
        self.assertIn(replay.EVIDENCE_EXPLORATORY, sections)
        clean_arms = {row["arm"] for row in sections[replay.EVIDENCE_CLEAN]["arms"]}
        exploratory_arms = {
            row["arm"] for row in sections[replay.EVIDENCE_EXPLORATORY]["arms"]
        }
        self.assertEqual(clean_arms & exploratory_arms, set())
        self.assertIn(
            "swingtrader_composite_v1@1.0.0/shadow", exploratory_arms,
            "the compatibility arm's evidence is reconstructed by construction",
        )

    def test_no_section_names_a_winner_from_this_sample(self):
        payload = self.card()
        for evidence, section in payload["sections"].items():
            with self.subTest(evidence):
                self.assertIsNone(section["winner"])
                self.assertEqual(section["label"], metrics.STATUS_INSUFFICIENT)
                self.assertTrue(section["reasons"])

    def test_the_exploratory_section_says_it_can_never_rank_a_winner(self):
        section = self.card()["sections"][replay.EVIDENCE_EXPLORATORY]
        self.assertTrue(any(
            "archival_reconstructed" in reason for reason in section["reasons"]
        ))

    def test_every_row_shows_its_sample_size_floors_and_warnings(self):
        payload = self.card()
        for section in payload["sections"].values():
            for row in section["arms"]:
                with self.subTest(row["arm"]):
                    self.assertIn("n_matured", row)
                    self.assertIn("n_open", row)
                    self.assertIn("n_distinct_dates", row)
                    self.assertEqual(row["floors"]["matured"], 100)
                    self.assertTrue(row["warnings"])

    def test_the_cost_assumptions_are_printed_and_never_implied(self):
        payload = self.card()
        self.assertEqual(payload["inputs"]["costs"]["total_bps"], 15.0)
        for section in payload["sections"].values():
            for row in section["arms"]:
                self.assertEqual(row["costs"]["slippage_bps"], 10.0)

    def test_the_refused_arm_is_reported_rather_than_omitted(self):
        payload = self.card()
        refused = {row["arm"] for row in payload["refusals"]}
        self.assertIn("swingtrader_composite_v1@1.0.0/shadow", refused)

    def test_uncertainty_intervals_carry_their_seed_and_replication_count(self):
        payload = self.card()
        rows = [
            row
            for section in payload["sections"].values()
            for row in section["arms"]
            if row["uncertainty"]
        ]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["uncertainty"]["method"], "stationary_block_bootstrap")
            self.assertEqual(row["uncertainty"]["reps"], 50)
            self.assertIsNotNone(row["uncertainty"]["seed"])

    def test_the_multiple_testing_denominator_is_every_variant_tried(self):
        payload = self.card()
        self.assertEqual(payload["variants"]["n_trials"], 4)
        for section in payload["sections"].values():
            for row in section["arms"]:
                self.assertEqual(row["n_trials"], 4)

    def test_the_markdown_carries_the_same_verdict_as_the_json(self):
        payload = self.card()
        document = scoreboard.render_markdown(payload)
        self.assertIn("# Strategy Lab scoreboard — q1_2026_roster", document)
        self.assertIn("insufficient_evidence", document)
        self.assertIn("Exploratory — archival_reconstructed", document)
        self.assertIn("Pre-registered: **4**", document)
        self.assertNotIn("winner: `", document)

    def test_the_card_is_a_pure_function_of_its_inputs(self):
        self.assertEqual(
            scoreboard.to_json(self.card()), scoreboard.to_json(self.card())
        )


class CliDeterminismTests(AcceptanceTestCase):
    def _run_cli(self, directory: Path, suffix: str) -> tuple[str, str]:
        json_path = directory / f"scoreboard-{suffix}.json"
        markdown_path = directory / f"scoreboard-{suffix}.md"
        code = scoreboard.main([
            "--database-url", self.db.url,
            "--experiment", EXPERIMENT,
            "--cutoff", CUTOFF.isoformat(),
            "--json", str(json_path),
            "--markdown", str(markdown_path),
            "--reps", "50",
            "--seed", "20260908",
        ])
        self.assertEqual(code, 0)
        return (
            json_path.read_text(encoding="utf-8"),
            markdown_path.read_text(encoding="utf-8"),
        )

    def test_the_same_input_produces_byte_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            first_json, first_markdown = self._run_cli(directory, "a")
            second_json, second_markdown = self._run_cli(directory, "b")
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)

    def test_the_artifact_contains_no_wall_clock(self):
        """A card that changed because it was generated twice is not evidence."""
        with tempfile.TemporaryDirectory() as raw:
            document, _ = self._run_cli(Path(raw), "a")
        payload = json.loads(document)
        self.assertEqual(payload["inputs"]["cutoff_utc"], CUTOFF.isoformat())
        self.assertEqual(payload["schema"], scoreboard.SCHEMA)

    def test_a_different_seed_is_recorded_rather_than_hidden(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            baseline = json.loads(self._run_cli(directory, "a")[0])
            code = scoreboard.main([
                "--database-url", self.db.url,
                "--experiment", EXPERIMENT,
                "--cutoff", CUTOFF.isoformat(),
                "--json", str(directory / "other.json"),
                "--reps", "50",
                "--seed", "424242",
            ])
            self.assertEqual(code, 0)
            other = json.loads((directory / "other.json").read_text(encoding="utf-8"))
        seeds = {
            row["uncertainty"]["seed"]
            for payload in (baseline, other)
            for section in payload["sections"].values()
            for row in section["arms"]
            if row["uncertainty"]
        }
        self.assertEqual(seeds, {20260908, 424242})


if __name__ == "__main__":
    unittest.main()
