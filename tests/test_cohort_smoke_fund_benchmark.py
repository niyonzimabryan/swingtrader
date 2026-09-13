"""`cohort_smoke` end to end with a **fund** as the benchmark (Spec N §11).

Spec N §11's definition of done asks for at least one `insufficient` and one
point-in-time answer, both readable. Until this change the roster could not be
run against a realistically-shaped world at all, because the benchmark every
abnormal return in §5.2 is measured against is SPY, SPY lives only in
Sharadar's `funds` table, and the adapter was equities-only — so
`COMPARABLE_BENCHMARK_SECURITY_UID` was unset and `CohortContext` refused to
construct.

This is that run, offline: the synthetic world from `tests/cohortfixture.py`
with its benchmark marked `asset_class="fund"`, which is the shape production
has. The two things it holds down are the two that a fund benchmark could
plausibly break:

1. the benchmark is **excluded from `liquid_us_equity_v1`** — a fund is never a
   universe member, so it cannot be a cohort member and a benchmark at once;
2. the cohort still finds its benchmark bars and answers, because exclusion
   from the universe is not exclusion from the price file.

`as_of` is `sessions[330]` and that is not arbitrary: it is where the roster
produces one `ok` and one `insufficient` in the same run, which is what §11
asks to see. Earlier and both refuse; at the end of the world both answer.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

import config.settings  # noqa: F401  (imported for the side effect below)
from data.prices import store, universes
from data.prices.base import ASSET_CLASS_EQUITY, ASSET_CLASS_FUND

from tests import cohortfixture
from tests.dbfixture import init_test_db

#: `config/__init__.py` binds a `Settings` **instance** as `config.settings`,
#: which shadows the submodule of the same name on the package. `sys.modules`
#: is therefore the only way to reach the module object to patch it — an
#: `import config.settings as cs` hands back the instance instead.
SETTINGS_MODULE = sys.modules["config.settings"]

#: See the module docstring: the session where the roster splits one/one.
SMOKE_SESSION_INDEX = 330


class CohortSmokeWithAFundBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = init_test_db("cohort_smoke_fund")
        from database.db import get_session

        with get_session() as session:
            cls.world = cohortfixture.seed_world(
                session, benchmark_asset_class=ASSET_CLASS_FUND
            )
            session.commit()
        cls.settings = cohortfixture.settings_for(cls.world, cls.db.url)

    @classmethod
    def tearDownClass(cls):
        cls.db.cleanup()

    def _run_smoke(self, *args) -> tuple[list[dict], str]:
        import scripts.cohort_smoke as smoke

        buffer = io.StringIO()
        with mock.patch.object(
            SETTINGS_MODULE, "Settings", lambda *a, **k: self.settings
        ):
            with contextlib.redirect_stdout(buffer):
                code = smoke.main([*args, "--json"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        rows, _end = json.JSONDecoder().raw_decode(text[text.index("[\n"):])
        return rows, text

    # -- the two properties a fund benchmark could break -------------------- #

    def test_the_benchmark_is_a_fund_and_is_not_a_universe_member(self):
        from database.db import get_session

        with get_session() as session:
            classes = store.asset_class_by_uid(session)
            members = {
                m.security_uid for m in store.members_as_of(
                    session, universes.UNIVERSE_SLUG, self.world.sessions[-1]
                )
            }
        self.assertEqual(classes[cohortfixture.BENCH_UID], ASSET_CLASS_FUND)
        self.assertNotIn(cohortfixture.BENCH_UID, members)
        self.assertTrue(members, "the equities must still be ranked")
        self.assertTrue(
            all(classes.get(uid) == ASSET_CLASS_EQUITY for uid in members),
            "every universe member must be an equity",
        )

    def test_the_benchmark_still_has_stored_bars(self):
        """Out of the universe is not out of the price file.

        `comparables/cohort.py` reads the benchmark's bars straight from
        `price_bars` by uid and raises if they are missing, so a filter that
        had reached the bars rather than the membership would fail here.
        """
        from database.db import get_session

        with get_session() as session:
            bars = store.load_bars(session, cohortfixture.BENCH_UID)
        self.assertEqual(len(bars), len(self.world.sessions))

    # -- Spec N §11 --------------------------------------------------------- #

    def test_the_roster_answers_with_one_ok_and_one_insufficient(self):
        as_of = self.world.sessions[SMOKE_SESSION_INDEX]
        rows, text = self._run_smoke("--as-of", as_of.isoformat(), "--depth", "quick")
        statuses = {row["setup"]: row["status"] for row in rows}

        self.assertIn("ok", statuses.values(), statuses)
        self.assertIn("insufficient", statuses.values(), statuses)
        # `insufficient` is a refusal with a reason attached, not a blank.
        for slug, status in statuses.items():
            if status == "insufficient":
                reason = next(r for r in rows if r["setup"] == slug)["refusal_reason"]
                self.assertTrue(reason, f"{slug} refused without saying why")
        self.assertIn("cohort smoke:", text)

    def test_an_ok_answer_is_point_in_time_and_citable(self):
        as_of = self.world.sessions[SMOKE_SESSION_INDEX]
        rows, _text = self._run_smoke("--as-of", as_of.isoformat(), "--depth", "quick")
        answered = [r for r in rows if r["status"] == "ok"]
        self.assertTrue(answered)
        for row in answered:
            self.assertIn(row["evidence_tier"], ("clean_pit", "vendor_pit"))
            self.assertTrue(row["citation_id"].startswith("cohort:"))
            self.assertTrue(row["build"]["universe_point_in_time"])

    def test_a_missing_benchmark_is_still_a_refusal_not_a_number(self):
        """The failure this whole change exists to end, still failing loudly.

        Spec N §5.2's abnormal return has no meaning without a benchmark, so a
        uid that names nothing must refuse rather than fall back to a price
        return. `--benchmark` overrides the setting, so this asks for one that
        was never stored.
        """
        as_of = self.world.sessions[SMOKE_SESSION_INDEX]
        rows, _text = self._run_smoke(
            "--as-of", as_of.isoformat(), "--depth", "quick",
            "--benchmark", "nope:not-a-security",
        )
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["status"], ("unavailable", "pending_plane"))
            self.assertTrue(row["refusal_reason"])


if __name__ == "__main__":
    unittest.main()
