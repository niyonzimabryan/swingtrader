"""Spec Q §10, §16: the measurement layer, and what it refuses to say.

The arithmetic is hand-computed and asserted exactly — an equity curve of three
known returns has one drawdown, and it is written out with the sum that produced
it. The refusals are the point of the file:

* missing costs **block** a result instead of producing a zero-cost one;
* an open position is counted, labelled and excluded, never scored as zero;
* a small-sample leader is never called a winner, however far ahead it is;
* clean and reconstructed evidence never meet in one statistic;
* a reserved holdout read during development raises.

The two inference backends are exercised twice: once through the real
``comparables.inference`` adapter in ``scripts/strategy_lab_scoreboard.py``,
where the Šidák family level is checked against ``inference.sidak_adjusted``
itself rather than against the algebra reimplemented here, and once through a
stub that lets the ranking gates be driven to every outcome without a
thousand-replication bootstrap in a unit test.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import date, timedelta

from comparables import inference
from scripts.strategy_lab_scoreboard import (
    ComparablesMultiplicity,
    ComparablesUncertainty,
)
from strategy_lab import metrics
from strategy_lab.replay import EVIDENCE_CLEAN, EVIDENCE_EXPLORATORY, CostAssumptions

COSTS = CostAssumptions(slippage_bps=10.0, half_spread_bps=5.0)
START = date(2026, 1, 5)


def obs(
    arm="a",
    ticker="AAPL",
    *,
    day=0,
    hold=5,
    net=1.0,
    gross=None,
    matured=True,
    costs=1.0,
    evidence=EVIDENCE_CLEAN,
    r=None,
    notional=10_000.0,
) -> metrics.TradeObservation:
    entry = START + timedelta(days=day)
    return metrics.TradeObservation(
        arm=arm,
        ticker=ticker,
        entry_date=entry,
        exit_date=entry + timedelta(days=hold),
        holding_days=hold,
        gross_pct=net + 0.1 if gross is None else gross,
        matured=matured,
        evidence_class=evidence,
        rule_fired="time",
        net_pct=net if costs is not None else None,
        r_multiple=(net / 2.0 if r is None else r) if costs is not None else None,
        costs=costs,
        notional=notional,
    )


def a_sample(arm="a", *, n=40, nets=None, evidence=EVIDENCE_CLEAN):
    """`n` trades on distinct dates and distinct tickers."""
    nets = nets if nets is not None else [1.0 if i % 3 else -0.5 for i in range(n)]
    return [
        obs(arm, f"T{i:03d}", day=i * 3, net=nets[i], evidence=evidence)
        for i in range(n)
    ]


LOW_FLOORS = metrics.EvidenceFloors(matured=5, distinct_dates=3, closed=5)


@dataclass(frozen=True)
class StubUncertainty:
    """A deterministic interval, so a gate test is about the gate.

    The interval widens with the confidence level, the way a percentile
    bootstrap does — a *tighter* interval at a stricter level would be
    nonsense, and the multiplicity tests below depend on the widening being
    real. The 10x factor is chosen so that the Šidák step from one trial to
    forty moves the bound by ~0.15 rather than ~0.02: a gate that turns on a
    margin near the float epsilon is not being tested, it is being coin-flipped
    (this fixture once sat on a one-ULP tie and passed on 3.11 while failing on
    3.12).
    """

    half_width: float = 0.2
    shift: float = 0.0

    def interval(self, series, event_dates, *, horizon, level):
        estimate = sum(series) / len(series)
        width = self.half_width * (1.0 + 10.0 * (level - 0.9))
        return metrics.Interval(
            estimate=estimate,
            lower=estimate - width + self.shift,
            upper=estimate + width + self.shift,
            level=level,
            method="stub",
            block_length=1,
            reps=1,
            seed=0,
            n_eff=float(len(set(event_dates))),
        )


@dataclass(frozen=True)
class StubMultiplicity:
    rejected: bool = True

    def family_level(self, level, n_trials):
        return float(level ** (1.0 / n_trials))

    def stepm(self, family_series, target):
        return self.rejected, "stub"


class ArithmeticTests(unittest.TestCase):
    def test_the_equity_curve_drawdown_and_profit_factor_are_hand_checkable(self):
        # +10%, -20%, +5% one trade at a time:
        #   1.0 -> 1.10 -> 0.88 -> 0.924
        # peak 1.10, trough 0.88 -> drawdown = 0.88/1.10 - 1 = -20%
        # profit factor = (10 + 5) / 20 = 0.75; win rate = 2/3
        sample = [
            obs("a", "AAA", day=0, net=10.0),
            obs("a", "BBB", day=10, net=-20.0),
            obs("a", "CCC", day=20, net=5.0),
        ]
        result = metrics.evaluate_arm("a", sample, floors=LOW_FLOORS, costs=COSTS)
        self.assertAlmostEqual(result.max_drawdown_pct, -20.0, places=9)
        self.assertAlmostEqual(result.profit_factor, 0.75, places=9)
        self.assertAlmostEqual(result.win_rate, 2 / 3, places=9)
        self.assertAlmostEqual(result.total_return_pct, -7.6, places=9)
        self.assertAlmostEqual(result.mean_net_pct, (10.0 - 20.0 + 5.0) / 3)
        self.assertAlmostEqual(result.median_net_pct, 5.0)

    def test_exposure_and_turnover_state_their_definitions_in_the_numbers(self):
        # Three 5-day holds over a window from 5 Jan to 25 Jan (20 days):
        #   exposure = 15 position-days / 20 calendar days = 0.75
        #   turnover = 3 closed trades / (20 / 365.25) years
        sample = [
            obs("a", "AAA", day=0, hold=5, net=1.0),
            obs("a", "BBB", day=5, hold=5, net=1.0),
            obs("a", "CCC", day=15, hold=5, net=1.0),
        ]
        result = metrics.evaluate_arm("a", sample, floors=LOW_FLOORS, costs=COSTS)
        self.assertAlmostEqual(result.exposure_positions_per_day, 0.75, places=9)
        self.assertAlmostEqual(
            result.turnover_trades_per_year, 3 / (20 / 365.25), places=6
        )

    def test_a_benchmark_is_reported_relative_and_with_exposure(self):
        benchmark = metrics.benchmark_from_returns(
            "spy_total_return", [(START, 400.0), (START + timedelta(days=20), 404.0)]
        )
        self.assertAlmostEqual(benchmark.return_pct, 1.0)
        result = metrics.evaluate_arm(
            "a",
            [obs("a", "AAA", day=0, net=3.0), obs("a", "BBB", day=10, net=2.0)],
            floors=metrics.EvidenceFloors(matured=2, distinct_dates=2, closed=2),
            costs=COSTS,
            benchmark=benchmark,
        )
        # 1.03 x 1.02 = 1.0506 -> 5.06% against the benchmark's 1.00%
        self.assertAlmostEqual(result.total_return_pct, 5.06, places=9)
        self.assertAlmostEqual(result.benchmark_relative_pct, 4.06, places=9)
        self.assertIsNotNone(result.exposure_positions_per_day)

    def test_no_losing_trade_leaves_the_profit_factor_undefined_and_says_so(self):
        result = metrics.evaluate_arm(
            "a", a_sample(nets=[1.0] * 40), floors=LOW_FLOORS, costs=COSTS
        )
        self.assertIsNone(result.profit_factor)
        self.assertIn(metrics.WARN_PROFIT_FACTOR_UNDEFINED, result.warnings)


class RefusalTests(unittest.TestCase):
    def test_missing_costs_block_the_result_rather_than_zeroing_them(self):
        """Spec Q §16: 'costs missing -> result blocked, not zero-cost'."""
        sample = a_sample()
        sample[3] = replace(sample[3], net_pct=None, costs=None, r_multiple=None)
        result = metrics.evaluate_arm("a", sample, floors=LOW_FLOORS)
        self.assertEqual(result.status, metrics.STATUS_BLOCKED)
        self.assertIn(metrics.WARN_COSTS_MISSING, result.warnings)
        self.assertIsNone(result.mean_net_pct)
        self.assertIsNone(result.total_return_pct)
        self.assertIsNone(result.profit_factor)
        self.assertEqual(result.n_matured, len(sample))

    def test_open_positions_are_counted_and_excluded_never_scored_as_zero(self):
        """Spec Q §9: a long-horizon arm is not ranked on incomplete positions."""
        sample = a_sample(n=10) + [
            obs("a", "OPEN1", day=100, matured=False),
            obs("a", "OPEN2", day=103, matured=False),
        ]
        result = metrics.evaluate_arm("a", sample, floors=LOW_FLOORS, costs=COSTS)
        self.assertEqual(result.n_open, 2)
        self.assertEqual(result.n_matured, 10)
        self.assertIn(metrics.WARN_OPEN_OBSERVATIONS, result.warnings)
        self.assertEqual(result.open_tickers, ("OPEN1", "OPEN2"))
        matured_only = metrics.evaluate_arm(
            "a", a_sample(n=10), floors=LOW_FLOORS, costs=COSTS
        )
        self.assertAlmostEqual(result.mean_net_pct, matured_only.mean_net_pct)

    def test_no_matured_observation_is_insufficient_evidence_not_a_zero_return(self):
        result = metrics.evaluate_arm(
            "a", [obs("a", "OPEN", matured=False)], floors=LOW_FLOORS, costs=COSTS
        )
        self.assertEqual(result.status, metrics.STATUS_INSUFFICIENT)
        self.assertIsNone(result.mean_net_pct)
        self.assertEqual(result.n_matured, 0)

    def test_a_sample_below_the_floors_is_shown_with_its_n_and_labelled(self):
        result = metrics.evaluate_arm(
            "a", a_sample(n=6), floors=metrics.EvidenceFloors()
        )
        self.assertEqual(result.status, metrics.STATUS_INSUFFICIENT)
        self.assertIn(metrics.WARN_BELOW_MATURED_FLOOR, result.warnings)
        self.assertEqual(result.n_matured, 6)
        self.assertEqual(result.floors.matured, 100)

    def test_mixed_evidence_classes_never_share_a_statistic(self):
        with self.assertRaises(metrics.MixedEvidence):
            metrics.evaluate_arm(
                "a",
                a_sample(n=4) + a_sample(n=4, evidence=EVIDENCE_EXPLORATORY),
                floors=LOW_FLOORS, costs=COSTS,
            )

    def test_reconstructed_evidence_carries_its_warning_everywhere(self):
        result = metrics.evaluate_arm(
            "a", a_sample(evidence=EVIDENCE_EXPLORATORY),
            floors=LOW_FLOORS, costs=COSTS,
        )
        self.assertEqual(result.evidence_class, EVIDENCE_EXPLORATORY)
        self.assertIn(metrics.WARN_EXPLORATORY, result.warnings)

    def test_an_absent_backend_is_a_warning_and_not_a_silent_approximation(self):
        result = metrics.evaluate_arm("a", a_sample(), floors=LOW_FLOORS, costs=COSTS)
        self.assertIsNone(result.uncertainty)
        self.assertIn(metrics.WARN_UNCERTAINTY_UNAVAILABLE, result.warnings)
        self.assertIn(metrics.WARN_MULTIPLICITY_UNAVAILABLE, result.warnings)


class SplitTests(unittest.TestCase):
    """Spec Q §10: chronological only, with purge and embargo."""

    def test_folds_are_chronological_and_never_shuffled(self):
        sample = a_sample(n=30)
        folds = metrics.chronological_folds(sample, n_folds=3)
        self.assertEqual(len(folds), 2)
        for fold in folds:
            self.assertTrue(fold.train)
            for train in fold.train:
                for validation in fold.validation:
                    self.assertLess(train.entry_date, validation.entry_date)

    def test_an_overlapping_label_window_is_purged_from_training(self):
        # A 40-day hold entered before the validation block still resolves
        # inside it, so its label knows the validation period's returns.
        sample = [
            obs("a", "AAA", day=0, hold=1),
            obs("a", "BBB", day=3, hold=40),   # reaches into the validation block
            obs("a", "CCC", day=6, hold=1),
            obs("a", "DDD", day=30, hold=1),
        ]
        folds = metrics.chronological_folds(sample, n_folds=2)
        purged = {o.ticker for o in folds[0].purged}
        self.assertIn("BBB", purged)
        self.assertNotIn("BBB", {o.ticker for o in folds[0].train})

    def test_the_embargo_widens_the_purge(self):
        sample = [
            obs("a", "AAA", day=0, hold=1),
            obs("a", "BBB", day=10, hold=1),   # exits 9 days before validation
            obs("a", "CCC", day=12, hold=1),
            obs("a", "DDD", day=20, hold=1),
        ]
        without = metrics.chronological_folds(sample, n_folds=2, embargo_days=0)
        with_embargo = metrics.chronological_folds(sample, n_folds=2, embargo_days=15)
        self.assertLess(len(with_embargo[0].train), len(without[0].train))
        self.assertEqual(with_embargo[0].embargo_days, 15)

    def test_too_few_observations_refuse_to_be_split(self):
        with self.assertRaises(metrics.MetricsError):
            metrics.chronological_folds(a_sample(n=2), n_folds=3)

    def test_a_single_fold_is_not_a_walk_forward_split(self):
        with self.assertRaises(metrics.MetricsError):
            metrics.chronological_folds(a_sample(n=10), n_folds=1)


class HoldoutTests(unittest.TestCase):
    def test_the_holdout_is_the_most_recent_slice_by_entry_date(self):
        holdout = metrics.reserve_holdout(a_sample(n=20), fraction=0.25)
        self.assertEqual(len(holdout.reserved), 5)
        self.assertEqual(len(holdout.development), 15)
        self.assertTrue(all(
            o.entry_date < holdout.start_date for o in holdout.development
        ))

    def test_reading_the_holdout_during_development_raises(self):
        sample = a_sample(n=20)
        holdout = metrics.reserve_holdout(sample, fraction=0.25)
        with self.assertRaises(metrics.HoldoutViolation) as caught:
            metrics.evaluate_arm(
                "a", sample, floors=LOW_FLOORS, costs=COSTS, holdout=holdout
            )
        self.assertIn("no longer a holdout", str(caught.exception))

    def test_the_development_slice_evaluates_without_touching_the_holdout(self):
        sample = a_sample(n=20)
        holdout = metrics.reserve_holdout(sample, fraction=0.25)
        result = metrics.evaluate_arm(
            "a", holdout.development, floors=LOW_FLOORS, costs=COSTS, holdout=holdout
        )
        self.assertEqual(result.n_matured, 15)

    def test_unsealing_is_explicit(self):
        sample = a_sample(n=20)
        holdout = metrics.reserve_holdout(sample, fraction=0.25)
        result = metrics.evaluate_arm(
            "a", sample, floors=LOW_FLOORS, costs=COSTS,
            holdout=holdout, unseal_holdout=True,
        )
        self.assertEqual(result.n_matured, 20)


class OverlapTests(unittest.TestCase):
    """Spec Q §10: correlation and overlapping ticker/time exposure, both."""

    def test_two_arms_taking_the_same_trades_show_full_overlap(self):
        left = a_sample("a", n=10)
        right = [replace(o, arm="b") for o in left]
        pair = metrics.overlap("a", left, "b", right)
        self.assertEqual(pair.n_shared, 10)
        self.assertAlmostEqual(pair.jaccard, 1.0)
        self.assertAlmostEqual(pair.correlation, 1.0, places=9)

    def test_disjoint_arms_show_no_overlap_and_no_correlation(self):
        left = a_sample("a", n=6)
        right = [
            obs("b", f"Z{i:03d}", day=200 + i * 3, net=1.0) for i in range(6)
        ]
        pair = metrics.overlap("a", left, "b", right)
        self.assertEqual(pair.n_shared, 0)
        self.assertAlmostEqual(pair.jaccard, 0.0)
        self.assertIsNone(pair.correlation)
        self.assertIn("at least", pair.correlation_note)

    def test_a_correlation_needs_enough_shared_trades_to_mean_anything(self):
        left = a_sample("a", n=6)
        right = [replace(left[0], arm="b"), replace(left[1], arm="b")]
        pair = metrics.overlap("a", left, "b", right)
        self.assertIsNone(pair.correlation)
        self.assertEqual(pair.n_shared, 2)

    def test_opposite_outcomes_on_shared_trades_correlate_negatively(self):
        left = [obs("a", f"T{i}", day=i * 3, net=float(i)) for i in range(6)]
        right = [obs("b", f"T{i}", day=i * 3, net=float(-i)) for i in range(6)]
        pair = metrics.overlap("a", left, "b", right)
        self.assertAlmostEqual(pair.correlation, -1.0, places=9)

    def test_shared_exposure_days_are_reported(self):
        left = [obs("a", "AAA", day=0, hold=10)]
        right = [obs("b", "AAA", day=0, hold=4)]
        pair = metrics.overlap("a", left, "b", right)
        self.assertEqual(pair.shared_exposure_days, 4)

    def test_pairwise_overlaps_are_stable_and_unordered(self):
        by_arm = {"b": a_sample("b", n=6), "a": a_sample("a", n=6)}
        pairs = metrics.pairwise_overlaps(by_arm)
        self.assertEqual([(p.left, p.right) for p in pairs], [("a", "b")])


class RankingTests(unittest.TestCase):
    """The one property this file exists for: no small-sample winner."""

    def _rows(self, *, n=60, leader_net=3.0, rival_net=0.2, floors=LOW_FLOORS,
              uncertainty=None, multiplicity=None, benchmark=None, n_trials=2):
        benchmark = benchmark or metrics.Benchmark(
            "spy_total_return", START, START + timedelta(days=200), 0.0
        )
        by_arm = {
            "leader": a_sample("leader", n=n, nets=[leader_net] * n),
            "rival": a_sample("rival", n=n, nets=[rival_net] * n),
        }
        rows = [
            metrics.evaluate_arm(
                arm, obs_list, floors=floors, costs=COSTS, benchmark=benchmark,
                uncertainty=uncertainty, multiplicity=multiplicity,
                n_trials=n_trials,
            )
            for arm, obs_list in sorted(by_arm.items())
        ]
        return rows, by_arm

    def test_a_small_sample_leader_is_never_called_a_winner(self):
        rows, by_arm = self._rows(
            n=6, floors=metrics.EvidenceFloors(),
            uncertainty=StubUncertainty(), multiplicity=StubMultiplicity(),
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=metrics.EvidenceFloors(),
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertEqual(board.label, metrics.STATUS_INSUFFICIENT)
        self.assertTrue(any("below its floors" in r for r in board.reasons))
        # It is still ranked first — hiding the ordering would be its own lie.
        self.assertEqual(board.rows[0].arm, "leader")

    def test_a_blocked_arm_stops_the_whole_board_from_naming_a_winner(self):
        rows, by_arm = self._rows(
            uncertainty=StubUncertainty(), multiplicity=StubMultiplicity()
        )
        blocked = metrics.evaluate_arm(
            "broken",
            [replace(o, net_pct=None, costs=None, r_multiple=None)
             for o in a_sample("broken", n=60)],
            floors=LOW_FLOORS,
        )
        board = metrics.rank_arms(
            rows + [blocked], by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("is blocked" in r for r in board.reasons))

    def test_reconstructed_evidence_can_never_rank_a_winner(self):
        by_arm = {
            "leader": a_sample("leader", n=60, nets=[3.0] * 60,
                               evidence=EVIDENCE_EXPLORATORY),
        }
        rows = [metrics.evaluate_arm(
            "leader", by_arm["leader"], floors=LOW_FLOORS, costs=COSTS,
            benchmark=metrics.Benchmark("spy", START, START, 0.0),
            uncertainty=StubUncertainty(), multiplicity=StubMultiplicity(),
            n_trials=1,
        )]
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=1,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("archival_reconstructed" in r for r in board.reasons))

    def test_a_leader_that_clears_every_gate_is_named(self):
        rows, by_arm = self._rows(
            uncertainty=StubUncertainty(), multiplicity=StubMultiplicity()
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertEqual(board.winner, "leader")
        self.assertEqual(board.label, "leader_separated")
        self.assertEqual(board.reasons, ())

    def test_an_interval_that_does_not_exclude_zero_names_no_winner(self):
        rows, by_arm = self._rows(
            leader_net=0.05, rival_net=0.01,
            uncertainty=StubUncertainty(half_width=1.0),
            multiplicity=StubMultiplicity(),
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("does not exclude zero" in r for r in board.reasons))

    def test_a_leader_that_has_not_separated_from_the_runner_up_is_not_a_winner(self):
        rows, by_arm = self._rows(
            leader_net=3.0, rival_net=2.95,
            uncertainty=StubUncertainty(half_width=0.5),
            multiplicity=StubMultiplicity(),
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("has not separated" in r for r in board.reasons))

    def test_a_leader_that_loses_to_its_benchmark_is_not_a_winner(self):
        rows, by_arm = self._rows(
            uncertainty=StubUncertainty(), multiplicity=StubMultiplicity(),
            benchmark=metrics.Benchmark(
                "spy_total_return", START, START + timedelta(days=200), 1e9
            ),
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("did not beat" in r for r in board.reasons))

    def test_a_family_wise_test_that_does_not_reject_names_no_winner(self):
        rows, by_arm = self._rows(
            uncertainty=StubUncertainty(),
            multiplicity=StubMultiplicity(rejected=False),
        )
        board = metrics.rank_arms(
            rows, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(rejected=False), n_trials=2,
        )
        self.assertIsNone(board.winner)
        self.assertTrue(any("step-M" in r for r in board.reasons))

    def test_without_an_uncertainty_backend_nothing_can_be_a_winner(self):
        rows, by_arm = self._rows()
        board = metrics.rank_arms(rows, by_arm=by_arm, floors=LOW_FLOORS, n_trials=2)
        self.assertIsNone(board.winner)
        self.assertTrue(any("no uncertainty interval" in r for r in board.reasons))

    def test_more_trials_widen_the_adjustment_and_can_remove_a_winner(self):
        """The multiple-testing denominator is not decoration.

        Leader +0.30 against a rival at +0.05, with a stub half-width of 0.15:

        * 1 trial  -> level 0.900000, adjusted lower 0.150000, clearing the
          rival's point estimate by +0.100;
        * 40 trials -> level 0.997369, adjusted lower 0.0039458, missing it by
          -0.046 while still sitting above zero.

        So it is the *separation* gate that removes the winner, not the
        exclude-zero one, and both margins are ~14 orders of magnitude above
        float noise so the verdict cannot turn on the interpreter's rounding.
        """
        settings = dict(
            leader_net=0.30, rival_net=0.05,
            uncertainty=StubUncertainty(half_width=0.15),
            multiplicity=StubMultiplicity(),
        )
        rows_few, by_arm = self._rows(**settings, n_trials=1)
        few = metrics.rank_arms(
            rows_few, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=1,
        )
        rows_many, _ = self._rows(**settings, n_trials=40)
        many = metrics.rank_arms(
            rows_many, by_arm=by_arm, floors=LOW_FLOORS,
            multiplicity=StubMultiplicity(), n_trials=40,
        )
        self.assertAlmostEqual(rows_few[0].adjusted_lower, 0.15, places=6)
        self.assertAlmostEqual(rows_many[0].adjusted_lower, 0.0039458, places=6)
        self.assertGreater(
            rows_few[0].adjusted_lower, rows_many[0].adjusted_lower,
            "40 trials must not produce a tighter bound than 1",
        )
        self.assertGreater(
            rows_many[0].adjusted_lower, 0.0,
            "the 40-trial bound still excludes zero; separation is what fails",
        )
        self.assertEqual(few.winner, "leader")
        self.assertIsNone(many.winner)
        self.assertTrue(any("has not separated" in r for r in many.reasons))

    def test_a_scoreboard_ranks_one_evidence_class_at_a_time(self):
        clean = metrics.evaluate_arm(
            "a", a_sample("a"), floors=LOW_FLOORS, costs=COSTS
        )
        exploratory = metrics.evaluate_arm(
            "b", a_sample("b", evidence=EVIDENCE_EXPLORATORY),
            floors=LOW_FLOORS, costs=COSTS,
        )
        with self.assertRaises(metrics.MixedEvidence):
            metrics.rank_arms([clean, exploratory], floors=LOW_FLOORS)

    def test_undeclared_variants_raise_a_warning(self):
        self.assertEqual(
            metrics.variant_warnings(declared=4, tried=7),
            (metrics.WARN_UNDECLARED_VARIANTS,),
        )
        self.assertEqual(metrics.variant_warnings(declared=4, tried=4), ())


class InferenceBridgeTests(unittest.TestCase):
    """The adapter really is `comparables/inference.py`, not a copy of it."""

    def test_the_family_level_is_the_exact_inverse_of_sidak_adjusted(self):
        backend = ComparablesMultiplicity()
        for level in (0.80, 0.90, 0.95):
            for trials in (1, 2, 5, 40):
                with self.subTest(level=level, trials=trials):
                    per_comparison = backend.family_level(level, trials)
                    family_alpha = inference.sidak_adjusted(
                        1.0 - per_comparison, trials
                    )
                    self.assertAlmostEqual(family_alpha, 1.0 - level, places=12)

    def test_the_family_level_is_stricter_as_trials_grow(self):
        backend = ComparablesMultiplicity()
        levels = [backend.family_level(0.9, m) for m in (1, 2, 10, 100)]
        self.assertEqual(levels, sorted(levels))

    def test_the_interval_comes_from_the_stationary_block_bootstrap(self):
        backend = ComparablesUncertainty(reps=100, seed=7)
        series = [1.0, -0.5, 2.0, 0.25, -1.0, 3.0, 0.5, -0.25, 1.5, 0.75]
        dates = [START + timedelta(days=3 * i) for i in range(len(series))]
        interval = backend.interval(series, dates, horizon=5, level=0.9)
        self.assertEqual(interval.method, "stationary_block_bootstrap")
        self.assertEqual(interval.reps, 100)
        self.assertEqual(interval.seed, 7)
        self.assertAlmostEqual(interval.estimate, sum(series) / len(series))
        self.assertLessEqual(interval.lower, interval.estimate)
        self.assertGreaterEqual(interval.upper, interval.estimate)
        # Distinct dates, so the design effect leaves n_eff at n.
        self.assertAlmostEqual(interval.n_eff, float(len(series)))

    def test_the_effective_sample_size_falls_when_events_cluster(self):
        backend = ComparablesUncertainty(reps=50, seed=7)
        series = [1.0, 1.05, 0.95, -1.0, -1.05, -0.95]
        clustered = [START, START, START] + [START + timedelta(days=30)] * 3
        interval = backend.interval(series, clustered, horizon=1, level=0.9)
        self.assertLess(interval.n_eff, float(len(series)))

    def test_the_same_seed_reproduces_the_same_interval(self):
        backend = ComparablesUncertainty(reps=100, seed=11)
        series = [0.5, -0.25, 1.0, 0.75, -1.5, 2.0, 0.1, 0.2, -0.3, 0.4]
        dates = [START + timedelta(days=3 * i) for i in range(len(series))]
        first = backend.interval(series, dates, horizon=2, level=0.9)
        second = backend.interval(series, dates, horizon=2, level=0.9)
        self.assertEqual(first.canonical(), second.canonical())


if __name__ == "__main__":
    unittest.main()
