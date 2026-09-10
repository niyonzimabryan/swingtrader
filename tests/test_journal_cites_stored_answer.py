"""`journal_append` cites a stored Phase 3c answer on the same rule as an in-memory one.

Phase 2's citer accepted only a `comparables.report` answer object; Phase 3c's
registry resolves an id to a `ResolvedCitation` of the stored row. Both must
refuse `quick`, `insufficient` and `inconclusive`, and accept `full`/`ok`.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import date

from comparables.citations import ResolvedCitation
from research_workspace.store import ResearchRefused, cite_cohort_answer


def _resolved(depth: str, status: str, **answer) -> ResolvedCitation:
    return ResolvedCitation(
        citation_id="cohort:7",
        answer_id=7,
        query_id=3,
        setup_hash="a" * 64,
        family_slug="liquid_us_equity_v1/gap",
        as_of=date(2026, 9, 1),
        depth=depth,
        status=status,
        evidence_tier="vendor_pit",
        answer={"depth": depth, "status": status, **answer},
        archival_block=None,
        provenance_mix={"vendor_pit": 40},
    )


class StoredCitationTests(unittest.TestCase):
    def test_full_ok_is_cited_with_the_stored_hash(self):
        resolved = _resolved("full", "ok", n=40)
        cited = cite_cohort_answer(resolved)
        self.assertEqual(cited.cohort_answer_id, "cohort:7")
        expected = hashlib.sha256(
            json.dumps(resolved.answer, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(cited.evidence_hash, expected)

    def test_quick_insufficient_and_inconclusive_are_refused(self):
        for depth, status in (("quick", "ok"), ("full", "insufficient"), ("full", "inconclusive")):
            with self.subTest(depth=depth, status=status):
                with self.assertRaises(ResearchRefused) as ctx:
                    cite_cohort_answer(_resolved(depth, status, refusal_reason="3 dates"))
                self.assertEqual(ctx.exception.code, "uncitable_cohort_answer")

    def test_seam_binds_to_phase_two_registry(self):
        from research_workspace import citations
        from workspace.tools import bind_citation_seam

        try:
            self.assertEqual(bind_citation_seam(), "research_workspace.citations")
            self.assertTrue(citations.has_resolver())
            # Not a cohort citation at all: unresolvable, never a guess.
            self.assertIsNone(citations.resolve("full-ok@1"))
        finally:
            citations.clear_answer_resolver()


if __name__ == "__main__":
    unittest.main()
