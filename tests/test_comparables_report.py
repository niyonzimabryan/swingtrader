"""The response contract (Spec N §8) and the refusal rules."""

from __future__ import annotations

import unittest
from datetime import date

from comparables import config, fixtures as fx
from comparables.outcomes import calendar_time_alpha, calendar_time_series
from comparables.report import (
    CITATION_REQUIRED_FIELDS,
    CohortAnswer,
    CohortPrediction,
    ModelNumberError,
    NotCitableError,
    PolicySummary,
    QuickCohortAnswer,
    RefusedAnswer,
    assert_citable,
    build_answer,
    cells_examined,
    cohort_answer_from_text,
    evidence_tier_for,
    floor_check,
    to_json,
)
from comparables.setup_spec import Condition, SetupSpec

FAST = dict(reps=200, null_draws=25)


def spec(horizons=(5,), slug="syn_v1") -> SetupSpec:
    return SetupSpec(
        slug=slug,
        version="1.0.0",
        conditions=(Condition("sue_seasonal", ">", 1.0),),
        universe="liquid_us_equity_v1",
        horizons_sessions=horizons,
        execution_policy="event_swing_14cal_v1",
        match_covariates=("market_cap_decile", "liquidity_decile"),
        lookback_years=5,
    )


class _Cohorts:
    """Built once; these synthetic worlds are the expensive part of the suite."""

    _cache: dict = {}

    @classmethod
    def get(cls, key, **kwargs):
        if key not in cls._cache:
            cls._cache[key] = fx.synthetic_cohort(**kwargs)
        return cls._cache[key]


def clearing_cohort():
    return _Cohorts.get("clearing", n_dates=20, per_date=2, sessions=400,
                        seed=3, effect_daily=0.0008)


class RefusalTests(unittest.TestCase):
    def test_insufficient_is_returned_not_hedged(self):
        answer = build_answer(fx.SETUP, fx.cohort(), fx.benchmark_series(),
                              fx.CALENDAR, policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, RefusedAnswer)
        self.assertEqual(answer.status, "insufficient")
        self.assertIn("distinct event dates", answer.refusal_reason)

        # No statistic fields exist at all — not empty ones, none.
        for field in ("horizons", "policy", "stability", "null_tests", "n_eff",
                      "regime_breakdown", "shrinkage", "balance", "cost_model"):
            self.assertFalse(hasattr(answer, field),
                             f"a refusal must not carry {field}")
        # But it does carry the composition facts that justify the refusal.
        self.assertEqual(answer.n_matured, 6)
        self.assertEqual(answer.n_distinct_dates, 2)
        self.assertAlmostEqual(answer.delisting_rate, 1 / 6, places=12)
        self.assertEqual(answer.evidence_tier, "vendor_pit")
        self.assertEqual(answer.trials_against_this_pattern, 1)

    def test_clustered_cohort_reports_distinct_dates(self):
        """Thirty events on two dates are two observations wearing a disguise."""
        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=2, per_date=15, sessions=120, seed=8)
        self.assertEqual(len(events), 30)

        answer = build_answer(spec(), events, benchmark, calendar,
                              policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, RefusedAnswer)
        self.assertEqual(answer.n_distinct_dates, 2)
        self.assertEqual(answer.n_matured, 30)
        self.assertIn("2 distinct event dates", answer.refusal_reason)
        self.assertIn("wrong denominator", answer.refusal_reason)

        # The matured floor alone would have let this through.
        self.assertGreaterEqual(answer.n_matured, config.COHORT_FLOOR_MATURED)

    def test_floor_is_on_distinct_dates(self):
        """60 events on 12 dates is insufficient; 30 events on 20 dates is not."""
        calendar, benchmark, many = fx.synthetic_cohort(
            n_dates=12, per_date=5, sessions=300, seed=14)
        self.assertEqual(len(many), 60)
        answer = build_answer(spec(), many, benchmark, calendar,
                              policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, RefusedAnswer)
        self.assertEqual(answer.n_distinct_dates, 12)
        self.assertEqual(answer.n_matured, 60)
        self.assertIn("12 distinct event dates", answer.refusal_reason)

        # Twenty dates carrying thirty events between them clears both floors.
        cal2, bench2, wide = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=14)
        by_date: dict = {}
        for event in wide:
            by_date.setdefault(event.known_at_utc, []).append(event)
        trimmed = []
        for i, group in enumerate(by_date.values()):
            trimmed.extend(group if i < 10 else group[:1])
        self.assertEqual(len(trimmed), 30)
        self.assertEqual(len({e.known_at_utc for e in trimmed}), 20)
        ok = build_answer(spec(), trimmed, bench2, cal2, policy=fx.POLICY, **FAST)
        self.assertIsInstance(ok, CohortAnswer)
        self.assertEqual(ok.n_matured, 30)
        self.assertEqual(ok.n_distinct_dates, 20)

    def test_single_floor_config(self):
        """Changing one constant moves every floor check at once."""
        calendar, benchmark, events = fx.CALENDAR, fx.benchmark_series(), fx.cohort()
        self.assertIsInstance(
            build_answer(fx.SETUP, events, benchmark, calendar,
                         policy=fx.POLICY, **FAST),
            RefusedAnswer)
        self.assertIsNotNone(
            floor_check(events, calendar, (10,), floors=config.get_floors()))

        original = (config.COHORT_FLOOR_DISTINCT_DATES, config.COHORT_FLOOR_MATURED)
        try:
            config.COHORT_FLOOR_DISTINCT_DATES = 2
            config.COHORT_FLOOR_MATURED = 3
            floors = config.get_floors()
            self.assertEqual((floors.distinct_dates, floors.matured), (2, 3))
            self.assertIsNone(floor_check(events, calendar, (10,), floors=floors))
            answer = build_answer(fx.SETUP, events, benchmark, calendar,
                                  policy=fx.POLICY, **FAST)
            self.assertIsInstance(answer, CohortAnswer)
            self.assertEqual(answer.floors.distinct_dates, 2)
            self.assertEqual(answer.floors.matured, 3)
        finally:
            (config.COHORT_FLOOR_DISTINCT_DATES,
             config.COHORT_FLOOR_MATURED) = original

        self.assertEqual(config.COHORT_FLOOR_DISTINCT_DATES, 20)
        self.assertEqual(config.COHORT_FLOOR_MATURED, 30)
        self.assertIsInstance(
            build_answer(fx.SETUP, events, benchmark, calendar,
                         policy=fx.POLICY, **FAST),
            RefusedAnswer)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        calendar, benchmark, events = clearing_cohort()
        cls.calendar, cls.benchmark, cls.events = calendar, benchmark, events
        cls.answer = build_answer(spec(), events, benchmark, calendar,
                                  policy=fx.POLICY, **FAST)

    def test_status_and_tier_separate(self):
        self.assertIsInstance(self.answer, CohortAnswer)
        self.assertEqual(self.answer.status, "ok")
        self.assertEqual(self.answer.evidence_tier, "vendor_pit")

        # Tier is data quality; status is whether an answer exists. They move
        # independently: the same cohort refused still carries its tier.
        refused = build_answer(fx.SETUP, fx.cohort(), fx.benchmark_series(),
                               fx.CALENDAR, policy=fx.POLICY, **FAST)
        self.assertEqual(refused.status, "insufficient")
        self.assertEqual(refused.evidence_tier, "vendor_pit")
        self.assertFalse(hasattr(refused, "horizons"))

        # Provenance drives the tier, not the status.
        import dataclasses
        live = tuple(dataclasses.replace(e, provenance="observed_live")
                     for e in self.events)
        self.assertEqual(evidence_tier_for(live), "clean_pit")
        mixed = (dataclasses.replace(self.events[0],
                                     provenance="archival_reconstructed"),
                 ) + self.events[1:]
        self.assertEqual(evidence_tier_for(mixed), "archival_reconstructed")
        mixed_answer = build_answer(spec(), mixed, self.benchmark, self.calendar,
                                    policy=fx.POLICY, **FAST)
        self.assertEqual(mixed_answer.status, "insufficient")
        self.assertIn("never combined", mixed_answer.refusal_reason)

    def test_calendar_time_is_primary(self):
        result = self.answer.horizons[0]
        series = calendar_time_series(self.events, self.benchmark, self.calendar, 5)
        expected = calendar_time_alpha(series, 5)

        self.assertAlmostEqual(result.headline.estimate, expected.alpha_times_h,
                               places=12)
        self.assertEqual(result.calendar_time_sessions, expected.n_sessions)
        self.assertAlmostEqual(result.calendar_time_beta, expected.beta, places=12)
        self.assertEqual(result.headline.method, "stationary_block_bootstrap")

        # The clustered CAR is a separate, clearly-labelled cross-check field.
        self.assertNotAlmostEqual(result.headline.estimate,
                                  result.clustered_car.estimate, places=6)
        self.assertAlmostEqual(result.clustered_car.estimate, result.mean_car,
                               places=12)
        from comparables.report import HorizonResult
        self.assertIn("headline", HorizonResult.__dataclass_fields__)
        self.assertIn("clustered_car", HorizonResult.__dataclass_fields__)
        self.assertIs(type(result.headline).__name__ == "ConfidenceInterval", True)
        self.assertIs(type(result.clustered_car).__name__ == "ClusteredCARResult", True)

    def test_calendar_time_is_tiebreak(self):
        """Same-sign, different-magnitude estimates headline the calendar-time
        figure; the cross-check is shown beside it, not averaged in."""
        result = self.answer.horizons[0]
        head, cross = result.headline.estimate, result.clustered_car.estimate
        self.assertGreater(head * cross, 0.0, "this fixture must agree in sign")
        self.assertNotAlmostEqual(head, cross, places=6)
        self.assertEqual(self.answer.status, "ok")

        expected = calendar_time_alpha(
            calendar_time_series(self.events, self.benchmark, self.calendar, 5), 5)
        self.assertAlmostEqual(head, expected.alpha_times_h, places=12)
        # Nothing in the answer is the average of the two.
        self.assertNotAlmostEqual(head, (head + cross) / 2.0, places=9)

    def test_single_regime_warned_not_refused(self):
        """Spec N §5.4: a one-regime cohort is warned, not refused.

        §10 still carries the v0.2 row name `test_single_regime_refuses_
        generalization`; §5.4 of the same v0.4 document explicitly drops that
        refusal. This test asserts the §5.4 behaviour and the name says so.
        """
        import dataclasses

        one_regime = tuple(dataclasses.replace(e, regime="bull")
                           for e in self.events)
        answer = build_answer(spec(), one_regime, self.benchmark, self.calendar,
                              policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, CohortAnswer)
        self.assertEqual(answer.status, "ok")
        self.assertTrue(any(w.startswith("single_regime") for w in answer.warnings))
        self.assertTrue(any("one market mood" in w for w in answer.warnings))
        self.assertEqual(len(answer.regime_breakdown), 1)

        self.assertFalse(any(w.startswith("single_regime")
                             for w in self.answer.warnings))

    def test_few_clusters_marks_clustered_se_unreliable(self):
        result = self.answer.horizons[0]
        self.assertEqual(self.answer.n_distinct_dates, 20)
        self.assertEqual(result.clustered_car.n_clusters_date, 20)
        self.assertFalse(result.clustered_car.reliable)
        self.assertIn("bootstrap CI is the only interval",
                      result.clustered_car.reliability_reason)
        self.assertTrue(any("bootstrap CI is the only interval" in w
                            for w in self.answer.warnings))
        # The bootstrap CI is still there and still complete.
        self.assertLess(result.headline.lower, result.headline.upper)

    def test_block_length_estimated_and_printed(self):
        self.assertEqual(self.answer.block_length, self.answer.horizons[0].block_length)
        block = self.answer.block_length
        self.assertGreaterEqual(block.used, 5)
        self.assertEqual(block.horizon_floor, 5)
        self.assertEqual(self.answer.horizons[0].headline.block_length, block.used)

    def test_n_eff_reported(self):
        neff = self.answer.horizons[0].n_eff
        self.assertEqual(neff.n, self.answer.horizons[0].n_matured)
        self.assertEqual(neff.n_distinct_dates, 20)
        self.assertLessEqual(neff.n_eff, neff.n)

    def test_quick_answer_not_citable(self):
        quick = build_answer(spec(), self.events, self.benchmark, self.calendar,
                             depth="quick", policy=fx.POLICY, **FAST)
        self.assertIsInstance(quick, QuickCohortAnswer)
        self.assertEqual(quick.depth, "quick")

        # It carries what a fast loop needs ...
        for field in ("n_matured", "n_distinct_dates", "horizons", "balance",
                      "evidence_tier", "trials_against_this_pattern"):
            self.assertTrue(hasattr(quick, field), field)
        self.assertLess(quick.horizons[0].headline.lower,
                        quick.horizons[0].headline.upper)

        # ... and structurally lacks every field a citation requires.
        for field in CITATION_REQUIRED_FIELDS:
            self.assertFalse(hasattr(quick, field),
                             f"a quick answer must not carry {field}")
            self.assertTrue(hasattr(self.answer, field), field)

        with self.assertRaises(NotCitableError):
            assert_citable(quick)
        self.assertIs(assert_citable(self.answer), self.answer)
        with self.assertRaises(NotCitableError):
            assert_citable(build_answer(fx.SETUP, fx.cohort(), fx.benchmark_series(),
                                        fx.CALENDAR, policy=fx.POLICY, **FAST))

    def test_cells_examined_counted(self):
        with_regimes = build_answer(spec(), self.events, self.benchmark,
                                    self.calendar, policy=fx.POLICY,
                                    include_regimes=True, **FAST)
        without = build_answer(spec(), self.events, self.benchmark, self.calendar,
                               policy=fx.POLICY, include_regimes=False, **FAST)
        n_regimes = len({e.regime for e in self.events})
        self.assertEqual(n_regimes, 2)
        self.assertEqual(with_regimes.cells_examined - without.cells_examined,
                         n_regimes)
        self.assertEqual(len(with_regimes.regime_breakdown), n_regimes)
        self.assertEqual(without.regime_breakdown, ())
        self.assertEqual(cells_examined(3, 6, 4), 33)

    def test_determinism_same_seed_byte_identical(self):
        a = build_answer(spec(), self.events, self.benchmark, self.calendar,
                         policy=fx.POLICY, seed=1234, **FAST)
        b = build_answer(spec(), self.events, self.benchmark, self.calendar,
                         policy=fx.POLICY, seed=1234, **FAST)
        self.assertEqual(to_json(a), to_json(b))
        c = build_answer(spec(), self.events, self.benchmark, self.calendar,
                         policy=fx.POLICY, seed=99, **FAST)
        self.assertNotEqual(to_json(a), to_json(c))

    def test_shrinkage_gated_on_family_size(self):
        self.assertIsNone(self.answer.shrinkage)
        self.assertIn("at least 5", self.answer.shrinkage_reason)
        self.assertTrue(any("at least 5" in w for w in self.answer.warnings))
        for result in self.answer.horizons:
            self.assertEqual(result.mean_car_shrunk, result.mean_car)

    def test_policy_return_is_net(self):
        summary = self.answer.horizons[0].policy
        self.assertIsInstance(summary, PolicySummary)
        self.assertNotEqual(summary.net, summary.gross)
        self.assertLess(summary.net, summary.gross)
        levels = [bps for bps, _ in summary.net_by_slippage_bps]
        self.assertEqual(levels, [10.0, 25.0, 50.0])
        values = [v for _, v in summary.net_by_slippage_bps]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(summary.net, values[0])
        self.assertEqual(self.answer.cost_model.baseline_slippage_bps, 10.0)
        self.assertEqual(dict(self.answer.cost_model.half_spread_bps_by_decile)[1],
                         45.0)

    def test_policy_net_carries_a_lower_90_bound(self):
        """Spec L §6.6 sizes from this interval and no other (Spec N §5.3).

        The level is 0.90 and not `CONFIDENCE_LEVEL`'s 0.95 on purpose: the
        sizing rule names the lower **90%** bound, and `portfolio/evidence.py`
        refuses an interval published at any other level rather than
        relabelling it. The interval must bracket its own point estimate and
        that estimate must be the policy net itself — not the headline, which
        is a different quantity under a different exit rule.
        """
        for result in self.answer.horizons:
            with self.subTest(horizon=result.horizon_sessions):
                interval = result.policy.net_ci
                self.assertIsNotNone(interval, "a full answer's policy leg needs its interval")
                self.assertEqual(interval.level, config.POLICY_CONFIDENCE_LEVEL)
                self.assertEqual(interval.level, 0.90)
                self.assertEqual(interval.method, "stationary_block_bootstrap")
                self.assertAlmostEqual(interval.estimate, result.policy.net, places=12)
                self.assertLessEqual(interval.lower, interval.estimate)
                self.assertLessEqual(interval.estimate, interval.upper)
                # A different quantity, measured differently: they must not be
                # the same object or the same numbers by accident.
                self.assertNotEqual(interval.lower, result.headline.lower)

    def test_policy_interval_is_seeded_like_everything_else(self):
        """Same seed, same bound. A sizing input that wobbled per call is not one."""
        again = build_answer(spec(), self.events, self.benchmark, self.calendar,
                             policy=fx.POLICY, **FAST)
        self.assertEqual(
            [h.policy.net_ci.lower for h in self.answer.horizons],
            [h.policy.net_ci.lower for h in again.horizons],
        )

    def test_policy_interval_round_trips_through_to_json(self):
        """`to_json` stays a plain dict, and the new leg is in it."""
        import json

        body = json.loads(to_json(self.answer))
        leg = body["horizons"][0]["policy"]["net_ci"]
        self.assertEqual(
            set(leg),
            {"estimate", "lower", "upper", "level", "method", "block_length",
             "reps", "seed"},
        )
        # `_plain` renders floats as `repr` strings for byte-determinism, so a
        # reader coerces. The evidence gate does exactly this.
        self.assertEqual(float(leg["level"]), 0.90)
        self.assertAlmostEqual(
            float(leg["lower"]), self.answer.horizons[0].policy.net_ci.lower, places=12
        )


class ModelNumberTests(unittest.TestCase):
    def test_no_model_number_in_output(self):
        with self.assertRaises(ModelNumberError):
            cohort_answer_from_text(
                "the cohort returned about 4.1% over two weeks, n=37")
        with self.assertRaises(ModelNumberError):
            PolicySummary(
                policy_slug="event_swing_14cal_v1",
                net="about 1.7%",                      # type: ignore[arg-type]
                net_ci=None,
                gross=0.02,
                net_by_slippage_bps=(),
                stopped_out=None,                      # type: ignore[arg-type]
            )
        with self.assertRaises(ModelNumberError):
            CohortPrediction(
                setup_hash="abc", as_of=date(2024, 1, 2), horizon_sessions=5,
                point_estimate="roughly four percent",  # type: ignore[arg-type]
                realized_return=0.01, sign_correct=True, realized_percentile=61.0,
            )
        # The honest path still works.
        summary = PolicySummary("p", 0.017, None, 0.021, ((10.0, 0.017),), None)
        self.assertEqual(summary.net, 0.017)

    def test_the_policy_interval_is_a_required_field(self):
        """`net_ci` cannot be forgotten: §8 says no field is optional."""
        with self.assertRaises(TypeError):
            PolicySummary(                             # type: ignore[call-arg]
                policy_slug="p", net=0.017, gross=0.021,
                net_by_slippage_bps=(), stopped_out=None,  # type: ignore[arg-type]
            )


class PredictionRecordTests(unittest.TestCase):
    def test_mean_ci_not_scored_as_prediction_interval(self):
        fields = set(CohortPrediction.__dataclass_fields__)
        self.assertIn("sign_correct", fields)
        self.assertIn("realized_percentile", fields)
        for banned in ("coverage", "covered", "in_interval", "within_ci", "hit_ci"):
            self.assertFalse(any(banned in f for f in fields),
                             f"{banned!r} must not exist: the CI is on the cohort "
                             f"mean and is never scored as a predictive interval")
        record = CohortPrediction(
            setup_hash="deadbeef", as_of=date(2024, 3, 1), horizon_sessions=10,
            point_estimate=0.012, realized_return=0.031,
            sign_correct=True, realized_percentile=78.5,
        )
        self.assertTrue(record.sign_correct)
        self.assertEqual(record.realized_percentile, 78.5)


class DisagreementTests(unittest.TestCase):
    def test_methods_disagree_downgrades(self):
        """Positive mean CAR, negative calendar-time alpha -> `inconclusive`."""
        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=12,
            beta=2.0, effect_daily=-0.0005, market_drift=0.0012, idio_sd=0.004)
        answer = build_answer(spec(), events, benchmark, calendar,
                              policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, CohortAnswer)
        result = answer.horizons[0]
        self.assertGreater(result.clustered_car.estimate, 0.0)
        self.assertLess(result.headline.estimate, 0.0)
        self.assertEqual(answer.status, "inconclusive")
        self.assertIn("disagree in sign", answer.refusal_reason)
        # Both numbers are shown; neither is averaged away.
        self.assertIn(f"{result.headline.estimate:+.6f}", answer.refusal_reason)
        self.assertIn(f"{result.clustered_car.estimate:+.6f}", answer.refusal_reason)
        self.assertTrue(any("disagree in sign" in w for w in answer.warnings))


class StabilityTests(unittest.TestCase):
    def test_decay_split_reported(self):
        """A real early effect and none late sets `decayed=True`."""
        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=21,
            effect_daily=0.004, late_effect_daily=0.0, idio_sd=0.002)
        answer = build_answer(spec(), events, benchmark, calendar,
                              policy=fx.POLICY, **FAST)
        self.assertIsInstance(answer, CohortAnswer)
        horizon, stability = answer.stability[0]
        self.assertEqual(horizon, 5)
        self.assertTrue(stability.decayed)
        self.assertGreater(stability.early.estimate, 0.01)
        self.assertLess(abs(stability.late.estimate), stability.early.estimate / 2)
        self.assertEqual(stability.early.label, "early_half")
        self.assertEqual(stability.late.label, "late_half")
        self.assertEqual(stability.early.n + stability.late.n, len(events))
        self.assertTrue(stability.per_year)
        self.assertEqual(stability.method, "calendar_time_alpha_times_h")
        self.assertIn("late", stability.reason)

        # A stable cohort is not flagged.
        _, bench2, steady = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=21,
            effect_daily=0.004, late_effect_daily=0.004, idio_sd=0.002)
        steady_answer = build_answer(spec(), steady, bench2, calendar,
                                     policy=fx.POLICY, **FAST)
        self.assertFalse(steady_answer.stability[0][1].decayed)


if __name__ == "__main__":
    unittest.main()
