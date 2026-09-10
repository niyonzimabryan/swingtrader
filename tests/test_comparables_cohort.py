"""Cohort construction from stored data (Spec N §4, Phase 3c).

The Phase 3b tests measure cohorts they were handed. These build them from
rows, which is where every one of Spec N §2's failure modes actually gets in:
a fact that did not exist yet, a universe evaluated from today's list, a
delisted name quietly absent, a market cap computed from next quarter's share
count.

Named exactly as Spec N §10 names them, where §10 names them.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import date, datetime, timedelta, timezone

from tests import cohortfixture as cf
from tests.dbfixture import TestDatabase

from comparables import cohort as cohort_mod
from comparables import lookahead as lookahead_mod
from comparables import report as report_mod
from comparables import setups as roster
from comparables.setup_spec import Condition, SetupSpec


#: The expensive, never-mutated part of the world (prices, filings, share
#: counts — on the order of 13,000 rows) is seeded once for this whole module
#: rather than once per test: nothing below ever mutates it, only the universe
#: and the snapshot, which `seed_mutable_world` cheaply rebuilds every test.
#: See the comment above `cohortfixture.seed_base_world`.
def setUpModule():
    global _MODULE_DB, _MODULE_BASE

    from database.db import get_session, init_db

    _MODULE_DB = TestDatabase("cohort_module")
    init_db(_MODULE_DB.url)
    # A short-lived session that commits and closes, not one held open for the
    # whole module: SQLite allows only one writer at a time, and each test
    # below opens its own session on the same file/schema. A session left open
    # across the module would hold a write lock that starves every one of them.
    with get_session() as session:
        _MODULE_BASE = cf.seed_base_world(session)


def tearDownModule():
    _MODULE_DB.cleanup()


class CohortTestCase(unittest.TestCase):
    """One short-lived session per test, over the module's shared base world."""

    #: Passed to `cohortfixture.seed_mutable_world`, not to the retired
    #: `seed_world` — only the universe/audit/snapshot flags apply here, since
    #: the base (prices, filings, share counts) is module-shared and seeded
    #: with `cohortfixture.seed_base_world`'s defaults for every class.
    seed_kwargs: dict = {}

    def setUp(self):
        from database.db import get_session, init_db

        # Defensive: nothing else in this module repoints the global engine,
        # but this keeps the invariant explicit and cheap (ensure_schema on an
        # already-versioned database is a fast no-op check).
        init_db(_MODULE_DB.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.world = cf.seed_mutable_world(self.session, _MODULE_BASE, **self.seed_kwargs)

    def build(self, slug="gap_and_go_v1", *, as_of=None, context=None, **kwargs):
        return cohort_mod.build_cohort(
            self.session,
            roster.get(slug),
            as_of=as_of or self.world.as_of,
            context=context or self.world.context,
            **kwargs,
        )


# --------------------------------------------------------------------------- #
# The bitemporal filter (§4.1)
# --------------------------------------------------------------------------- #


class LookaheadTests(CohortTestCase):
    def test_lookahead_rejected(self):
        """A fact with `known_at_utc > cutoff` never enters a cohort.

        Asserted three ways, because the rule can be broken in three places:
        at the read (the SQL filter), at the qualification (a covariate reaching
        into a session the event precedes), and at the event clock (session 0
        landing on or before the instant the fact was knowable).
        """
        build = self.build("earnings_sue_seasonal_v1")
        self.assertTrue(build.events, "the fixture cohort should not be empty")

        # 1. The read seam refuses a later fact outright.
        from filings.observations import observations_known_at

        cutoff = min(e.known_at_utc for e in build.events)
        rows = observations_known_at(self.session, cutoff=cutoff)
        for row in rows:
            self.assertLessEqual(
                row.known_at_utc.replace(tzinfo=timezone.utc), cutoff,
                f"{row.fact_type} was knowable at {row.known_at_utc}, after {cutoff}",
            )

        # 2. Every covariate is computed from a session that had closed.
        from comparables.config import SESSION_CLOSE_UTC

        for event in build.events:
            zero = build.calendar.session_zero(event.known_at_utc)
            prior = build.calendar.sessions[zero - 1]
            self.assertLessEqual(
                datetime.combine(prior, SESSION_CLOSE_UTC), event.known_at_utc,
                f"{event.event_id}: session {prior} had not closed at its cutoff",
            )

        # 3. Session 0 never opens before the fact was knowable.
        from comparables.config import SESSION_OPEN_UTC

        for event in build.events:
            zero = build.calendar.session_zero(event.known_at_utc)
            self.assertGreaterEqual(
                datetime.combine(build.calendar.sessions[zero], SESSION_OPEN_UTC),
                event.known_at_utc,
            )

    def test_a_fact_published_after_the_event_cannot_qualify_it(self):
        """The direct form: delete nothing, move the clock back instead."""
        early = self.world.sessions[120]
        build = cohort_mod.build_cohort(
            self.session, roster.get("earnings_sue_seasonal_v1"),
            as_of=early, context=self.world.context,
        )
        for event in build.events:
            self.assertLessEqual(event.known_at_utc.date(), early)

    def test_announcement_time_from_8k_acceptance(self):
        """An event dated from `filingDate` rather than `acceptanceDateTime` is
        rejected — at read time, not only at write time.

        `filings/observations.py` refuses to *write* such a row. A row written
        before that rule existed, or by a future adapter that forgets, would
        still be sitting in the ledger, so the cohort builder checks the field
        name it was stamped from and drops the event by reason.
        """
        from database.models import SourceObservation

        row = (
            self.session.query(SourceObservation)
            .filter(SourceObservation.fact_type == cohort_mod.FACT_TYPE_EARNINGS_8K)
            .order_by(SourceObservation.known_at_utc)
            .first()
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.payload["known_at_source"], "acceptanceDateTime")

        import json

        payload = dict(row.payload)
        payload["known_at_source"] = "filingDate"
        row.payload_json = json.dumps(payload, sort_keys=True)
        self.session.flush()

        build = self.build("earnings_sue_seasonal_v1")
        rejected = [
            e for e in build.excluded if e.reason == "known_at_not_acceptance"
        ]
        self.assertEqual(len(rejected), 1, "the mis-stamped 8-K should be refused")
        self.assertIn("lookahead", rejected[0].detail)
        self.assertNotIn(
            rejected[0].event_id, {e.event_id for e in build.events}
        )


# --------------------------------------------------------------------------- #
# Universe and survivorship (§4.2)
# --------------------------------------------------------------------------- #


class UniverseTests(CohortTestCase):
    def test_universe_is_stored_not_computed(self):
        """No `universe_membership` rows for the period caps the tier.

        The alternative — falling back to "every security we happen to have" and
        saying nothing — is survivorship bias with a clean-looking tier on it.
        """
        with_rows = self.build()
        self.assertTrue(with_rows.universe.point_in_time)
        self.assertEqual(with_rows.evidence_cap, "vendor_pit")

        from database.models import UniverseMembership

        self.session.query(UniverseMembership).delete()
        self.session.flush()

        without = self.build()
        self.assertFalse(without.universe.point_in_time)
        self.assertEqual(without.evidence_cap, "archival_reconstructed")
        self.assertIn("survivorship bias", without.universe.reason)
        for event in without.events:
            self.assertEqual(event.provenance, "archival_reconstructed")
        self.assertEqual(
            report_mod.evidence_tier_for(without.events), "archival_reconstructed"
        )

    def test_survivorship_downgrades_tier(self):
        """A universe that cannot reproduce point-in-time membership is capped.

        `sp500_wikipedia_v1` is a hand-maintained Wikipedia scrape with
        acknowledged incompleteness, so its membership source is
        `archival_reconstructed` and every cohort drawn on it inherits that,
        however clean the prices are.
        """
        from data.prices.base import MembershipInterval
        from data.prices import store, universes

        rows = tuple(
            MembershipInterval(
                universe_slug=universes.UNIVERSE_SLUG,
                security_uid=cf.uid_for(i),
                ticker=cf.ticker_for(i),
                member_from=self.world.sessions[0],
                member_to=None,
                source="sp500_wikipedia_v1",
                known_at_utc=cf.session_close_utc(self.world.sessions[0]),
            )
            for i in range(cf.N_SECURITIES)
        )
        store.replace_universe(self.session, universes.UNIVERSE_SLUG, rows)

        build = self.build()
        self.assertTrue(build.universe.point_in_time)
        self.assertEqual(build.universe.provenance, "archival_reconstructed")
        self.assertEqual(build.evidence_cap, "archival_reconstructed")

    def test_membership_is_evaluated_as_of_the_event_date(self):
        """A name that joined after an event is not in that event's universe.

        Not "was it ever a member", which is what a join against today's list
        answers. `SY01` joins two thirds of the way through the window; its
        earlier announcements are excluded by name and reason, and its later
        ones are in the cohort.
        """
        from data.prices.base import MembershipInterval
        from data.prices import store, universes

        latecomer = cf.ticker_for(1)
        joined = self.world.sessions[300]
        rows = [
            MembershipInterval(
                universe_slug=universes.UNIVERSE_SLUG,
                security_uid=cf.uid_for(i),
                ticker=cf.ticker_for(i),
                member_from=joined if i == 1 else self.world.sessions[0],
                member_to=None,
                source=universes.SOURCE,
                known_at_utc=cf.session_close_utc(
                    joined if i == 1 else self.world.sessions[0]
                ),
            )
            for i in range(cf.N_SECURITIES)
        ]
        store.replace_universe(self.session, universes.UNIVERSE_SLUG, rows)

        build = self.build("earnings_sue_seasonal_v1")
        mine = [e for e in build.events if e.ticker == latecomer]
        for event in mine:
            self.assertGreaterEqual(
                event.known_at_utc.date(), joined,
                "a pre-membership event entered the cohort",
            )
        excluded = [
            e for e in build.excluded
            if e.ticker == latecomer and e.reason == "not_a_universe_member"
        ]
        self.assertTrue(excluded, "the pre-membership candidate was not recorded")
        for item in excluded:
            self.assertLess(item.event_date, joined)

    def test_survivor_only_cohort_refused(self):
        """A cohort meeting the floor only after excluding delisted members.

        Phase 3b implemented the check and left it inert for want of a universe
        delisting rate. The price plane supplies it now: `securities` carries
        each name's delisting date and reason, and `universe_membership` says
        which of them the universe held over the period.
        """
        build = self.build("earnings_sue_seasonal_v1")
        self.assertIsNotNone(build.universe_delisting_rate)
        self.assertGreater(build.universe_delisting_rate, 0.0)

        survivors = dataclasses.replace(
            build,
            events=tuple(
                e for e in build.events if e.ticker != self.world.delisted_ticker
            ),
        )
        answer = cohort_mod.answer_for(survivors, depth="quick", reps=50).primary
        self.assertEqual(answer.status, "insufficient")
        self.assertIn("survivors alone", answer.refusal_reason)
        self.assertIn("composition failure", answer.refusal_reason)

        # And the cohort *with* its delisted member is not refused on that rule.
        kept = cohort_mod.answer_for(build, depth="quick", reps=50).primary
        self.assertEqual(kept.status, "ok")

    def test_delisted_names_are_retained_with_a_terminal_outcome(self):
        """The name that went to zero stays in, carrying the Shumway return."""
        build = self.build("earnings_sue_seasonal_v1")
        delisted = [
            e for e in build.events if e.ticker == self.world.delisted_ticker
        ]
        self.assertTrue(delisted, "the delisted name left the cohort entirely")
        for event in delisted:
            self.assertIsNotNone(event.terminal)
            self.assertTrue(event.terminal.resolved)
            self.assertEqual(event.terminal.reason, "performance_nasdaq")
            self.assertAlmostEqual(event.terminal.terminal_return, -0.55)

    def test_a_merger_without_deal_terms_is_censored_not_matured(self):
        """§4.4: an unresolved end is counted, reported, and never in a mean."""
        from comparables.outcomes import maturity

        build = self.build("earnings_sue_seasonal_v1")
        merged = [e for e in build.events if e.ticker == self.world.merged_ticker]
        self.assertTrue(merged, "the merged name left the cohort entirely")
        for event in merged:
            self.assertIsNotNone(event.terminal)
            self.assertFalse(event.terminal.resolved)
            self.assertIn("merger", event.terminal.reason)
            self.assertFalse(maturity(event, build.calendar, 20).matured)


# --------------------------------------------------------------------------- #
# The delisting audit (§4.2)
# --------------------------------------------------------------------------- #


class DelistingAuditTests(CohortTestCase):
    def test_delisting_audit_recorded(self):
        """A snapshot with no audit result cannot back a point-in-time cohort.

        A price file that merely *stops* at the last quote looks identical to
        one that carried the collapse, and the difference is the whole of the
        terminal return. Without the audit the engine does not know which it
        has, so it says so in the tier rather than picking one.
        """
        good = self.build()
        self.assertTrue(good.snapshot.audit_recorded)
        self.assertTrue(good.snapshot.can_back_point_in_time)
        self.assertEqual(good.evidence_cap, "vendor_pit")

        from data.prices import store

        store.record_snapshot(
            self.session, "unaudited", cf.SOURCE, {"synthetic": True}, None
        )
        context = dataclasses.replace(
            self.world.context, price_snapshot_slug="unaudited"
        )
        unaudited = self.build(context=context)
        self.assertFalse(unaudited.snapshot.audit_recorded)
        self.assertFalse(unaudited.snapshot.can_back_point_in_time)
        self.assertEqual(unaudited.evidence_cap, "archival_reconstructed")
        self.assertEqual(
            report_mod.evidence_tier_for(unaudited.events), "archival_reconstructed"
        )

    def test_a_missing_snapshot_is_named_not_assumed(self):
        context = dataclasses.replace(
            self.world.context, price_snapshot_slug="never_created"
        )
        build = self.build(context=context)
        self.assertFalse(build.snapshot.exists)
        self.assertEqual(build.evidence_cap, "archival_reconstructed")
        self.assertTrue(any("no price snapshot" in w for w in build.warnings))

    def test_a_carried_collapse_is_not_synthesised_twice(self):
        """When the audit says the file carries the collapse, Shumway is not
        applied on top: the stored bars already are the terminal decline."""
        from data.prices import store

        store.record_snapshot(
            self.session, cf.SNAPSHOT_SLUG, cf.SOURCE, {"synthetic": True},
            cf.audit_blob(synthesised=False),
        )
        build = self.build("earnings_sue_seasonal_v1")
        self.assertFalse(build.snapshot.terminal_returns_synthesised)
        delisted = [
            e for e in build.events if e.ticker == self.world.delisted_ticker
        ]
        self.assertTrue(delisted)
        for event in delisted:
            self.assertEqual(event.terminal.reason, "performance_carried_in_series")
            self.assertEqual(event.terminal.terminal_return, 0.0)
            self.assertTrue(event.terminal.resolved)


# --------------------------------------------------------------------------- #
# Provenance (§8)
# --------------------------------------------------------------------------- #


class ProvenanceTests(CohortTestCase):
    def test_vendor_pit_never_pooled_with_archival(self):
        """Mixed provenance yields separate blocks; `provenance_mix` sums to n.

        Phase 3b implemented the safe half of the §8 rule — a mixed cohort was
        refused outright. This is the other half: two blocks, each measured on
        its own events, and no statistic computed across the boundary.
        """
        build = self.build("earnings_sue_seasonal_v1")
        self.assertTrue(build.events)

        # Re-label a third of the cohort archival, as a mixed ledger would.
        events = list(build.events)
        for i, event in enumerate(events):
            if i % 3 == 0:
                events[i] = dataclasses.replace(
                    event, provenance="archival_reconstructed"
                )
        mixed = dataclasses.replace(build, events=tuple(events))
        result = cohort_mod.answer_for(mixed, depth="quick", reps=50)

        self.assertIsNotNone(result.archival, "no separate archival block rendered")
        self.assertTrue(result.blocks.point_in_time)
        self.assertTrue(result.blocks.archival)
        self.assertEqual(
            {e.provenance for e in result.blocks.archival},
            {"archival_reconstructed"},
        )
        self.assertNotIn(
            "archival_reconstructed", {e.provenance for e in result.blocks.point_in_time}
        )
        self.assertEqual(
            sum(dict(result.provenance_mix).values()), len(mixed.events)
        )
        self.assertEqual(result.blocks.n, len(mixed.events))

        # The two blocks are different statistics, not one statistic twice.
        self.assertEqual(result.primary.evidence_tier, "vendor_pit")
        self.assertEqual(result.archival.evidence_tier, "archival_reconstructed")
        self.assertNotEqual(
            report_mod.to_json(result.primary), report_mod.to_json(result.archival)
        )

    def test_the_archival_block_is_not_a_second_trial(self):
        """Rendering two blocks is one question asked once (§7)."""
        build = self.build("earnings_sue_seasonal_v1")
        events = list(build.events)
        for i in range(0, len(events), 3):
            events[i] = dataclasses.replace(
                events[i], provenance="archival_reconstructed"
            )
        mixed = dataclasses.replace(build, events=tuple(events))
        result = cohort_mod.answer_for(mixed, depth="quick", reps=50)
        self.assertEqual(result.archival.trials_against_this_pattern, 0)


# --------------------------------------------------------------------------- #
# Facts (§4.0)
# --------------------------------------------------------------------------- #


class FactTests(CohortTestCase):
    def test_sue_from_xbrl_only(self):
        """`sue_seasonal` comes from `companyfacts` rows, never a vendor estimate.

        Two assertions, because the property has two halves: the number is a
        function of stored XBRL EPS observations *and* nothing else on the path
        can supply one. Deleting every `eps_diluted` row must empty the cohort;
        if some other source could serve the fact, it would not.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(cohort_mod.seasonal_sue).strip())
        body = ast.get_source_segment(
            inspect.getsource(cohort_mod.seasonal_sue).strip(),
            ast.Module(body=tree.body[0].body[1:], type_ignores=[]),
        ) or "\n".join(
            ast.unparse(node) for node in tree.body[0].body[1:]
        )
        self.assertIn("FACT_TYPE_EPS", body)
        for banned in ("estimate", "consensus", "analyst", "vendor"):
            self.assertNotIn(
                banned, body.lower(),
                f"seasonal_sue's body mentions {banned!r}; the v1 surprise fact "
                f"is computed entirely from XBRL companyfacts (Spec N §4.0)",
            )

        before = self.build("earnings_sue_seasonal_v1")
        self.assertTrue(before.events)

        from database.models import SourceObservation

        self.session.query(SourceObservation).filter(
            SourceObservation.fact_type == "eps_diluted"
        ).delete(synchronize_session=False)
        self.session.flush()

        after = self.build("earnings_sue_seasonal_v1")
        self.assertEqual(after.events, ())
        self.assertTrue(any(
            e.reason in ("no_sue_available", "no_eps_period_for_announcement")
            for e in after.excluded
        ))

    def test_sue_is_the_announced_quarter_never_a_stale_one(self):
        """A company whose EPS lands with the 10-Q is dated *there*, not at the
        announcement, and its SUE is the announced quarter's, not last one's."""
        build = self.build("earnings_sue_seasonal_v1")
        late = [e for e in build.events if e.ticker == self.world.late_eps_ticker]
        self.assertTrue(late, "the late-EPS company produced no events")
        notes = [w for w in build.warnings if "sue_known_after_announcement" in w]
        self.assertTrue(notes, "the late-EPS path was never taken")
        for note in notes:
            announced = note.split("announced ")[1].split(",")[0]
            knowable = note.split("knowable ")[1]
            self.assertLess(
                datetime.fromisoformat(announced), datetime.fromisoformat(knowable)
            )

    def test_market_cap_has_share_source(self):
        """A `market_cap_decile` on a name with no known share count is refused.

        Not computed from a later count, and not defaulted to a decile. The
        exclusion is by name and by reason so a reader can see the composition
        change and its cause.
        """
        build = self.build("earnings_sue_seasonal_v1")
        refused = [
            e for e in build.excluded if e.reason == "market_cap_no_share_source"
        ]
        self.assertTrue(refused, "the no-share-count company was not refused")
        self.assertEqual(
            {e.ticker for e in refused}, {self.world.no_shares_ticker}
        )
        self.assertNotIn(
            self.world.no_shares_ticker, {e.ticker for e in build.events}
        )

        # And every name that *is* in the cohort has a market cap traceable to
        # a share count that was knowable at its event date.
        for event in build.events:
            self.assertIn("market_cap_decile", dict(event.covariates))

    def test_a_share_count_filed_later_is_not_used(self):
        """The count that qualifies an event was knowable before it."""
        cutoff = datetime(2022, 6, 1, tzinfo=timezone.utc)
        row = cohort_mod.shares_outstanding_known_at(
            self.session, entity_cik=cf.cik_for(1), cutoff=cutoff
        )
        self.assertIsNotNone(row)
        self.assertLessEqual(row.known_at_utc.replace(tzinfo=timezone.utc), cutoff)

        earlier = cohort_mod.shares_outstanding_known_at(
            self.session, entity_cik=cf.cik_for(1),
            cutoff=datetime(2018, 1, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(earlier, "a count from the future was returned")

    def test_the_three_price_series_reach_the_cohort(self):
        build = self.build()
        event = build.events[0]
        self.assertEqual(
            len(event.series.raw), len(event.series.split_adjusted)
        )
        self.assertEqual(
            len(event.series.raw), len(event.series.total_return)
        )

    def test_sector_is_a_current_vintage_covariate(self):
        """`sector` is usable and is labelled `vintage=current` (§4.5)."""
        from comparables import balance, config

        self.assertIn("sector", config.CURRENT_VINTAGE_COVARIATES)
        block = balance.query_vs_cohort("sector", 1.0, [1.0, 2.0, 3.0])
        self.assertEqual(block.vintage, "current")
        neutral = balance.query_vs_cohort("liquidity_decile", 1.0, [1.0, 2.0, 3.0])
        self.assertEqual(neutral.vintage, "point_in_time")


# --------------------------------------------------------------------------- #
# The analog ranker as a generator only (§4.5)
# --------------------------------------------------------------------------- #


class AnalogGeneratorTests(CohortTestCase):
    def test_analog_generator_adds_no_bias(self):
        """The statistics are invariant to the ranker's ordering and top-k.

        This is the property Spec N §4.5 actually relies on: *"the final
        statistics are computed over the matched set invariant to the ranker's
        ordering or its fixed top-k. Otherwise similarity becomes a hidden
        selection-on-outcome channel."* So a generator that reverses the
        candidate list, and one that truncates it to three, must both leave the
        answer byte-identical.
        """
        baseline = self.build()
        reversed_order = self.build(candidate_generator=lambda ids: list(reversed(ids)))
        top_three = self.build(candidate_generator=lambda ids: list(ids)[:3])
        dropped = self.build(candidate_generator=lambda ids: [])

        expected = lookahead_mod.fingerprints(baseline.events)
        for variant, label in (
            (reversed_order, "reversed"), (top_three, "top-3"), (dropped, "empty"),
        ):
            with self.subTest(generator=label):
                self.assertEqual(
                    lookahead_mod.fingerprints(variant.events), expected,
                    "the generator changed which events qualified",
                )

        answers = [
            report_mod.to_json(
                cohort_mod.answer_for(b, depth="quick", reps=50).primary
            )
            for b in (baseline, reversed_order, top_three, dropped)
        ]
        self.assertEqual(len(set(answers)), 1, "the generator moved a statistic")

        # It really did propose something different, or the test proves nothing.
        self.assertNotEqual(baseline.candidate_order, reversed_order.candidate_order)
        self.assertEqual(len(top_three.candidate_order), 3)

    def test_the_ranker_carries_one_post_event_feature_and_it_cannot_reach_here(self):
        """`data/analog_ranker.py` is a generator because of exactly this.

        Its similarity score is a weighted blend of pre-event features **and one
        post-event one**: `outcome_completeness`, which reads
        `EventOutcome.status` — a fact that only exists once the horizon has
        matured. Spec N §10 asks that "the ranker's features are all pre-event";
        as written, one is not, and the ranker is not this phase's to change.

        What makes that safe is structural rather than aspirational: the cohort
        seam takes candidate *identifiers* and nothing else, the qualified set is
        computed over every candidate regardless, and `comparables/` cannot
        import `data.analog_ranker` at all (`test_comparables_import_graph`). The
        finding is asserted here so it stays visible until the ranker drops the
        feature.
        """
        import inspect
        import re

        from data.analog_ranker import AnalogRanker

        source = inspect.getsource(AnalogRanker._analog_score)
        parts = set(re.findall(r'"(\w+)":\s*self\._', source))
        self.assertTrue(parts, "could not read the ranker's feature list")

        post_event = {"outcome_completeness"}
        self.assertEqual(
            parts & post_event, post_event,
            "the ranker no longer scores on outcome completeness — delete this "
            "test's carve-out and assert every feature is pre-event",
        )
        pre_event = parts - post_event
        self.assertEqual(pre_event, {
            "event_semantic_similarity", "peer_similarity", "context_similarity",
            "magnitude_similarity", "source_quality_confidence", "recency_score",
        })

        signature = inspect.signature(cohort_mod.build_cohort)
        annotation = signature.parameters["candidate_generator"].annotation
        self.assertIn("CandidateGenerator", str(annotation))


# --------------------------------------------------------------------------- #
# Setup roster and refusals
# --------------------------------------------------------------------------- #


class RosterTests(unittest.TestCase):
    def test_the_roster_is_frozen_hashed_and_family_keyed(self):
        for slug in roster.slugs():
            with self.subTest(setup=slug):
                entry = roster.get(slug)
                self.assertIsInstance(entry.spec, SetupSpec)
                self.assertEqual(len(entry.setup_hash), 64)
                self.assertTrue(entry.family_slug.startswith(entry.spec.universe))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    entry.spec.slug = "renamed"

    def test_a_parameter_makes_a_new_hash_in_the_same_family(self):
        base = roster.get("gap_and_go_v1")
        moved = roster.get("gap_and_go_v1", {"gap_pct": 6.0})
        self.assertNotEqual(base.setup_hash, moved.setup_hash)
        self.assertEqual(base.family_slug, moved.family_slug)

    def test_an_unknown_parameter_raises_rather_than_being_ignored(self):
        from comparables.setup_spec import SetupSpecError

        with self.assertRaises(SetupSpecError):
            roster.get("gap_and_go_v1", {"not_a_condition": 1.0})

    def test_an_unknown_slug_names_the_roster(self):
        with self.assertRaises(roster.UnknownSetup) as caught:
            roster.get("momentum_thing_v9")
        self.assertIn("gap_and_go_v1", str(caught.exception))

    def test_insider_cluster_refuses_with_pending_plane(self):
        """Declared, frozen, hashed — and refused, because Phase 4 owns the fact.

        `pending_plane` is not `insufficient`. The first means the evidence does
        not exist yet; the second means the engine looked and the cohort was too
        small. Rounding the first into the second would put "we found nothing"
        in front of a reader whose truth is "we have not looked".
        """
        entry = roster.get("insider_cluster_v1")
        self.assertFalse(entry.available)
        self.assertIn("pending_plane", entry.unavailable_reason)
        self.assertIn("Form 4", entry.unavailable_reason)
        self.assertEqual(len(entry.setup_hash), 64)
        self.assertEqual(
            entry.family_slug, "liquid_us_equity_v1:insider_cluster_count:>="
        )

    def test_the_roster_catalogue_is_plain_data(self):
        import json

        json.dumps(roster.catalogue())
        self.assertEqual(
            {e["slug"] for e in roster.catalogue()}, set(roster.slugs())
        )


class PendingPlaneCohortTests(CohortTestCase):
    def test_building_a_pending_setup_raises_rather_than_returning_empty(self):
        with self.assertRaises(cohort_mod.PendingPlane):
            self.build("insider_cluster_v1")


# --------------------------------------------------------------------------- #
# The policy table
# --------------------------------------------------------------------------- #


class PolicyTests(unittest.TestCase):
    def test_an_unknown_execution_policy_raises(self):
        with self.assertRaises(cohort_mod.CohortConstructionError):
            cohort_mod.policy_for("something_plausible_v1")

    def test_a_setup_with_no_policy_has_no_honest_policy_return(self):
        with self.assertRaises(cohort_mod.CohortConstructionError):
            cohort_mod.policy_for(None)

    def test_the_roster_policies_all_resolve(self):
        for slug in roster.slugs():
            spec = roster.get(slug).spec
            self.assertEqual(
                cohort_mod.policy_for(spec.execution_policy).slug,
                spec.execution_policy,
            )


if __name__ == "__main__":
    unittest.main()
