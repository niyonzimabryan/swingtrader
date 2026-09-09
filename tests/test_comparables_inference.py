"""Inference (Spec N §6): block bootstrap, clustering, Wilson, nulls, shrinkage."""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

import numpy as np

from comparables import config, fixtures as fx
from comparables.inference import (
    CohortMoments,
    effective_sample_size,
    iid_bootstrap_ci,
    method_of_moments_shrinkage,
    optimal_block_length_for,
    romano_wolf_stepm,
    shrinkage_for_family,
    sidak_adjusted,
    stationary_bootstrap_ci,
    two_way_clustered_car,
    wilson_interval,
)
from comparables.report import run_null_tests

SEED = 20260908


class WilsonTests(unittest.TestCase):
    def test_wilson_not_normal(self):
        """A 3-of-10 hit rate reports Wilson; no normal interval exists."""
        interval = wilson_interval(3, 10)
        self.assertEqual(interval.method, "wilson")
        self.assertAlmostEqual(interval.point, 0.3, places=12)
        self.assertAlmostEqual(interval.lower, 0.10779126740630099, places=12)
        self.assertAlmostEqual(interval.upper, 0.6032218525388546, places=12)

        z = 1.959963984540054
        normal_half = z * math.sqrt(0.3 * 0.7 / 10)
        self.assertNotAlmostEqual(interval.lower, 0.3 - normal_half, places=3)
        self.assertNotAlmostEqual(interval.upper, 0.3 + normal_half, places=3)

        # The schema has no place to put a normal-approximation interval.
        names = set(interval.__dataclass_fields__)
        self.assertFalse(any("normal" in n for n in names))
        self.assertEqual(names,
                         {"successes", "n", "point", "lower", "upper", "level", "method"})

        # And it stays inside [0, 1] exactly where the normal one does not.
        extreme = wilson_interval(10, 10)
        self.assertLessEqual(extreme.upper, 1.0)
        self.assertGreater(extreme.lower, 0.0)
        self.assertGreater(wilson_interval(0, 10).upper, 0.0)


class BlockBootstrapTests(unittest.TestCase):
    def test_block_length_estimated_and_printed(self):
        rng = np.random.default_rng(4)
        noise = rng.normal(0, 0.01, 400)
        series = [float(noise[i:i + 10].sum()) for i in range(len(noise) - 10)]

        block = optimal_block_length_for(series, horizon=10)
        self.assertEqual(block.method, "politis_white_ppw_stationary")
        self.assertGreater(block.estimated, 1.0)
        self.assertGreaterEqual(block.used, 10, "the horizon is a hard floor")
        self.assertEqual(block.used, max(math.ceil(block.estimated), 10))

        # A short horizon must not shrink the block below what arch estimated.
        short = optimal_block_length_for(series, horizon=1)
        self.assertEqual(short.used, max(math.ceil(short.estimated), 1))
        self.assertEqual(short.estimated, block.estimated)

        # A long horizon raises it.
        long = optimal_block_length_for(series, horizon=40)
        self.assertGreaterEqual(long.used, 40)

        ci = stationary_bootstrap_ci(series, block.used, reps=400, seed=SEED)
        self.assertEqual(ci.block_length, block.used)
        self.assertEqual(ci.method, "stationary_block_bootstrap")
        self.assertEqual(ci.seed, SEED)
        self.assertLess(ci.lower, ci.upper)

    def test_overlap_widens_ci(self):
        """Overlapping windows induce serial dependence the naive CI ignores.

        Ten-session CARs starting on consecutive sessions share nine of their
        ten daily returns, so the series of event CARs is strongly
        autocorrelated by construction. The stationary block bootstrap with the
        horizon as a block floor resamples that dependence; an iid resample of
        the same numbers pretends it away.
        """
        rng = np.random.default_rng(17)
        daily = rng.normal(0.0003, 0.01, 600)
        horizon = 10
        overlapping = [float(daily[i:i + horizon].sum())
                       for i in range(len(daily) - horizon)]

        block = optimal_block_length_for(overlapping, horizon)
        blocked = stationary_bootstrap_ci(overlapping, block.used, reps=2000, seed=SEED)
        naive = iid_bootstrap_ci(overlapping, reps=2000, seed=SEED)

        self.assertAlmostEqual(blocked.estimate, naive.estimate, places=12)
        self.assertGreater(blocked.width, naive.width * 1.5,
                           "the block bootstrap must be materially wider")

        # Non-overlapping windows over the same data are far closer.
        disjoint = [float(daily[i:i + horizon].sum())
                    for i in range(0, len(daily) - horizon, horizon)]
        d_block = optimal_block_length_for(disjoint, horizon)
        d_blocked = stationary_bootstrap_ci(disjoint, d_block.used, reps=2000, seed=SEED)
        d_naive = iid_bootstrap_ci(disjoint, reps=2000, seed=SEED)
        self.assertLess(d_blocked.width / d_naive.width, blocked.width / naive.width)


class EffectiveSampleSizeTests(unittest.TestCase):
    def _dates(self, n_dates: int, per_date: int) -> list[date]:
        base = date(2024, 1, 2)
        return [base + timedelta(days=7 * d) for d in range(n_dates) for _ in range(per_date)]

    def test_n_eff_reported(self):
        """`n_eff` falls as within-date correlation rises, `n_matured` fixed."""
        n_dates, per_date = 10, 3
        dates = self._dates(n_dates, per_date)
        rng = np.random.default_rng(2)

        results = []
        for share in (0.0, 0.5, 0.95):
            values = []
            for d in range(n_dates):
                common = rng.normal(0, 1)
                for _ in range(per_date):
                    idio = rng.normal(0, 1)
                    values.append(math.sqrt(share) * common
                                  + math.sqrt(1 - share) * idio)
            results.append(effective_sample_size(values, dates))

        for r in results:
            self.assertEqual(r.n, 30)
            self.assertEqual(r.n_distinct_dates, 10)
            self.assertAlmostEqual(r.mean_cluster_size, 3.0, places=12)
            self.assertLessEqual(r.n_eff, 30.0)
            self.assertGreaterEqual(r.n_eff, 10.0)

        self.assertGreater(results[0].n_eff, results[1].n_eff)
        self.assertGreater(results[1].n_eff, results[2].n_eff)
        self.assertGreater(results[2].icc_used, results[0].icc_used)

        # The two limits: one date is one observation, all-distinct dates is n.
        one_date = effective_sample_size([1.0, 2.0, 3.0], [date(2024, 1, 2)] * 3)
        self.assertAlmostEqual(one_date.n_eff, 1.0, places=12)
        all_distinct = effective_sample_size(
            [1.0, 2.0, 3.0],
            [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
        self.assertAlmostEqual(all_distinct.n_eff, 3.0, places=12)


class ClusteredStandardErrorTests(unittest.TestCase):
    def test_clustered_se_is_labelled_unreliable_below_thirty_clusters(self):
        rng = np.random.default_rng(9)
        cars = list(rng.normal(0.01, 0.05, 40))
        dates = [date(2024, 1, 2) + timedelta(days=d) for d in range(20) for _ in range(2)]
        tickers = [f"T{i:03d}" for i in range(40)]

        few = two_way_clustered_car(cars, dates, tickers)
        self.assertEqual(few.n_clusters_date, 20)
        self.assertFalse(few.reliable)
        self.assertIn("bootstrap CI is the only interval", few.reliability_reason)

        many_dates = [date(2024, 1, 2) + timedelta(days=d) for d in range(40)]
        many = two_way_clustered_car(cars, many_dates, tickers)
        self.assertEqual(many.n_clusters_date, 40)
        self.assertTrue(many.reliable)
        self.assertAlmostEqual(many.estimate, float(np.mean(cars)), places=12)


class NullTestTests(unittest.TestCase):
    def _placebo(self, seed, effect):
        from comparables.outcomes import calendar_time_alpha, calendar_time_series

        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=seed, effect_daily=effect)
        observed = calendar_time_alpha(
            calendar_time_series(events, benchmark, calendar, 10), 10).alpha_times_h
        nulls = run_null_tests(events, benchmark, calendar, 10, observed,
                               pool=events, draws=200, seed=SEED)
        return nulls.placebo_dates

    def test_placebo_null_effect(self):
        """Random-date placebo on a cohort with no effect finds nothing.

        Asserted over several draws of the synthetic world, because a single
        cohort landing at p = 0.06 by luck is the thing a null test is supposed
        to be able to do.
        """
        null_placebos = [self._placebo(seed, 0.0) for seed in range(31, 34)]
        for placebo in null_placebos:
            self.assertEqual(placebo.n_draws, 200)
            self.assertLess(abs(placebo.estimate), 0.01,
                            "the placebo mean effect should sit on zero")
            self.assertLess(placebo.lower, 0.0)
            self.assertGreater(placebo.upper, 0.0)
        median_p = float(np.median([p.p_value for p in null_placebos]))
        self.assertGreater(median_p, 0.10,
                           "a no-effect cohort must not stand out from its placebo")

        # A real effect does move out of the placebo band, every time.
        strong = [self._placebo(seed, 0.004) for seed in range(31, 34)]
        for placebo in strong:
            self.assertLess(placebo.p_value, 0.05)
            self.assertLess(abs(placebo.estimate), 0.01)
        self.assertLess(float(np.median([p.p_value for p in strong])), median_p)

    def test_pre_event_window_is_reported(self):
        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=31, effect_daily=0.0)
        nulls = run_null_tests(events, benchmark, calendar, 10, 0.0,
                               pool=events, draws=50, seed=SEED)
        self.assertEqual(nulls.pre_event_window.name, "pre_event_window")
        self.assertEqual(nulls.pre_event_window.n_draws, 40)
        self.assertIn("leakage", nulls.pre_event_window.note)


class ShrinkageTests(unittest.TestCase):
    def _family(self, n_cohorts, n_per, tau, sigma, seed):
        rng = np.random.default_rng(seed)
        out = []
        for i in range(n_cohorts):
            theta = rng.normal(0.0, tau) if tau > 0 else 0.0
            draws = rng.normal(theta, sigma, n_per)
            out.append(CohortMoments(f"c{i}", n_per, float(draws.mean()),
                                     float(draws.var(ddof=1))))
        return out

    def test_shrinkage_gated_on_family_size(self):
        four = self._family(4, 60, 0.02, 0.10, 1)
        spec, reason = shrinkage_for_family("liquid_us_equity_v1:sue_seasonal:>", four)
        self.assertIsNone(spec)
        self.assertIn("at least 5", reason)
        self.assertIn("has 4", reason)
        self.assertIn("§6.4", reason)

        five = self._family(5, 60, 0.02, 0.10, 1)
        spec5, reason5 = shrinkage_for_family("liquid_us_equity_v1:sue_seasonal:>", five)
        self.assertIsNotNone(spec5)
        self.assertEqual(reason5, "")
        self.assertEqual(spec5.n_cohorts, 5)

        self.assertEqual(config.SHRINKAGE_MIN_FAMILY_COHORTS, 5)

    def test_shrinkage_k_method_of_moments(self):
        """A simulation with known tau^2 recovers k; tau^2 <= 0 shrinks fully."""
        tau, sigma, n_per = 0.02, 0.10, 50
        k_true = sigma ** 2 / tau ** 2                       # 25.0
        estimates = []
        for seed in range(20):
            family = self._family(60, n_per, tau, sigma, seed)
            spec = method_of_moments_shrinkage("fam", family)
            self.assertFalse(spec.full_shrinkage)
            estimates.append(spec.k)
        median = float(np.median(estimates))
        self.assertGreater(median, k_true * 0.6)
        self.assertLess(median, k_true * 1.6)

        # tau^2 <= 0: the cohorts differ no more than noise predicts, and the
        # honest output is the family mean, printed as such.
        identical = [CohortMoments(f"c{i}", n_per, 0.03, sigma ** 2)
                     for i in range(8)]
        spec = method_of_moments_shrinkage("fam", identical)
        self.assertTrue(spec.full_shrinkage)
        self.assertEqual(spec.k, float("inf"))
        self.assertLessEqual(spec.tau2, 0.0)
        self.assertIn("indistinguishable", spec.note)
        self.assertAlmostEqual(spec.shrink(0.5, 50), spec.pooled, places=12)

        # Simulated with no between-cohort variation at all, k comes out huge
        # even when tau-hat squared lands just above zero.
        flat = self._family(40, n_per, 0.0, sigma, 7)
        self.assertGreater(method_of_moments_shrinkage("fam", flat).k, k_true * 4)

        # Shrinkage pulls a small cohort further than a large one.
        family = self._family(60, n_per, tau, sigma, 3)
        spec = method_of_moments_shrinkage("fam", family)
        small = spec.shrink(0.05, 5)
        large = spec.shrink(0.05, 5000)
        self.assertLess(abs(small - spec.pooled), abs(large - spec.pooled))

class MultiplicityTests(unittest.TestCase):
    """Romano-Wolf stepdown over a family (Spec N §7), with Sidak as fallback."""

    def _family(self, seed=5, n=250, real_effect=0.0025):
        rng = np.random.default_rng(seed)
        family = {f"noise{i}": list(rng.normal(0.0, 0.01, n)) for i in range(5)}
        family["real"] = list(rng.normal(real_effect, 0.01, n))
        return family

    def test_stepm_rejects_only_the_real_variant(self):
        family = self._family()
        rejected, note = romano_wolf_stepm(family, "real", seed=1, reps=500)
        self.assertTrue(rejected)
        self.assertIn("StepM over 6 family members", note)

        for name in ("noise0", "noise3"):
            rejected, _ = romano_wolf_stepm(family, name, seed=1, reps=500)
            self.assertFalse(rejected, f"{name} must not survive the stepdown")

        # An all-noise family rejects nothing, which is the point of the control.
        rng = np.random.default_rng(77)
        all_noise = {f"n{i}": list(rng.normal(0.0, 0.01, 250)) for i in range(6)}
        for name in all_noise:
            self.assertFalse(romano_wolf_stepm(all_noise, name, seed=1, reps=500)[0])

    def test_stepm_declines_rather_than_guessing_on_a_short_series(self):
        family = {"a": [0.01, 0.02], "b": [0.0, 0.01]}
        rejected, note = romano_wolf_stepm(family, "a", seed=1, reps=100)
        self.assertFalse(rejected)
        self.assertIn("StepM skipped", note)

        ragged = {"a": [0.0] * 10, "b": [0.0] * 9}
        self.assertIn("different series lengths",
                      romano_wolf_stepm(ragged, "a", seed=1, reps=100)[1])

    def test_sidak_fallback(self):
        self.assertAlmostEqual(sidak_adjusted(0.05, 1), 0.05, places=12)
        self.assertGreater(sidak_adjusted(0.05, 12), 0.4)
        self.assertLessEqual(sidak_adjusted(0.9, 12), 1.0)


if __name__ == "__main__":
    unittest.main()
