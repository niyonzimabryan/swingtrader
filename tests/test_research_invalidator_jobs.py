"""Spec M §4 and §8: the daily check job triggers, pages, and closes nothing.

The two rows this file exists for are ``test_invalidator_trigger_pages`` and
``test_never_auto_closes``. The second is the more important one: the value of
a system that watches a thesis comes entirely from it *not* acting on what it
sees.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from database.models import OrderEvent, Thesis, Trade
from research_workspace import invalidators, paging, store
from research_workspace.prices import SessionClose
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive


class StubPrices:
    """A price source with no network and no yfinance."""

    def __init__(self, closes_by_ticker):
        self.closes = closes_by_ticker
        self.calls = []

    def recent_closes(self, ticker, sessions):
        self.calls.append((ticker, sessions))
        series = self.closes.get(ticker.upper(), [])
        return series[-sessions:] if sessions else series


def closes(*prices, start=date(2026, 9, 1)):
    return [SessionClose(start + timedelta(days=i), p) for i, p in enumerate(prices)]


class StubSettings:
    def __init__(self, enabled=True):
        self.research_workspace_enabled = enabled


class InvalidatorJobTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("research_jobs")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

        self.pages = []

        def record(event):
            """A sink that accepts the page, as the Telegram one reports it did."""
            self.pages.append(event)
            return True

        previous = paging.set_pager(record)
        self.addCleanup(lambda: paging.set_pager(previous))
        self.addCleanup(invalidators.clear_event_matchers)
        self.settings = StubSettings()

    def active_thesis(self, **overrides):
        thesis = store.create_thesis(
            self.session,
            ticker=overrides.pop("ticker", "AMD"),
            title=overrides.pop("title", "MI400 ramp"),
            claim="Datacenter revenue doubles.",
            bear_case="One hyperscaler can walk.",
            **overrides,
        )
        store.set_probability(
            self.session,
            thesis,
            0.6,
            resolution_at=utcnow_naive().date() + timedelta(days=300),
            observable="FY27 datacenter revenue",
        )
        return thesis

    def run_job(self, price_source=None, now=None):
        return invalidators.run_daily_check(
            self.session,
            now=now,
            price_source=price_source,
            settings=self.settings,
        )


class PriceLevelTests(InvalidatorJobTestCase):
    def test_invalidator_trigger_pages(self):
        """A crossed `price_level` moves the thesis to `weakened` and pages."""
        thesis = self.active_thesis()
        row = store.add_invalidator(
            self.session,
            thesis,
            description="closes below $82 for three sessions",
            type="price_level",
            params={"operator": "below", "price": 82.0, "consecutive_sessions": 3},
        )
        store.activate(self.session, thesis)

        prices = StubPrices({"AMD": closes(90.0, 81.5, 80.2, 79.9)})
        report = self.run_job(price_source=prices)

        self.assertEqual([r.outcome for r in report.results], ["triggered"])
        self.assertEqual(thesis.status, "weakened")
        self.assertIsNotNone(thesis.weakened_at)
        self.assertEqual(row.status, "triggered")
        self.assertIn("81.5", row.triggered_reason)

        self.assertEqual(len(self.pages), 1)
        page = self.pages[0]
        self.assertEqual(page.kind, "invalidator_triggered")
        self.assertEqual(page.ticker, "AMD")
        self.assertEqual(page.invalidator_id, row.id)
        self.assertIn("weakened", page.as_message())
        self.assertEqual(report.pages_sent, 1)

    def test_a_single_close_below_does_not_trigger_a_three_session_rule(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis,
            description="closes below $82 for three sessions",
            type="price_level",
            params={"operator": "below", "price": 82.0, "consecutive_sessions": 3},
        )
        store.activate(self.session, thesis)

        report = self.run_job(price_source=StubPrices({"AMD": closes(90.0, 85.0, 81.0)}))
        self.assertEqual([r.outcome for r in report.results], ["not_triggered"])
        self.assertEqual(thesis.status, "active")
        self.assertEqual(self.pages, [])

    def test_missing_price_data_never_triggers(self):
        """A feed that is down leaves the thesis alone and says so."""
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level",
            params={"operator": "below", "price": 82.0, "consecutive_sessions": 3},
        )
        store.activate(self.session, thesis)

        report = self.run_job(price_source=StubPrices({"AMD": closes(70.0)}))
        self.assertEqual([r.outcome for r in report.results], ["insufficient_data"])
        self.assertEqual(thesis.status, "active")
        self.assertEqual(self.pages, [])

    def test_a_triggered_invalidator_is_not_re_paged_the_next_day(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)
        prices = StubPrices({"AMD": closes(80.0)})

        self.run_job(price_source=prices)
        second = self.run_job(price_source=prices)

        self.assertEqual(len(self.pages), 1)
        self.assertEqual(second.checked, 0)


class NeverAutoClosesTests(InvalidatorJobTestCase):
    def test_never_auto_closes(self):
        """A triggered invalidator creates no order and no proposal."""
        thesis = self.active_thesis(
            linked_position_ref="rh:AMD:2026-09-01",
            position_opened_at=utcnow_naive() - timedelta(days=5),
        )
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
            now=utcnow_naive() - timedelta(days=6),
        )
        store.activate(self.session, thesis)

        trades_before = self.session.query(Trade).count()
        orders_before = self.session.query(OrderEvent).count()

        report = self.run_job(price_source=StubPrices({"AMD": closes(80.0)}))

        self.assertEqual(len(report.triggered), 1)
        self.assertEqual(thesis.status, "weakened")
        # The position is untouched: no trade row, no order event, and the link
        # to the position is still just a reference.
        self.assertEqual(self.session.query(Trade).count(), trades_before)
        self.assertEqual(self.session.query(OrderEvent).count(), orders_before)
        self.assertEqual(thesis.linked_position_ref, "rh:AMD:2026-09-01")
        self.assertIsNone(thesis.closed_at)
        self.assertNotEqual(thesis.status, "closed")

    def test_the_page_says_no_position_was_changed(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)
        self.run_job(price_source=StubPrices({"AMD": closes(80.0)}))
        message = self.pages[0].as_message()
        self.assertIn("No position was changed", message)
        self.assertIn("no order or proposal was created", message)

    def test_the_check_module_cannot_reach_a_broker(self):
        """The import-graph half of the same guarantee.

        ``tests/test_no_execute_scope.py`` walks the whole closure; this is the
        cheap local assertion that fails in the same PR that would break it.
        """
        import research_workspace.invalidators as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("import execution", "from execution", "place_order",
                          "propose_order", "submit_order"):
            self.assertNotIn(forbidden, source)


class MetricThresholdTests(InvalidatorJobTestCase):
    def write_margin(self, value, period_end, known_at, *, cik="0000002488"):
        from filings.observations import Observation, write_observations

        write_observations(
            self.session,
            [
                Observation(
                    source="sec.companyfacts",
                    entity_cik=cik,
                    ticker_at_time="AMD",
                    fact_type="gross_margin",
                    valid_at=period_end,
                    known_at_utc=known_at,
                    known_at_source="acceptanceDateTime",
                    precision="second",
                    provenance_class="observed_live",
                    source_url="https://www.sec.gov/x",
                    source_trust="primary_regulator",
                    replay_eligible=True,
                    value_numeric=value,
                    unit="ratio",
                )
            ],
        )

    def test_two_quarters_below_the_threshold_trigger(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis,
            description="gross margin falls below 45% for two quarters",
            type="metric_threshold",
            params={
                "fact_type": "gross_margin",
                "operator": "below",
                "value": 0.45,
                "consecutive_periods": 2,
            },
        )
        store.activate(self.session, thesis)

        self.write_margin(0.51, datetime(2026, 3, 31, 20, 5), datetime(2026, 4, 28, 21, 3))
        self.write_margin(0.44, datetime(2026, 6, 30, 20, 5), datetime(2026, 7, 28, 21, 3))
        report = self.run_job(now=datetime(2026, 8, 1, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["not_triggered"])

        self.write_margin(0.42, datetime(2026, 9, 30, 20, 5), datetime(2026, 10, 27, 21, 3))
        report = self.run_job(now=datetime(2026, 11, 1, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["triggered"])
        self.assertEqual(thesis.status, "weakened")

    def test_a_fact_with_no_observations_reports_insufficient_data(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="margin below 45%",
            type="metric_threshold",
            params={"fact_type": "gross_margin", "operator": "below", "value": 0.45},
        )
        store.activate(self.session, thesis)
        report = self.run_job()
        self.assertEqual([r.outcome for r in report.results], ["insufficient_data"])
        self.assertEqual(thesis.status, "active")

    def test_a_restatement_supersedes_the_original_without_deleting_it(self):
        """Point-in-time by construction: the later-known row for a period wins."""
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="margin below 45%",
            type="metric_threshold",
            params={"fact_type": "gross_margin", "operator": "below", "value": 0.45},
        )
        store.activate(self.session, thesis)

        self.write_margin(0.44, datetime(2026, 6, 30, 20, 5), datetime(2026, 7, 28, 21, 3))
        self.write_margin(0.47, datetime(2026, 6, 30, 20, 5), datetime(2026, 8, 11, 18, 9))

        report = self.run_job(now=datetime(2026, 9, 1, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["not_triggered"])
        self.assertEqual(thesis.status, "active")


class TimeDecayTests(InvalidatorJobTestCase):
    def test_a_passed_deadline_triggers(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis,
            description="no re-rating within two quarters",
            type="time_decay",
            params={"deadline": "2026-09-01"},
        )
        store.activate(self.session, thesis)

        report = self.run_job(now=datetime(2026, 8, 20, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["not_triggered"])

        report = self.run_job(now=datetime(2026, 9, 2, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["triggered"])
        self.assertEqual(thesis.status, "weakened")

    def test_quarters_are_measured_from_the_position_open_date(self):
        opened = datetime(2026, 1, 15, 14, 30)
        thesis = self.active_thesis(position_opened_at=opened)
        row = store.add_invalidator(
            self.session, thesis, description="no re-rating within two quarters",
            type="time_decay", params={"quarters": 2}, now=opened - timedelta(days=1),
        )
        self.assertEqual(
            invalidators.deadline_for(row, thesis), date(2026, 1, 15) + timedelta(days=182)
        )

    def test_the_unless_observable_stops_a_deadline_from_paging(self):
        """The stated observable moved, so the deadline is not a weakening."""
        from filings.observations import Observation, write_observations

        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis,
            description="no re-rating within two quarters unless revenue doubles",
            type="time_decay",
            params={
                "deadline": "2026-09-01",
                "unless_metric": {
                    "fact_type": "datacenter_revenue",
                    "operator": "above",
                    "value": 10_000.0,
                },
            },
        )
        store.activate(self.session, thesis)
        write_observations(
            self.session,
            [
                Observation(
                    source="sec.companyfacts",
                    entity_cik="0000002488",
                    ticker_at_time="AMD",
                    fact_type="datacenter_revenue",
                    valid_at=datetime(2026, 6, 30, 20, 5),
                    known_at_utc=datetime(2026, 7, 28, 21, 3),
                    known_at_source="acceptanceDateTime",
                    precision="second",
                    provenance_class="observed_live",
                    source_url="https://www.sec.gov/x",
                    source_trust="primary_regulator",
                    replay_eligible=True,
                    value_numeric=12_500.0,
                    unit="USD_millions",
                )
            ],
        )
        report = self.run_job(now=datetime(2026, 9, 2, 12, 0))
        self.assertEqual([r.outcome for r in report.results], ["not_triggered"])
        self.assertIn("the stated observable moved", report.results[0].detail)
        self.assertEqual(thesis.status, "active")


class EventAndQualitativeTests(InvalidatorJobTestCase):
    def test_an_event_with_no_plane_reports_pending_rather_than_not_triggered(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="guidance cut",
            type="event", params={"event_key": "guidance_cut"},
        )
        store.activate(self.session, thesis)
        report = self.run_job()
        self.assertEqual([r.outcome for r in report.results], ["pending_plane"])
        self.assertEqual(thesis.status, "active")

    def test_a_registered_matcher_can_trigger(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="guidance cut",
            type="event", params={"event_key": "guidance_cut"},
        )
        store.activate(self.session, thesis)

        invalidators.register_event_matcher(
            "guidance_cut",
            lambda session, thesis, params, now: (True, "FY27 guidance cut in the 8-K"),
        )
        report = self.run_job()
        self.assertEqual([r.outcome for r in report.results], ["triggered"])
        self.assertEqual(thesis.status, "weakened")
        self.assertEqual(len(self.pages), 1)

    def test_post_hoc_invalidator_flagged_and_still_watched(self):
        """Flagged and excluded from metrics — not ignored by the job."""
        thesis = self.active_thesis(position_opened_at=utcnow_naive() - timedelta(days=2))
        row = store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)
        self.assertTrue(row.post_hoc)

        report = self.run_job(price_source=StubPrices({"AMD": closes(80.0)}))
        self.assertEqual(len(report.triggered), 1)

    def test_qualitative_invalidators_are_flagged_for_human_review(self):
        thesis = self.active_thesis()
        qualitative = store.add_invalidator(
            self.session, thesis, description="the AI capex narrative breaks",
            type="qualitative",
        )
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)

        report = self.run_job(price_source=StubPrices({"AMD": closes(95.0)}))
        flagged = [r.invalidator_id for r in report.needs_human_review]
        self.assertEqual(flagged, [qualitative.id])
        self.assertEqual(qualitative.status, "needs_human_review")
        self.assertEqual(self.pages, [])


class JobFlagTests(InvalidatorJobTestCase):
    def test_the_job_is_a_no_op_with_the_flag_off(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)

        self.settings = StubSettings(enabled=False)
        report = self.run_job(price_source=StubPrices({"AMD": closes(80.0)}))

        self.assertFalse(report.ran)
        self.assertEqual(report.checked, 0)
        self.assertEqual(thesis.status, "active")
        self.assertEqual(self.pages, [])

    def test_the_flag_defaults_off(self):
        from config.settings import Settings

        self.assertFalse(Settings.model_fields["research_workspace_enabled"].default)

    def test_a_draft_thesis_is_not_checked(self):
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        report = self.run_job(price_source=StubPrices({"AMD": closes(80.0)}))
        self.assertEqual(report.checked, 0)
        self.assertEqual(thesis.status, "draft")

    def test_the_job_makes_no_model_call(self):
        """Spec K §5: every scheduled job is pure Python.

        Asserted by patching the Anthropic client factory to fail loudly, so a
        model call anywhere under the job raises rather than costing money.
        """
        thesis = self.active_thesis()
        store.add_invalidator(
            self.session, thesis, description="closes below $82",
            type="price_level", params={"operator": "below", "price": 82.0},
        )
        store.activate(self.session, thesis)

        import research_workspace.invalidators as module

        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("anthropic", "Anthropic", "llm", "gemini"):
            self.assertNotIn(forbidden, source)
        self.run_job(price_source=StubPrices({"AMD": closes(95.0)}))


if __name__ == "__main__":
    unittest.main()
