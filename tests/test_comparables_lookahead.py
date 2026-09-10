"""The lookahead harness, over every stored cohort (Spec N §10).

> `test_truncated_data_identical_answer` — *re-running any cohort with every
> fact table truncated at each event's cutoff yields a byte-identical answer.
> Runs on every cohort in CI, not only on synthetic fixtures.*

The harness itself is `comparables/lookahead.py`; this file and
`scripts/cohort_lookahead_check.py` both call it, so the thing that runs in CI
and the thing an owner runs against the production database are the same code.

It is not a formality. Building it caught two real leaks in this phase's own
cohort builder before either reached a number anybody would have read:

* an eligible pool that stretched past the last event in the cohort, so a
  universe rebuilt a month later moved a balance diagnostic;
* an earnings event whose announced quarter was missing from the ledger being
  qualified on the **previous** quarter's surprise — a stale-but-real number,
  which is the most convincing kind of wrong.

Both are fixed, and both would have passed every other test in this repository.
"""

from __future__ import annotations

import unittest

from tests import cohortfixture as cf
from tests.dbfixture import TestDatabase

from comparables import cohort as cohort_mod
from comparables import lookahead as lookahead_mod
from comparables import setups as roster


class LookaheadHarnessTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("lookahead")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.world = cf.seed_world(self.session)

    def test_truncated_data_identical_answer(self):
        """Every stored cohort, every event cutoff, byte for byte.

        `max_cutoffs=None` is the point: not a sample of the cutoffs, every one
        of them, because a leak that only shows at one event's cutoff is still a
        leak. The bootstrap runs at 50 replications because the harness is
        asserting the *inputs* did not move, and a 10,000-replication run per
        cutoff would make it too slow to run in CI, which is the same as not
        running it. Every draw is seeded, so `quick` is byte-comparable.
        """
        reports = lookahead_mod.check_roster(
            self.session, as_of=self.world.as_of, context=self.world.context,
            max_cutoffs=None,
        )
        self.assertTrue(reports, "the harness checked no cohorts at all")
        for report in reports:
            with self.subTest(setup=report.setup_slug):
                self.assertGreater(
                    report.n_events, 0,
                    "a cohort with no events proves nothing; the harness would "
                    "be green because there was nothing to check",
                )
                self.assertEqual(report.n_cutoffs_checked, report.n_cutoffs_total)
                self.assertTrue(report.answer_identical)
                report.raise_for_findings()
                self.assertTrue(report.clean)

    def test_a_full_answer_is_identical_too_including_the_policy_interval(self):
        """The roster sweep runs at `quick`; the sizing input only exists at `full`.

        `check_roster` above compares `quick` answers, and a `quick` answer
        carries no `PolicySummary` at all — so the field Spec L §6.6 sizes a
        live order from, `policy.net_ci`, is not covered by it. This runs the
        same harness at `depth='full'` so the policy leg and its lower 90% bound
        go through the byte comparison with everything else: a bound that moved
        when facts published after the last event were deleted would be a
        lookahead leak straight into a position size.

        One horizon, two cutoffs and 50 replications, for the reason the
        harness's own docstring gives: it is asserting the inputs did not move,
        and a full-depth sweep at production replications would be too slow to
        run in CI, which is the same as not running it. `net_ci` is computed
        per horizon from the same code either way, so a second horizon would
        buy nothing but minutes.
        """
        from comparables import report as report_mod
        from tests import comparablesfixture as cfx

        # The bytes `_full_answer_identical` compares are `to_json`'s, and that
        # is where the field has to be for the truncation check to cover it.
        self.assertIn('"net_ci"', report_mod.to_json(cfx.full_answer()))

        report = lookahead_mod.check_cohort(
            self.session,
            roster.get("gap_and_go_v1", {"horizons_sessions": [5]}),
            as_of=self.world.as_of,
            context=self.world.context,
            max_cutoffs=2,
            depth="full",
            reps=50,
        )
        self.assertGreater(report.n_events, 0, "nothing was checked")
        self.assertTrue(report.answer_identical)
        report.raise_for_findings()

    def test_the_pending_roster_entry_is_skipped_not_reported_clean(self):
        reports = lookahead_mod.check_roster(
            self.session, as_of=self.world.as_of, context=self.world.context,
            max_cutoffs=2,
        )
        self.assertNotIn("insider_cluster_v1", {r.setup_slug for r in reports})

    def test_the_harness_detects_an_actual_leak(self):
        """A harness that has never failed is a harness nobody should trust.

        A deliberately leaky qualifier — one that computes an event's covariates
        from the *end* of the query window instead of from the last session that
        had closed when the event happened — must be caught. Without this, a
        green harness could equally mean "no leaks" or "the comparison is
        vacuous".
        """
        original = cohort_mod._last_session_closed_by

        def leaky(sessions, cutoff):
            # The leak: every covariate computed from the last session in the
            # window, whenever the event happened.
            return sessions[-1] if sessions else None

        cohort_mod._last_session_closed_by = leaky
        self.addCleanup(setattr, cohort_mod, "_last_session_closed_by", original)

        report = lookahead_mod.check_cohort(
            self.session, roster.get("earnings_sue_seasonal_v1"),
            as_of=self.world.as_of, context=self.world.context, max_cutoffs=6,
            reps=50,
        )
        self.assertGreater(report.n_events, 0, "the leaky build produced no cohort")
        self.assertFalse(report.clean, "the harness missed a deliberate leak")
        with self.assertRaises(lookahead_mod.LookaheadFailure):
            report.raise_for_findings()

    def test_truncation_is_rolled_back(self):
        """The harness deletes rows and must always put them back."""
        from database.models import PriceBar, SourceObservation, UniverseMembership

        def counts():
            return (
                self.session.query(SourceObservation).count(),
                self.session.query(PriceBar).count(),
                self.session.query(UniverseMembership).count(),
            )

        before = counts()
        lookahead_mod.check_cohort(
            self.session, roster.get("gap_and_go_v1"), as_of=self.world.as_of,
            context=self.world.context, max_cutoffs=3, reps=50,
        )
        self.assertEqual(counts(), before, "the harness left rows deleted")

    def test_truncation_survives_an_exception(self):
        """Even when the rebuild blows up, the savepoint rolls back."""
        from database.models import SourceObservation

        before = self.session.query(SourceObservation).count()
        with self.assertRaises(RuntimeError):
            with lookahead_mod._rolled_back(self.session):
                from datetime import datetime, timezone

                lookahead_mod.truncate_at(
                    self.session, datetime(2000, 1, 1, tzinfo=timezone.utc)
                )
                raise RuntimeError("boom")
        self.assertEqual(self.session.query(SourceObservation).count(), before)

    def test_an_empty_cohort_reports_itself_rather_than_passing_quietly(self):
        spec = roster.get("gap_and_go_v1", {"gap_pct": 500.0})
        report = lookahead_mod.check_cohort(
            self.session, spec, as_of=self.world.as_of, context=self.world.context,
        )
        self.assertEqual(report.n_events, 0)
        self.assertEqual(report.n_cutoffs_total, 0)
        self.assertTrue(report.clean)


class FingerprintTests(unittest.TestCase):
    def test_the_fingerprint_covers_what_qualification_decided(self):
        """And deliberately not the price series, which the harness truncates."""
        from comparables.fixtures import cohort as fixture_cohort

        event = fixture_cohort()[0]
        fingerprint = lookahead_mod.event_fingerprint(event)
        self.assertEqual(
            set(fingerprint),
            {"event_id", "ticker", "known_at_utc", "provenance", "regime",
             "liquidity_decile", "covariates", "terminal"},
        )
        self.assertNotIn("series", fingerprint)

    def test_floats_are_compared_by_repr_not_by_format(self):
        """`0.1 + 0.2` must not compare equal to `0.3` by rounding luck."""
        import dataclasses

        from comparables.fixtures import cohort as fixture_cohort

        event = fixture_cohort()[0]
        moved = dataclasses.replace(
            event, covariates=(("market_cap_decile", 8.000000000000002),)
        )
        self.assertNotEqual(
            lookahead_mod.fingerprints([event]), lookahead_mod.fingerprints([moved])
        )

    def test_cutoff_thinning_keeps_the_ends(self):
        import dataclasses
        from datetime import datetime, timedelta, timezone

        from comparables.fixtures import cohort as fixture_cohort

        base = fixture_cohort()[0]
        events = [
            dataclasses.replace(
                base,
                event_id=f"e{i}",
                known_at_utc=datetime(2024, 1, 1, tzinfo=timezone.utc)
                + timedelta(days=i),
            )
            for i in range(50)
        ]
        thinned = lookahead_mod._cutoffs(events, 5)
        every = lookahead_mod._cutoffs(events, None)
        self.assertEqual(len(thinned), 5)
        self.assertEqual(thinned[0], every[0])
        self.assertEqual(thinned[-1], every[-1])
        self.assertEqual(len(every), 50)


if __name__ == "__main__":
    unittest.main()
