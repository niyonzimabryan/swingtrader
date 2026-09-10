"""The query log, the answer cache, the citation seam and the track record.

Spec N §6.5, §7 and §8, as stored rows rather than in-process dictionaries.
Phase 3b tested the in-memory `TrialRegistry`; this tests the thing that
actually survives a restart.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta

from tests import cohortfixture as cf
from tests.dbfixture import TestDatabase

from comparables import citations, registry
from comparables import query as query_mod
from comparables import report as report_mod
from comparables import setups as roster
from comparables.setup_spec import Condition, SetupSpec


def spec(slug="probe_v1", *, fact="sue_seasonal", op=">", value=1.5,
         universe="liquid_us_equity_v1") -> SetupSpec:
    return SetupSpec(
        slug=slug,
        version="1.0.0",
        conditions=(Condition(fact, op, value),),
        universe=universe,
        horizons_sessions=(5, 10),
        execution_policy="event_swing_14cal_v1",
        match_covariates=("liquidity_decile",),
        lookback_years=5,
    )


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("registry")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

    def log(self, s, **kwargs):
        defaults = dict(
            requester_label="claude-code", as_of=date(2024, 1, 2), depth="quick",
            status="ok", evidence_tier="vendor_pit", result={"n": 1},
        )
        defaults.update(kwargs)
        return registry.record_query(self.session, s, **defaults)


# --------------------------------------------------------------------------- #
# Trial accounting (§7)
# --------------------------------------------------------------------------- #


class TrialCountTests(RegistryTestCase):
    def test_trial_count_increments(self):
        """The twelfth variant reports `trials_against_this_pattern=12`.

        Counted after the row is written, so the query being answered is
        included — "how many variants have been tried against this fact
        pattern" includes this one, or the first query would report zero.
        """
        for i in range(1, 13):
            record = self.log(spec(value=1.0 + i / 10.0))
            self.assertEqual(record.trials_against_this_pattern, i)

        self.assertEqual(
            registry.trials_for_family(self.session, spec().family_slug), 12
        )

    def test_re_running_the_same_spec_is_a_query_not_a_variant(self):
        for _ in range(5):
            record = self.log(spec())
        self.assertEqual(record.trials_against_this_pattern, 1)
        self.assertEqual(registry.queries_for_family(self.session, spec().family_slug), 5)

    def test_trial_count_keyed_by_family(self):
        """Renaming a setup inside the same family does not reset the count."""
        self.log(spec("first_v1", value=1.5))
        record = self.log(spec("totally_different_name_v9", value=2.5))
        self.assertEqual(record.trials_against_this_pattern, 2)

        other_family = self.log(spec("other_v1", fact="gap_pct"))
        self.assertEqual(other_family.trials_against_this_pattern, 1)

    def test_a_refused_query_is_logged_like_any_other(self):
        """A search that stopped counting when it stopped succeeding would be
        no accounting at all."""
        self.log(spec(), status="insufficient", refusal_reason="below the floor")
        self.assertEqual(registry.trials_for_family(self.session, spec().family_slug), 1)
        row = registry.queries_for_setup(self.session, spec().content_hash)[0]
        self.assertEqual(row.status, "insufficient")
        self.assertEqual(row.refusal_reason, "below the floor")

    def test_the_stored_registry_survives_a_new_object(self):
        self.log(spec(value=1.5))
        self.log(spec(value=2.5))
        fresh = registry.StoredTrialRegistry(self.session)
        self.assertEqual(fresh.record(spec(value=3.5)), 3)

    def test_the_log_stores_the_label_never_the_token(self):
        record = self.log(spec(), requester_label="claude-code")
        row = registry.query(self.session, record.query_id)
        self.assertEqual(row.requester_label, "claude-code")
        self.assertNotIn("token", {c.name for c in row.__table__.columns})


# --------------------------------------------------------------------------- #
# The answer cache (§4.0)
# --------------------------------------------------------------------------- #


class AnswerCacheTests(RegistryTestCase):
    def store(self, s, **kwargs):
        defaults = dict(
            as_of=date(2024, 1, 2), price_snapshot_id=7, depth="full",
            status="ok", evidence_tier="vendor_pit",
            answer_json='{"a":1}', provenance_mix={"vendor_pit": 30},
        )
        defaults.update(kwargs)
        return registry.store_answer(self.session, s, **defaults)

    def test_the_same_question_and_vintage_resolves_to_one_row(self):
        first = self.store(spec())
        second = self.store(spec())
        self.assertEqual(first.id, second.id)

    def test_a_different_vintage_is_a_different_answer(self):
        a = self.store(spec())
        b = self.store(spec(), price_snapshot_id=8)
        c = self.store(spec(), as_of=date(2024, 6, 3))
        self.assertEqual(len({a.id, b.id, c.id}), 3)

    def test_quick_and_full_are_different_answers_to_the_same_question(self):
        """Only one of them is citable, so a cache that conflated them would
        hand a caller an answer the type system says they may not cite."""
        full = self.store(spec(), depth="full")
        quick = self.store(spec(), depth="quick")
        self.assertNotEqual(full.id, quick.id)

    def test_no_snapshot_is_a_key_not_a_null(self):
        a = self.store(spec(), price_snapshot_id=None)
        b = self.store(spec(), price_snapshot_id=None)
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.price_snapshot_id, registry.NO_SNAPSHOT)

    def test_the_archival_block_is_stored_beside_the_answer_not_inside_it(self):
        row = self.store(spec(), archival_block_json='{"b":2}')
        self.assertEqual(row.answer, {"a": 1})
        self.assertEqual(row.archival_block, {"b": 2})


# --------------------------------------------------------------------------- #
# The citation seam (§8)
# --------------------------------------------------------------------------- #


class CitationTests(RegistryTestCase):
    def stored(self, depth="full", status="ok"):
        record = self.log(spec(), depth=depth, status=status)
        return registry.store_answer(
            self.session, spec(), as_of=date(2024, 1, 2), price_snapshot_id=7,
            depth=depth, status=status, evidence_tier="vendor_pit",
            answer_json=json.dumps({"refusal_reason": "below the floor"}),
            provenance_mix={"vendor_pit": 30}, query_id=record.query_id,
        )

    def test_a_full_answer_resolves(self):
        row = self.stored()
        resolved = citations.resolve(self.session, citations.citation_id(row.id))
        self.assertEqual(resolved.answer_id, row.id)
        self.assertTrue(resolved.citable)
        self.assertEqual(resolved.setup_hash, spec().content_hash)

    def test_quick_answer_not_citable(self):
        row = self.stored(depth="quick")
        with self.assertRaises(citations.NotCitable) as caught:
            citations.resolve(self.session, citations.citation_id(row.id))
        self.assertIn("depth='quick'", str(caught.exception))
        self.assertIn("full", str(caught.exception))
        # Displayable, but never quotable.
        shown = citations.resolve(
            self.session, citations.citation_id(row.id), require_citable=False
        )
        self.assertFalse(shown.citable)

    def test_an_insufficient_answer_carries_no_statistic_to_cite(self):
        row = self.stored(status="insufficient")
        with self.assertRaises(citations.NotCitable) as caught:
            citations.resolve(self.session, citations.citation_id(row.id))
        self.assertIn("below the floor", str(caught.exception))
        self.assertIn("refusal", str(caught.exception))

    def test_an_unknown_citation_says_so(self):
        with self.assertRaises(citations.CitationError):
            citations.resolve(self.session, "cohort:9999")
        with self.assertRaises(citations.CitationError):
            citations.resolve(self.session, "not-a-citation")

    def test_a_bare_integer_resolves_too(self):
        row = self.stored()
        self.assertEqual(
            citations.resolve(self.session, row.id).answer_id, row.id
        )

    def test_the_citation_payload_is_plain_copyable_data(self):
        row = self.stored()
        resolved = citations.resolve(self.session, row.id)
        payload = citations.citation_payload(resolved)
        json.dumps(payload)
        self.assertEqual(payload["citation_id"], f"cohort:{row.id}")
        self.assertEqual(payload["depth"], "full")


# --------------------------------------------------------------------------- #
# The engine's own track record (§6.5)
# --------------------------------------------------------------------------- #


class PredictionTests(RegistryTestCase):
    def rows(self, **kwargs):
        defaults = dict(
            cohort_answer_id=1, spec=spec(), as_of=date(2024, 1, 2),
            query_ticker="AAA",
            horizons=[{
                "horizon_sessions": 10,
                "point_estimate": 0.02,
                "ci_low": 0.005,
                "ci_high": 0.035,
                "cohort_outcomes": [-0.10, -0.02, 0.01, 0.03, 0.09],
                "matures_on": date(2024, 1, 17),
            }],
        )
        defaults.update(kwargs)
        return registry.record_predictions(self.session, **defaults)

    def test_mean_ci_not_scored_as_prediction_interval(self):
        """`cohort_predictions` carries sign and percentile fields and no
        coverage field — asserted against the table, not the docstring.

        The CI is on the cohort *mean*. One trade landing outside the interval
        for the average of a hundred trades says nothing, and advertising
        "coverage" against it would manufacture a failure out of a category
        error. There is nowhere to store one.
        """
        columns = set(registry.prediction_columns())
        self.assertIn("sign_correct", columns)
        self.assertIn("realized_percentile", columns)
        for banned in registry.FORBIDDEN_PREDICTION_FIELDS:
            self.assertNotIn(banned, columns)
        for column in columns:
            self.assertNotIn("coverage", column)

    def test_engine_predictions_scored(self):
        """A matured cited answer produces a scored row: sign and percentile."""
        (row,) = self.rows()
        self.assertIsNone(row.realized_return)
        self.assertIsNone(row.scored_at)

        due = registry.due_predictions(self.session, date(2024, 1, 20))
        self.assertEqual([r.id for r in due], [row.id])

        registry.score_prediction(self.session, row, 0.04)
        self.assertAlmostEqual(row.realized_return, 0.04)
        self.assertTrue(row.sign_correct)
        # 0.04 sits above four of five stored outcomes.
        self.assertAlmostEqual(row.realized_percentile, 80.0)
        self.assertIsNotNone(row.scored_at)

        self.assertEqual(registry.due_predictions(self.session, date(2024, 1, 20)), ())

    def test_a_wrong_sign_is_recorded_as_wrong(self):
        (row,) = self.rows()
        registry.score_prediction(self.session, row, -0.06)
        self.assertFalse(row.sign_correct)
        self.assertAlmostEqual(row.realized_percentile, 20.0)

    def test_a_zero_point_estimate_predicts_no_direction(self):
        """`None`, not `False`: no sign can be right or wrong about zero."""
        (row,) = self.rows(horizons=[{
            "horizon_sessions": 10, "point_estimate": 0.0,
            "ci_low": -0.01, "ci_high": 0.01,
            "cohort_outcomes": [-0.01, 0.0, 0.01],
            "matures_on": date(2024, 1, 17),
        }])
        registry.score_prediction(self.session, row, 0.02)
        self.assertIsNone(row.sign_correct)

    def test_the_track_record_is_gated_on_twenty_matured_predictions(self):
        for i in range(3):
            (row,) = self.rows(cohort_answer_id=100 + i)
            registry.score_prediction(self.session, row, 0.03)
        record = registry.track_record(self.session)
        self.assertEqual(record.n_scored, 3)
        self.assertFalse(record.reportable)
        self.assertIn("floor of 20", record.reason)
        self.assertAlmostEqual(record.sign_hit_rate, 1.0)

    def test_recording_the_same_answer_twice_updates_rather_than_duplicates(self):
        first = self.rows()
        second = self.rows()
        self.assertEqual(first[0].id, second[0].id)

    def test_percentile_of_an_empty_distribution_raises(self):
        with self.assertRaises(registry.RegistryError):
            registry.percentile_of(0.0, [])


# --------------------------------------------------------------------------- #
# Shrinkage across a stored family (§6.4)
# --------------------------------------------------------------------------- #


class ShrinkageTests(RegistryTestCase):
    def store_sibling(self, i: int, *, mean: float, n: int = 40,
                      variance: float = 0.004):
        s = spec(f"sib_{i}_v1", value=1.0 + i)
        registry.store_answer(
            self.session, s, as_of=date(2024, 1, 2) + timedelta(days=i),
            price_snapshot_id=7, depth="full", status="ok",
            evidence_tier="vendor_pit", answer_json="{}",
            provenance_mix={"vendor_pit": n},
            family_moments=[
                {"horizon": 5, "n": n, "mean": mean / 2, "within_variance": variance},
                {"horizon": 10, "n": n, "mean": mean, "within_variance": variance},
            ],
        )
        return s

    def test_shrinkage_declared(self):
        """A family of five or more stored cohorts declares family, k and pooled.

        Below five, `shrinkage` is `null` **with the reason** — `τ̂²` is noise
        there and a shrunk number would be a fixed prior wearing a data-driven
        costume (§6.4). At five it is declared, and every part of it is named.
        """
        from comparables.inference import shrinkage_for_family

        family = spec().family_slug
        for i in range(4):
            self.store_sibling(i, mean=0.01 + 0.004 * i)
        four = registry.family_moments(self.session, family)
        self.assertEqual(len(four), 4)
        shrinkage, reason = shrinkage_for_family(family, four)
        self.assertIsNone(shrinkage)
        self.assertIn("at least 5 cohorts", reason)
        self.assertIn("data-driven costume", reason)

        self.store_sibling(4, mean=0.03)
        five = registry.family_moments(self.session, family)
        self.assertEqual(len(five), 5)
        shrinkage, reason = shrinkage_for_family(family, five)
        self.assertIsNotNone(shrinkage)
        self.assertEqual(shrinkage.family_slug, family)
        self.assertEqual(shrinkage.n_cohorts, 5)
        self.assertGreater(shrinkage.k, 0.0)
        self.assertEqual(reason, "")

    def test_the_family_pool_is_taken_at_one_horizon_per_cohort(self):
        """Mixing horizons inside a family would shrink a 5-session mean toward
        a 10-session one, which is not a prior, it is a units error."""
        family = spec().family_slug
        for i in range(5):
            self.store_sibling(i, mean=0.02 + 0.001 * i)
        moments = registry.family_moments(self.session, family)
        self.assertEqual(len(moments), 5)
        for moment in moments:
            # The longest horizon's mean, never the 5-session half of it.
            self.assertGreater(moment.mean, 0.015)

    def test_the_cohort_being_answered_is_excluded_from_its_own_family_pool(self):
        family = spec().family_slug
        mine = self.store_sibling(0, mean=0.02)
        for i in range(1, 5):
            self.store_sibling(i, mean=0.02 + 0.001 * i)
        moments = registry.family_moments(
            self.session, family, exclude_setup_hash=mine.content_hash
        )
        self.assertEqual(len(moments), 4)

    def test_an_insufficient_sibling_is_not_a_family_cohort(self):
        family = spec().family_slug
        for i in range(5):
            self.store_sibling(i, mean=0.02)
        registry.store_answer(
            self.session, spec("refused_v1", value=99.0), as_of=date(2025, 1, 1),
            price_snapshot_id=7, depth="full", status="insufficient",
            evidence_tier="vendor_pit", answer_json="{}", provenance_mix={},
            family_moments=[{"horizon": 10, "n": 1, "mean": 9.9,
                             "within_variance": 1.0}],
        )
        self.assertEqual(len(registry.family_moments(self.session, family)), 5)


# --------------------------------------------------------------------------- #
# Prediction inputs come off the answer, not out of a re-derivation
# --------------------------------------------------------------------------- #


class PredictionInputTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("predinputs")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.world = cf.seed_world(self.session)

    def test_prediction_inputs_carry_the_cohorts_own_outcome_distribution(self):
        outcome = query_mod.answer_query(
            self.session, context=self.world.context, as_of=self.world.as_of,
            depth="full", setup="gap_and_go_v1",
            parameters={"horizons_sessions": [5, 10]},
            requester_label="test", reps=50,
        )
        self.assertEqual(outcome.status, "ok")
        rows = registry.prediction_inputs(outcome.answer, outcome.build)
        self.assertEqual([r["horizon_sessions"] for r in rows], [5, 10])
        for row in rows:
            self.assertTrue(row["cohort_outcomes"])
            self.assertLessEqual(row["ci_low"], row["point_estimate"])
            self.assertGreaterEqual(row["ci_high"], row["point_estimate"])
            self.assertGreater(row["matures_on"], self.world.as_of)
            self.assertNotIn("coverage", row)

    def test_a_cited_full_answer_produces_scorable_predictions(self):
        outcome = query_mod.answer_query(
            self.session, context=self.world.context, as_of=self.world.as_of,
            depth="full", setup="gap_and_go_v1",
            parameters={"horizons_sessions": [5]},
            requester_label="test", reps=50,
        )
        resolved = citations.resolve(self.session, outcome.citation_id)
        written = registry.record_predictions(
            self.session,
            cohort_answer_id=resolved.answer_id,
            spec=outcome.setup,
            as_of=self.world.as_of,
            query_ticker="SY01",
            horizons=registry.prediction_inputs(outcome.answer, outcome.build),
            query_id=outcome.query_id,
        )
        self.assertEqual(len(written), 1)
        registry.score_prediction(self.session, written[0], 0.05)
        self.assertIsNotNone(written[0].sign_correct)
        self.assertIsNotNone(written[0].realized_percentile)


if __name__ == "__main__":
    unittest.main()
