"""Balance diagnostics (Spec N §4.5) — the right tool for each question."""

from __future__ import annotations

import unittest

from comparables import config
from comparables.balance import (
    QueryCohortBalance,
    balance_block,
    matched_vs_pool,
    query_vs_cohort,
)


class QueryVersusCohortTests(unittest.TestCase):
    def test_query_vs_cohort_uses_distance(self):
        cohort = [4.0, 5.0, 6.0, 5.0, 5.0]     # mean 5.0, sd 0.7071
        typical = query_vs_cohort("liquidity_decile", 5.0, cohort)
        self.assertAlmostEqual(typical.standardized_distance, 0.0, places=12)
        self.assertEqual(typical.label, "typical")
        self.assertAlmostEqual(typical.percentile, 50.0, places=9)

        warned = query_vs_cohort("liquidity_decile", 6.0, cohort)
        self.assertAlmostEqual(warned.standardized_distance, 1.4142135623730951)
        self.assertEqual(warned.label, "warn")

        atypical = query_vs_cohort("liquidity_decile", 8.0, cohort)
        self.assertGreater(atypical.standardized_distance,
                           config.QUERY_DISTANCE_ATYPICAL)
        self.assertEqual(atypical.label, "atypical")
        self.assertAlmostEqual(atypical.percentile, 100.0, places=9)

        # A single event has no variance, so an SMD does not apply and the
        # record does not carry one.
        for record in (typical, warned, atypical):
            self.assertIsInstance(record, QueryCohortBalance)
            self.assertFalse(hasattr(record, "smd"))
            self.assertFalse(hasattr(record, "variance_ratio"))
            self.assertEqual(record.method, "standardized_distance")
            self.assertTrue(hasattr(record, "percentile"))

    def test_smd_thresholds(self):
        """An SMD of 0.15 warns; 0.30 labels `unbalanced`; a variance ratio of 3
        warns (Spec N §10)."""
        import numpy as np

        pool = list(np.random.default_rng(1).normal(0.0, 1.0, 4000))

        def matched_with_smd(target):
            # Both groups have unit variance, so the pooled SD is 1 and the SMD
            # is just the mean shift.
            shifted = list(np.random.default_rng(2).normal(target, 1.0, 4000))
            return matched_vs_pool("liquidity_decile", shifted, pool)

        balanced = matched_with_smd(0.0)
        self.assertLess(abs(balanced.smd), config.SMD_WARN)
        self.assertEqual(balanced.label, "balanced")

        warned = matched_with_smd(0.15)
        self.assertGreater(abs(warned.smd), config.SMD_WARN)
        self.assertLess(abs(warned.smd), config.SMD_UNBALANCED)
        self.assertEqual(warned.label, "warn")

        unbalanced = matched_with_smd(0.30)
        self.assertGreater(abs(unbalanced.smd), config.SMD_UNBALANCED)
        self.assertEqual(unbalanced.label, "unbalanced")

        # A variance ratio of 3 warns on its own, with the SMD at zero.
        wide = list(np.random.default_rng(3).normal(0.0, 3.0 ** 0.5, 8000))
        spread = matched_vs_pool("liquidity_decile", wide, pool)
        self.assertLess(abs(spread.smd), config.SMD_WARN)
        self.assertGreater(spread.variance_ratio, config.VARIANCE_RATIO_BOUNDS[1])
        self.assertEqual(spread.label, "warn")

    def test_matched_vs_pool_uses_smd_and_variance_ratio(self):
        pool = [float(i % 10 + 1) for i in range(200)]
        balanced = matched_vs_pool("liquidity_decile", pool[:40], pool)
        self.assertLess(abs(balanced.smd), config.SMD_WARN)
        self.assertEqual(balanced.label, "balanced")

        skewed = matched_vs_pool("liquidity_decile", [9.0, 10.0] * 20, pool)
        self.assertGreater(abs(skewed.smd), config.SMD_UNBALANCED)
        self.assertEqual(skewed.label, "unbalanced")
        self.assertLess(skewed.variance_ratio, config.VARIANCE_RATIO_BOUNDS[0])

    def test_sector_is_marked_current_vintage(self):
        record = matched_vs_pool("sector", [1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(record.vintage, "current")
        other = matched_vs_pool("liquidity_decile", [1.0, 2.0], [1.0, 2.0, 3.0])
        self.assertEqual(other.vintage, "point_in_time")

    def test_balance_block_labels_both_diagnostics(self):
        cohort = [{"liquidity_decile": v} for v in (9.0, 9.0, 10.0, 10.0)]
        pool = [{"liquidity_decile": float(i % 10 + 1)} for i in range(100)]
        block = balance_block(["liquidity_decile"], {"liquidity_decile": 2.0},
                             cohort, pool)
        self.assertEqual(len(block.query_vs_cohort), 1)
        self.assertEqual(len(block.matched_vs_pool), 1)
        self.assertEqual(block.query_vs_cohort[0].label, "atypical")
        self.assertEqual(block.matched_vs_pool[0].label, "unbalanced")
        self.assertTrue(any("atypical" in w for w in block.warnings))
        self.assertTrue(any("unbalanced" in w for w in block.warnings))


if __name__ == "__main__":
    unittest.main()
