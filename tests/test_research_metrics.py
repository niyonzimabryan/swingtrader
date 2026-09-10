"""Spec M §6: Brier with its decomposition, calibration, and the small-n floors.

The floors are the point. A Brier score over four resolutions is not a weak
signal, it is noise wearing a decimal point, and printing it would make the
track record worse than having none.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from research_workspace import metrics, store
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive


class StubSettings:
    research_workspace_enabled = True
    research_brier_min_resolved = 10
    research_calibration_min_resolved = 40
    research_calibration_coarse_below = 100


class MetricsTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("research_metrics")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.settings = StubSettings()

    def resolved_thesis(self, probability, outcome, *, index=0, revise_to=None):
        thesis = store.create_thesis(
            self.session,
            ticker=f"T{index:03d}",
            title=f"thesis {index}",
            claim="c",
            bear_case="b",
        )
        store.set_probability(
            self.session,
            thesis,
            probability,
            resolution_at=utcnow_naive().date() + timedelta(days=30),
            observable="o",
        )
        store.add_invalidator(
            self.session, thesis, description="closes below $1",
            type="price_level", params={"operator": "below", "price": 1.0},
        )
        store.activate(self.session, thesis)
        if revise_to is not None:
            store.set_probability(
                self.session, thesis, revise_to, reason="revised after entry"
            )
        store.resolve(self.session, thesis, outcome=outcome)
        return thesis

    def resolve_many(self, pairs):
        return [
            self.resolved_thesis(p, o, index=i) for i, (p, o) in enumerate(pairs)
        ]


class BrierTests(MetricsTestCase):
    def test_calibration_insufficient_below_ten(self):
        """Fewer than ten resolved theses prints `insufficient`, not a score."""
        self.resolve_many([(0.7, "true")] * 9)
        result = metrics.brier(self.session, settings=self.settings)

        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n_resolved"], 9)
        self.assertEqual(result["floor"], 10)
        self.assertNotIn("brier_score", result)

        self.resolved_thesis(0.7, "true", index=99)
        scored = metrics.brier(self.session, settings=self.settings)
        self.assertEqual(scored["status"], "ok")
        self.assertIn("brier_score", scored)

    def test_the_decomposition_satisfies_murphys_identity(self):
        """BS = reliability - resolution + uncertainty, or the split is wrong."""
        self.resolve_many(
            [(0.7, "true"), (0.7, "true"), (0.7, "false"), (0.3, "false"),
             (0.3, "false"), (0.3, "true"), (0.9, "true"), (0.9, "true"),
             (0.5, "false"), (0.5, "true"), (0.2, "false"), (0.8, "true")]
        )
        result = metrics.brier(self.session, settings=self.settings)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(
            result["brier_score"],
            result["reliability"] - result["resolution"] + result["uncertainty"],
            places=3,
        )
        self.assertAlmostEqual(result["identity_residual"], 0.0, places=6)
        self.assertEqual(result["buckets_used"], ["<=40%", "40-60%", ">=60%"])

    def test_brier_uses_original_probability(self):
        """A probability revised after entry is scored on the original number."""
        # Ten theses stated at 0.9 that all resolve false: scored on 0.9 the
        # Brier is 0.81; scored on the revised 0.1 it would be 0.01.
        for index in range(10):
            self.resolved_thesis(0.9, "false", index=index, revise_to=0.1)

        result = metrics.brier(self.session, settings=self.settings)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scored_on"], "original_probability")
        self.assertAlmostEqual(result["brier_score"], 0.81, places=6)
        self.assertEqual(result["n_revised_probabilities"], 10)

        # And the revision itself is kept, not discarded.
        thesis = store.theses_for(self.session, "T000")[0]
        self.assertEqual(thesis.probability, 0.1)
        self.assertEqual(thesis.original_probability, 0.9)
        history = thesis.probability_history
        self.assertEqual([h["probability"] for h in history], [0.9, 0.1])
        self.assertEqual(history[1]["reason"], "revised after entry")

    def test_the_calibration_table_waits_for_forty(self):
        self.resolve_many([(0.7, "true")] * 12)
        table = metrics.calibration_table(self.session, settings=self.settings)
        self.assertEqual(table["status"], "insufficient")
        self.assertEqual(table["floor"], 40)
        self.assertNotIn("rows", table)

    def test_the_calibration_table_uses_three_coarse_buckets_below_a_hundred(self):
        pairs = []
        for i in range(45):
            pairs.append((0.7, "true" if i % 3 else "false"))
        self.resolve_many(pairs)
        table = metrics.calibration_table(self.session, settings=self.settings)

        self.assertEqual(table["status"], "ok")
        self.assertEqual([row["bucket"] for row in table["rows"]],
                         ["<=40%", "40-60%", ">=60%"])
        populated = [row for row in table["rows"] if row["n"]]
        self.assertEqual(len(populated), 1)
        self.assertEqual(populated[0]["n"], 45)


class HonestyMetricsTests(MetricsTestCase):
    def test_the_unflattering_numbers_are_reported(self):
        # Two invalidated, one quietly abandoned.
        for index in range(3):
            thesis = store.create_thesis(
                self.session, ticker=f"T{index}", title=f"t{index}",
                claim="c", bear_case="b",
            )
            store.set_probability(
                self.session, thesis, 0.6,
                resolution_at=utcnow_naive().date() + timedelta(days=10),
            )
            store.add_invalidator(
                self.session, thesis, description="closes below $1",
                type="price_level", params={"operator": "below", "price": 1.0},
            )
            store.activate(self.session, thesis)
            thesis.activated_at = utcnow_naive() - timedelta(days=40)
            if index < 2:
                store.review(self.session, thesis, verdict="invalidated")
            else:
                store.abandon(self.session, thesis)

        report = metrics.honesty_metrics(self.session, settings=self.settings)
        split = report["invalidated_versus_abandoned"]
        self.assertEqual(split["invalidated"], 2)
        self.assertEqual(split["abandoned"], 1)
        self.assertAlmostEqual(split["share_abandoned"], 0.3333, places=3)
        self.assertEqual(report["median_days_to_invalidation"]["status"], "ok")

    def test_small_n_prints_insufficient_rather_than_a_hit_rate(self):
        self.resolve_many([(0.7, "true"), (0.7, "false")])
        report = metrics.honesty_metrics(self.session, settings=self.settings)
        self.assertEqual(report["hit_rate_resolved"]["status"], "insufficient")
        self.assertEqual(report["journal_matched_exit_reason"]["status"], "insufficient")

    def test_the_journal_exit_reason_match_is_measured_once_recorded(self):
        entry = store.journal_append(
            self.session, decision="closed", tickers=["AMD"],
            note_md="Exited on the thesis breaking.",
        )
        store.record_outcome(
            self.session, entry, exit_reason="stop", matched_reason=False,
            note="stopped out on a market drawdown, not the thesis",
        )
        report = metrics.honesty_metrics(self.session, settings=self.settings)
        matched = report["journal_matched_exit_reason"]
        self.assertEqual(matched["status"], "ok")
        self.assertEqual(matched["n"], 1)
        self.assertEqual(matched["share_matching"], 0.0)

    def test_the_quarterly_report_renders_every_section(self):
        report = metrics.quarterly_report(self.session, settings=self.settings)
        self.assertEqual(set(report), {"honesty", "brier", "calibration"})
        self.assertEqual(report["brier"]["status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
