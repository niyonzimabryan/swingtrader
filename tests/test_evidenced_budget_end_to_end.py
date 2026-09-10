"""The evidenced budget, end to end on stored rows (Spec L §6.6, Spec N §8).

Phase 3c stored cohort answers and Phase 6 built the sizing rule that reads
them, but nothing joined the two: a real answer carried a policy net point
estimate and no interval, a `SetupSpec` carried no ticker, and the gate read
attributes off an object where the resolver hands back a stored row. So every
real citation was — correctly, and uselessly — `discretionary` with
`citation_no_policy_lower_bound`.

This file is the join, and it is deliberately not a unit test of any of the
three pieces. It runs `compare_setups` against `tests/cohortfixture.py`'s
stored world, journals the answer it gets back through the Spec M citer, and
then proposes an order citing it through the **real** Phase 2 seam
(`workspace.tools.bind_citation_seam`) — the resolver production uses, not a
fake. What it asserts is the thing neither PR could assert alone: that the card
says `evidenced`, and prints `risk_fraction`, `m = clip(LB/PE, 0, 1)`, `LB`,
`PE` and the horizon those came from.

The negative half is the same rule refusing to award the label, once per way an
answer can fail to be evidence: a subject that did not qualify, a `quick`
answer, an `insufficient` one, an `inconclusive` one, `LB <= 0`, a citation
older than `CITATION_MAX_AGE_SESSIONS`, and an answer about another name. In
`advisory` mode every one of those is a live `proposed` row on the
discretionary budget with the reason printed — **nothing is suppressed for lack
of evidence** — and `strict` mode is asserted to behave differently, because a
flag that changes nothing is not a flag.

The world is seeded with `gap_followthrough`, which is the only way an
evidenced case can exist at all: without a real post-gap drift the fixture's
policy net has a lower 90% bound below zero, and `LB <= 0` is by definition
*not* the evidenced branch.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from database.db import get_session, init_db
from portfolio import evidence, proposals
from portfolio.sizing import EVIDENCE_SIZED_TO_ZERO
from tests import cohortfixture as cf
from tests import proposalfixture as pf
from tests.dbfixture import TestDatabase

from comparables import citations as cohort_citations
from comparables import query as query_mod

#: Strong enough that the simulated trade clears its 4% first target inside the
#: policy's 14-calendar-day cap, so the policy net's lower 90% bound sits above
#: zero. Anything weaker tests the discretionary branch, which is tested below
#: anyway and does not need a second cohort.
FOLLOWTHROUGH = 0.006

#: Five horizons at `full` depth is a lot of bootstrap for a test that is not
#: about the bootstrap. Two is enough for "the horizon nearest the hold" to be a
#: choice rather than a formality.
HORIZONS = [5, 10]
REPS = 200

ENTRY, STOP = 100.0, 95.0
RISK_FRACTION = 0.005


class EvidencedBudgetEndToEnd(unittest.TestCase):
    """One database carrying both worlds: the cohort's rows and the ledger's."""

    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("evidenced_e2e")
        init_db(cls.db.url)

        with get_session() as session:
            cls.world = cf.seed_world(session, gap_followthrough=FOLLOWTHROUGH)
            session.commit()

        # The subject qualifies on the last injected gap date and on no later
        # one, so that is the `as_of` a proposal in that name would cite. Its
        # own event is a cohort *member* — membership is a property of the facts
        # and the conditions, not of when the question was asked — and is
        # censored at every horizon, because the session it would be entered at
        # is after `as_of`. The statistics therefore come from the 25 earlier
        # gap dates, which is the right answer to "how has this pattern done"
        # asked by somebody about to trade the 26th.
        cls.as_of: date = cls.world.gap_sessions[-1]
        cls.now = datetime.combine(
            cls.as_of + timedelta(days=1), datetime.min.time()
        ).replace(hour=14)

        with get_session() as session:
            pf.synced_session(session, now=cls.now)
            session.commit()

        cls.settings = pf.settings()

    @classmethod
    def tearDownClass(cls):
        cls.db.cleanup()

    def setUp(self):
        # The real Phase 2 seam, bound the way `workspace.app` binds it, so the
        # gate resolves through `research_workspace.citations` to a stored
        # `ResolvedCitation` rather than through a stub that agrees with it.
        from research_workspace import citations as research_citations

        from workspace.tools import bind_citation_seam

        self.assertEqual(bind_citation_seam(), "research_workspace.citations")
        self.addCleanup(research_citations.clear_answer_resolver)

    # -- helpers ------------------------------------------------------------

    def ask(self, *, subject_ticker="", depth="full", as_of=None, parameters=None):
        """One `compare_setups`, committed, returning its `QueryOutcome` payload."""
        with get_session() as session:
            outcome = query_mod.answer_query(
                session,
                context=self.world.context,
                as_of=as_of or self.as_of,
                depth=depth,
                setup="gap_and_go_v1",
                parameters=(
                    {"horizons_sessions": list(HORIZONS)}
                    if parameters is None else parameters
                ),
                subject_ticker=subject_ticker,
                reps=REPS,
            )
            payload = outcome.payload()
            session.commit()
            return payload

    def propose(self, *, ticker="SY05", cohort_answer_id="", settings=None, **kwargs):
        params = dict(
            ticker=ticker,
            entry=ENTRY,
            stop=STOP,
            risk_fraction=RISK_FRACTION,
            expected_hold_sessions=5,
            cohort_answer_id=cohort_answer_id,
            settings=settings or self.settings,
            owner_id="99887766",
            now=self.now,
        )
        params.update(kwargs)
        with get_session() as session:
            row = proposals.create_proposal(session, **params)
            payload = proposals.proposal_payload(row)
            card = row.card_md or ""
            session.commit()
            return payload, card

    # -- the evidenced path -------------------------------------------------

    def test_a_qualifying_subject_produces_an_evidenced_proposal(self):
        """`compare_setups` -> `journal_append` -> `propose_order`, on stored rows."""
        answer = self.ask(subject_ticker="SY05")
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["depth"], "full")
        self.assertTrue(answer["citable"])
        self.assertEqual(
            answer["subject"],
            {
                "ticker": "SY05",
                "qualifies": True,
                "reason": "qualified",
                "event_date": self.as_of.isoformat(),
            },
        )

        # The interval Spec L §6.6 sizes from is on the answer, at 0.90, and is
        # a number this test reads rather than one it supplies.
        policy = answer["answer"]["horizons"][0]["policy"]
        self.assertEqual(float(policy["net_ci"]["level"]), 0.90)
        lower = float(policy["net_ci"]["lower"])
        point = float(policy["net"])
        self.assertGreater(lower, 0.0)
        self.assertGreater(point, lower)

        citation = answer["citation_id"]

        # Spec M: the decision is written down before the position, and the
        # `evidenced` label cannot be self-awarded — the citer re-checks it.
        from research_workspace.store import journal_append

        with get_session() as session:
            entry = journal_append(
                session,
                decision="opened",
                tickers=["SY05"],
                cohort_answer_id=citation,
                budget="evidenced",
                sizing_rationale="risk_fraction scaled by the policy net's lower 90% bound",
                note_md="end-to-end: the evidenced budget on a stored answer",
            )
            self.assertEqual(entry.budget, "evidenced")
            self.assertEqual(entry.cohort_answer_id, citation)
            self.assertTrue(entry.cohort_evidence_hash)
            session.commit()

        payload, card = self.propose(cohort_answer_id=citation)

        self.assertEqual(payload["status"], "proposed")
        self.assertEqual(payload["budget"], evidence.EVIDENCED)
        self.assertEqual(payload["evidence"]["cohort_answer_id"], citation)

        expected_m = min(1.0, max(0.0, lower / point))
        self.assertAlmostEqual(payload["evidence"]["m"], expected_m, places=9)
        self.assertAlmostEqual(payload["evidence"]["lower_bound"], lower, places=12)
        self.assertAlmostEqual(payload["evidence"]["point_estimate"], point, places=12)
        self.assertIn(payload["evidence"]["horizon_sessions"], HORIZONS)
        self.assertAlmostEqual(
            payload["risk_fraction_effective"], RISK_FRACTION * expected_m, places=9
        )
        self.assertGreater(payload["quantity"], 0)

        # And the card prints every field §6.6 names.
        self.assertIn("*budget:* `evidenced`", card)
        self.assertIn("*risk_fraction:*", card)
        self.assertIn(f"LB={lower:+.4f}", card)
        self.assertIn(f"PE={point:+.4f}", card)
        self.assertIn(f"horizon={payload['evidence']['horizon_sessions']}", card)
        self.assertIn(f"m={expected_m:.4f}", card)

    def test_the_evidenced_multiplier_shrinks_the_size_it_does_not_inflate_it(self):
        """`m = clip(LB/PE, 0, 1)`: evidence can only ever reduce the risk taken."""
        citation = self.ask(subject_ticker="SY05")["citation_id"]
        evidenced, _ = self.propose(cohort_answer_id=citation)
        uncited, _ = self.propose()

        self.assertLess(evidenced["evidence"]["m"], 1.0)
        self.assertLess(
            evidenced["risk_fraction_effective"], RISK_FRACTION,
            "an evidenced proposal is scaled down by its own uncertainty",
        )
        # Not a comparison of sizes: the uncited one draws from the *smaller*
        # discretionary cap, so it is smaller for a different reason entirely.
        self.assertEqual(uncited["budget"], evidence.DISCRETIONARY)
        self.assertIsNone(uncited["evidence"]["m"])

    def test_two_subjects_are_two_citations_over_one_set_of_statistics(self):
        """The cohort does not depend on the subject; the citation does."""
        first = self.ask(subject_ticker="SY05")
        second = self.ask(subject_ticker="SY00")
        self.assertNotEqual(first["citation_id"], second["citation_id"])
        self.assertEqual(first["answer"], second["answer"])
        self.assertTrue(second["subject"]["qualifies"] is False)

        # And asking the same question about the same subject again is served
        # from the cache, at the same citation id.
        again = self.ask(subject_ticker="SY05")
        self.assertTrue(again["cached"])
        self.assertEqual(again["citation_id"], first["citation_id"])
        self.assertEqual(again["subject"], first["subject"])

    # -- every way an answer is not evidence for this trade -----------------

    def test_a_subject_that_did_not_qualify_is_discretionary(self):
        """A real answer about a pattern this name is not an instance of."""
        answer = self.ask(subject_ticker="SY00")
        self.assertFalse(answer["subject"]["qualifies"])
        self.assertIn("condition_failed", answer["subject"]["reason"])

        payload, card = self.propose(ticker="SY00", cohort_answer_id=answer["citation_id"])
        self.assertEqual(payload["status"], "proposed")
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.SUBJECT_NOT_QUALIFIED, payload["evidence"]["reason"])
        self.assertIn("condition_failed", payload["evidence"]["reason"])
        self.assertIn("discretionary", card)

    def test_an_answer_about_another_name_is_discretionary(self):
        citation = self.ask(subject_ticker="SY05")["citation_id"]
        payload, _ = self.propose(ticker="SY06", cohort_answer_id=citation)
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.WRONG_TICKER, payload["evidence"]["reason"])

    def test_an_answer_with_no_subject_at_all_is_discretionary(self):
        """A question asked about the pattern is not evidence for a name."""
        answer = self.ask()
        self.assertIsNone(answer["subject"])
        payload, _ = self.propose(cohort_answer_id=answer["citation_id"])
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.WRONG_TICKER, payload["evidence"]["reason"])

    def test_a_quick_answer_is_discretionary(self):
        """`quick` structurally lacks the fields a citation needs (Spec N §8)."""
        answer = self.ask(subject_ticker="SY05", depth="quick")
        self.assertEqual(answer["depth"], "quick")
        self.assertFalse(answer["citable"])
        payload, _ = self.propose(cohort_answer_id=answer["citation_id"])
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.NOT_CITABLE, payload["evidence"]["reason"])

    def test_an_insufficient_answer_is_discretionary(self):
        """A refusal is a real finding and carries no statistic to cite."""
        answer = self.ask(
            subject_ticker="SY05",
            parameters={"gap_pct": 400.0, "horizons_sessions": list(HORIZONS)},
        )
        self.assertEqual(answer["status"], "insufficient")
        payload, _ = self.propose(cohort_answer_id=answer["citation_id"])
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.NOT_CITABLE, payload["evidence"]["reason"])

    def test_an_inconclusive_answer_is_discretionary(self):
        """`full` and resolvable, and still not evidence *for* the trade."""
        stored = self._store_variant(status="inconclusive")
        payload, _ = self.propose(cohort_answer_id=stored)
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.NOT_OK, payload["evidence"]["reason"])

    def test_a_stale_citation_is_discretionary(self):
        """Older than `CITATION_MAX_AGE_SESSIONS`: re-run compare_setups.

        Aged by moving the *answer's* `as_of`, not the clock: a proposal's age
        budget runs from the ledger's own `as_of_utc`, so winding `now` forward
        would refuse the proposal for a stale ledger and never reach the
        citation rule at all.
        """
        stored = self._store_variant(as_of_date=self.as_of - timedelta(days=30))
        payload, _ = self.propose(cohort_answer_id=stored)
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.STALE_CITATION, payload["evidence"]["reason"])

    def test_a_nonpositive_lower_bound_is_discretionary_and_prints_the_bound(self):
        """Advisory mode re-labels and shows the negative bound; strict zeroes it."""
        stored = self._store_variant(negative_lower_bound=True)

        advisory, card = self.propose(cohort_answer_id=stored)
        self.assertEqual(advisory["status"], "proposed")
        self.assertEqual(advisory["budget"], evidence.DISCRETIONARY)
        self.assertLess(advisory["evidence"]["lower_bound"], 0.0)
        self.assertIn(evidence.NONPOSITIVE_LOWER_BOUND, advisory["evidence"]["reason"])
        self.assertIn("LB=-", card)

        strict, _ = self.propose(
            cohort_answer_id=stored,
            settings=pf.settings(evidence_gate_mode="strict"),
        )
        self.assertEqual(strict["status"], "risk_rejected")
        self.assertEqual(strict["rejection_code"], EVIDENCE_SIZED_TO_ZERO)

    def test_an_answer_with_no_policy_interval_is_discretionary(self):
        """The gap this PR closed, asserted as a rule rather than a state."""
        stored = self._store_variant(drop_interval=True)
        payload, _ = self.propose(cohort_answer_id=stored)
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.NO_POLICY_BOUND, payload["evidence"]["reason"])
        # The point estimate is still printed: what is missing is the bound.
        self.assertIsNotNone(payload["evidence"]["point_estimate"])
        self.assertIsNone(payload["evidence"]["lower_bound"])

    def test_an_interval_at_another_level_is_not_the_lower_90_bound(self):
        """§6.6 names one number; a 95% bound is a different one, not a substitute."""
        stored = self._store_variant(interval_level=0.95)
        payload, _ = self.propose(cohort_answer_id=stored)
        self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
        self.assertIn(evidence.NO_POLICY_BOUND, payload["evidence"]["reason"])

    def test_nothing_sizes_to_zero_on_evidence_in_advisory_mode(self):
        """Spec L §6.6, stated as a property over every refusal above."""
        cases = {
            "no subject": self.ask()["citation_id"],
            "did not qualify": self.ask(subject_ticker="SY00")["citation_id"],
            "quick": self.ask(subject_ticker="SY05", depth="quick")["citation_id"],
            "no interval": self._store_variant(drop_interval=True),
            "negative bound": self._store_variant(negative_lower_bound=True),
            "unresolvable": "cohort:999999",
        }
        for label, citation in cases.items():
            with self.subTest(case=label):
                payload, _ = self.propose(cohort_answer_id=citation)
                self.assertEqual(payload["status"], "proposed", label)
                self.assertEqual(payload["budget"], evidence.DISCRETIONARY)
                self.assertGreater(payload["quantity"], 0, "an idea was suppressed")

    # -- a stored answer, edited where the rule has to bite ------------------

    def _store_variant(
        self,
        *,
        status: str | None = None,
        negative_lower_bound: bool = False,
        drop_interval: bool = False,
        interval_level: float | None = None,
        as_of_date: date | None = None,
    ) -> str:
        """Copy the real stored answer, change one thing, store it, cite it.

        The alternative — a hand-built answer object — would assert the gate
        against a shape the engine does not actually produce, which is exactly
        the mismatch this PR exists to remove. So the body here is the engine's
        own JSON with a single field moved, and every other field is whatever
        `compare_setups` wrote.
        """
        import copy
        import json

        from database.models import CohortAnswerRow

        base = self.ask(subject_ticker="SY05")
        with get_session() as session:
            row = session.get(
                CohortAnswerRow,
                cohort_citations.parse_citation(base["citation_id"]),
            )
            body = copy.deepcopy(row.answer)
            for horizon in body["horizons"]:
                policy = horizon["policy"]
                if drop_interval:
                    policy["net_ci"] = None
                elif negative_lower_bound:
                    policy["net_ci"]["lower"] = repr(-abs(float(policy["net"])) / 2.0)
                elif interval_level is not None:
                    policy["net_ci"]["level"] = repr(float(interval_level))
            # A family of its own, so a variant never becomes a *sibling* of
            # the real cohort: Spec N §6.4 shrinks an estimate toward its
            # family's pooled mean, and a doctored row joining that pool would
            # move the real answer's numbers from one test to the next.
            copied = CohortAnswerRow(
                setup_hash=row.setup_hash,
                family_slug=f"{row.family_slug}/variant",
                as_of_date=as_of_date or row.as_of_date,
                price_snapshot_id=row.price_snapshot_id,
                depth=row.depth,
                subject_ticker=row.subject_ticker,
                subject_qualifies=row.subject_qualifies,
                subject_reason=row.subject_reason,
                subject_event_date=row.subject_event_date,
                status=status or row.status,
                evidence_tier=row.evidence_tier,
                query_id=row.query_id,
                answer_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
                provenance_mix_json=row.provenance_mix_json,
                family_moments_json=row.family_moments_json,
            )
            # The unique key includes the subject, so a second row for the same
            # subject would collide. Give the variant its own setup hash: it is
            # a different stored answer, which is what it is.
            copied.setup_hash = f"{row.setup_hash[:56]}{len(body['horizons']):08d}"
            copied.setup_hash = _unique_hash(session, copied.setup_hash)
            session.add(copied)
            session.flush()
            citation = cohort_citations.citation_id(copied.id)
            session.commit()
        return citation


def _unique_hash(session, candidate: str) -> str:
    """A setup hash no stored answer is using yet, so the variant is its own row."""
    from database.models import CohortAnswerRow

    suffix = 0
    text = candidate
    while session.query(CohortAnswerRow).filter(
        CohortAnswerRow.setup_hash == text
    ).count():
        suffix += 1
        text = f"{candidate[:60]}{suffix:04d}"
    return text


if __name__ == "__main__":
    unittest.main()
