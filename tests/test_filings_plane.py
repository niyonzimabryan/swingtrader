"""Phase 4 — the filings plane: Form 4, 13D/G, 8-K items, entity history, CUSIP.

The Spec O section 6 tests this file is responsible for:

``test_form4_transaction_types_distinguished``
``test_form4_codes_never_pooled``
``test_amendment_supersedes``
``test_unmapped_cusip_surfaced``
``test_ambiguous_cusip_surfaced``
``test_ticker_reuse_resolved_by_date``

13F is out of scope for Phase 4 (Spec O section 3.1), so
``test_13f_staleness_rendered`` and ``test_13f_requires_denominator`` are not
here — there is nothing to render.

Everything runs offline: SEC responses come from ``tests/fixtures/filings/``
and OpenFIGI responses from an inline handler, both through
``httpx.MockTransport``, so the throttle, headers, retry and decode all execute
without a network.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from database.db import get_session_factory
from database.models import EntityHistory, SourceObservation
from filings import client as sec_client
from filings import cusip as cusip_module
from filings import entities, eight_k, form4, ownership
from filings.api import filings_recent, insider_activity
from filings.client import SECClient
from filings.errors import AdapterSchemaError
from filings.observations import current_view, observations_known_at
from filings.openfigi_client import OpenFIGIClient, OpenFIGIError
from filings.plane import FilingsPlaneDisabled, ingest_filings
from filings.sec_minimal import parse_submissions
from tests.dbfixture import init_test_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "filings"

CIK_INSIDER = "0002000001"
CIK_TARGET = "0002000002"
CIK_OLD_TICKER_HOLDER = "0002000003"
CIK_NEW_TICKER_HOLDER = "0002000004"

USER_AGENT = "SwingTrader Test suite@example.invalid"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def submissions(cik: str):
    return load_json(f"submissions_CIK{cik}.json")


def company(cik: str):
    return parse_submissions(submissions(cik))


ACCESSION_INDEX = load_json("schedule13_index.json")

#: EDGAR calls every structured Schedule 13 cover page ``primary_doc.xml``, so
#: the fixture files are keyed by accession and mapped back here from the
#: archive folder in the URL.
SCHEDULE_DOCS = {
    ACCESSION_INDEX["sc_13d"].replace("-", ""): f"schedule13d_{ACCESSION_INDEX['sc_13d']}.xml",
    ACCESSION_INDEX["sc_13d_a"].replace("-", ""): f"schedule13d_{ACCESSION_INDEX['sc_13d_a']}.xml",
    ACCESSION_INDEX["sc_13g"].replace("-", ""): f"schedule13g_{ACCESSION_INDEX['sc_13g']}.xml",
}


def fixture_handler(request: httpx.Request) -> httpx.Response:
    """Serve the recorded fixtures by URL shape, exactly as EDGAR lays them out."""
    path = request.url.path
    leaf = path.rstrip("/").split("/")[-1]

    if path.startswith("/submissions/"):
        candidate = FIXTURES / sec_client.fixture_name_for_url(str(request.url))
        if candidate.exists():
            return httpx.Response(200, content=candidate.read_bytes())
        return httpx.Response(404, json={"error": "not recorded"})

    if leaf == "primary_doc.xml":
        # Every structured Schedule 13 cover page has this name; the archive
        # folder is the accession, which is what tells them apart.
        folder = path.rstrip("/").split("/")[-2]
        name = SCHEDULE_DOCS.get(folder)
        if name and (FIXTURES / name).exists():
            return httpx.Response(200, content=(FIXTURES / name).read_bytes())
        return httpx.Response(404, text="not recorded")

    candidate = FIXTURES / leaf
    if candidate.exists():
        return httpx.Response(200, content=candidate.read_bytes())
    return httpx.Response(404, json={"error": "not recorded"})


def fixture_transport() -> httpx.MockTransport:
    return httpx.MockTransport(fixture_handler)


def fixture_client(**kwargs) -> SECClient:
    kwargs.setdefault("transport", fixture_transport())
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return SECClient(USER_AGENT, **kwargs)


class EnabledSettings:
    plane_filings_enabled = True
    plane_sec_minimal_enabled = True
    sec_user_agent = USER_AGENT
    sec_max_requests_per_second = 10.0
    sec_request_timeout_s = 5.0
    sec_max_retries = 1
    openfigi_api_key = ""
    openfigi_timeout_s = 5.0


class DisabledSettings(EnabledSettings):
    plane_filings_enabled = False


NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 12, 31, tzinfo=timezone.utc)


class FilingsPlaneCase(unittest.TestCase):
    """One ingested company per test class, on a disposable database."""

    def setUp(self):
        self.db = init_test_db("filings_plane")
        self.session = get_session_factory()()
        self.addCleanup(self.db.cleanup)
        self.addCleanup(self.session.close)

    def ingest(self, cik: str, **kwargs):
        with fixture_client() as client:
            return ingest_filings(
                self.session,
                client,
                cik,
                settings=EnabledSettings(),
                observed_at=kwargs.pop("observed_at", NOW),
                **kwargs,
            )

    def rows(self, **kwargs):
        kwargs.setdefault("cutoff", LATER)
        return observations_known_at(self.session, **kwargs)


# ---------------------------------------------------------------------------
# Form 4 — the transaction codes
# ---------------------------------------------------------------------------


class Form4Tests(FilingsPlaneCase):
    def test_form4_transaction_types_distinguished(self):
        """An award and an open-market purchase are never pooled.

        Spec O section 6. The separation is structural: each code category has
        its own ``fact_type``, so there is no query that returns both unless it
        asks for both by name.
        """
        self.ingest(CIK_INSIDER)

        purchases = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["open_market_purchase"])
        awards = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["award"])
        exercises = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["exercise"])
        withholding = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["tax_withholding"])
        gifts = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["gift"])

        self.assertTrue(purchases, "the fixture has open-market purchases")
        self.assertTrue(awards, "the fixture has an award")
        self.assertTrue(exercises and withholding and gifts)

        self.assertEqual({r.payload["transaction_code"] for r in purchases}, {"P"})
        self.assertEqual({r.payload["transaction_code"] for r in awards}, {"A"})
        self.assertEqual({r.payload["transaction_code"] for r in exercises}, {"M"})
        self.assertEqual({r.payload["transaction_code"] for r in withholding}, {"F"})
        self.assertEqual({r.payload["transaction_code"] for r in gifts}, {"G"})

        # No fact type carries two categories.
        for fact_type in form4.ALL_FACT_TYPES:
            categories = {
                r.payload["transaction_category"] for r in self.rows(fact_type=fact_type)
            }
            self.assertLessEqual(len(categories), 1, f"{fact_type} pools categories")

    def test_form4_codes_never_pooled(self):
        """``P``/``S`` are never aggregated with ``A``/``M``/``F``/``G``.

        Spec O section 6, and the aggregation refuses rather than filters: a
        silent filter would make "insiders bought 17,000 shares" true of a set
        the caller believed also contained awards.
        """
        self.ingest(CIK_INSIDER)

        # `current_view` drops the row the 4/A supersedes, so the amended
        # purchase is counted once — at its amended size.
        open_market = current_view(self.rows(fact_type=sorted(form4.OPEN_MARKET_FACT_TYPES)))
        totals = form4.net_open_market_shares(open_market)
        self.assertEqual(totals.purchased_shares, 16500.0)   # 11500 (amended) + 5000
        self.assertEqual(totals.sold_shares, 13000.0)        # 8000 + 3000 + 2000
        self.assertEqual(totals.net_shares, 3500.0)

        for pooled in ("award", "exercise", "tax_withholding", "gift"):
            contaminated = open_market + self.rows(
                fact_type=form4.FACT_TYPE_BY_KEY[pooled]
            )
            with self.assertRaises(form4.PoolingRefused) as caught:
                form4.net_open_market_shares(contaminated)
            self.assertIn("never pooled", str(caught.exception))

    def test_form4_10b5_1_sale_is_flagged(self):
        """A planned sale is flagged; an unflagged one is unknown, not false.

        Verification claim 14 could not confirm the field-level representation
        of the 2023 checkbox, so the parser accepts the documented candidate
        elements and the footnote fallback, and records which one fired. The
        case that matters most is the last assertion: absence of evidence is
        stored as ``None``, because storing it as ``False`` would make a
        planned sale read as a discretionary one.
        """
        self.ingest(CIK_INSIDER)
        sales = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["open_market_sale"])
        by_owner = {r.payload["owner_cik"]: r for r in sales}

        element_flagged = by_owner["0002100004"]
        self.assertIs(element_flagged.payload["plan_10b5_1"], True)
        self.assertTrue(element_flagged.payload["plan_10b5_1_basis"].startswith("element:"))

        footnote_flagged = by_owner["0002100005"]
        self.assertIs(footnote_flagged.payload["plan_10b5_1"], True)
        self.assertTrue(footnote_flagged.payload["plan_10b5_1_basis"].startswith("footnote:"))
        self.assertIn(form4.WARN_10B5_1_FROM_FOOTNOTE, footnote_flagged.warnings)

        unflagged = by_owner["0002100006"]
        self.assertIsNone(unflagged.payload["plan_10b5_1"])
        self.assertIn(form4.WARN_10B5_1_UNKNOWN, unflagged.warnings)

        totals = form4.net_open_market_shares(sales)
        self.assertEqual(totals.planned_sales, 2)
        self.assertEqual(totals.planned_sale_shares, 11000.0)
        self.assertEqual(totals.unknown_plan_flag, 1)

    def test_known_at_is_acceptance_not_transaction_date(self):
        """A Form 4 is knowable when EDGAR accepted it, not when the trade was.

        The fixture's first purchase was made on 2026-03-02 and accepted at
        22:30 UTC on 2026-03-03 — after the close. Treating the transaction
        date as availability hands a backtest a day of the insider's own
        information; treating the *filing* date as availability hands it the
        rest of 2026-03-03.
        """
        self.ingest(CIK_INSIDER)
        row = self.rows(fact_type=form4.FACT_TYPE_BY_KEY["open_market_purchase"])[0]
        self.assertEqual(row.valid_at.date(), date(2026, 3, 2))
        self.assertEqual(
            row.known_at_utc, datetime(2026, 3, 3, 22, 30, 44)
        )
        self.assertEqual(row.payload["known_at_source"], "acceptanceDateTime")

        before = observations_known_at(
            self.session,
            fact_type=form4.FACT_TYPE_BY_KEY["open_market_purchase"],
            cutoff=datetime(2026, 3, 3, 20, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(before, [], "not knowable before acceptance")

    def test_unknown_transaction_code_raises(self):
        """A code outside SEC's table is a schema change, not an 'other'."""
        text = (FIXTURES / "form4_purchase.xml").read_text(encoding="utf-8")
        with self.assertRaises(AdapterSchemaError):
            form4.parse_form4(text.replace("<transactionCode>P<", "<transactionCode>QQ<"))

    def test_plane_flag_defaults_off(self):
        with fixture_client() as client:
            with self.assertRaises(FilingsPlaneDisabled):
                ingest_filings(
                    self.session, client, CIK_INSIDER, settings=DisabledSettings()
                )


# ---------------------------------------------------------------------------
# Amendments
# ---------------------------------------------------------------------------


class AmendmentTests(FilingsPlaneCase):
    def test_amendment_supersedes(self):
        """A ``4/A`` supersedes; the original stays queryable.

        Spec O section 6. The original is not deleted and not hidden: a cohort
        cutting off between the two must still see the un-amended number,
        because that is what was known then.
        """
        self.ingest(CIK_INSIDER)

        purchases = self.rows(
            fact_type=form4.FACT_TYPE_BY_KEY["open_market_purchase"],
            entity_cik=CIK_INSIDER,
        )
        pat = [r for r in purchases if r.payload["owner_cik"] == "0002100001"]
        self.assertEqual(len(pat), 2, "the original and its amendment both exist")

        original = next(r for r in pat if not r.payload["is_amendment"])
        amendment = next(r for r in pat if r.payload["is_amendment"])

        self.assertEqual(original.value_numeric, 12000.0)
        self.assertEqual(amendment.value_numeric, 11500.0)
        self.assertEqual(amendment.superseded_observation_id, original.id)
        self.assertIsNone(original.superseded_observation_id)
        self.assertGreater(amendment.known_at_utc, original.known_at_utc)

        # Before the amendment was accepted, the original is the answer.
        earlier = observations_known_at(
            self.session,
            fact_type=form4.FACT_TYPE_BY_KEY["open_market_purchase"],
            cutoff=datetime(2026, 3, 16, tzinfo=timezone.utc),
            entity_cik=CIK_INSIDER,
        )
        self.assertIn(original.id, [r.id for r in earlier])
        self.assertNotIn(amendment.id, [r.id for r in earlier])

    def test_schedule_13d_amendment_supersedes(self):
        """The same rule holds for an ``SC 13D/A``."""
        self.ingest(CIK_TARGET)
        events = self.rows(
            fact_type=ownership.FACT_TYPE_13D_EVENT, entity_cik=CIK_TARGET
        )
        self.assertEqual(len(events), 2)
        original = next(r for r in events if not r.payload["is_amendment"])
        amendment = next(r for r in events if r.payload["is_amendment"])
        self.assertEqual(amendment.superseded_observation_id, original.id)

    def test_reingest_is_idempotent(self):
        first = self.ingest(CIK_INSIDER)
        second = self.ingest(CIK_INSIDER)
        self.assertGreater(first.written, 0)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.duplicates, first.written)


# ---------------------------------------------------------------------------
# 13D/G
# ---------------------------------------------------------------------------


class ScheduleThirteenTests(FilingsPlaneCase):
    def test_13d_and_13g_are_distinct_fact_types(self):
        """Intent to influence is the difference, and it is not a payload note."""
        coverage = self.ingest(CIK_TARGET)
        self.assertEqual(coverage.schedule13_filings, 3)
        self.assertEqual(coverage.schedule13_structured, 3)

        d_events = self.rows(fact_type=ownership.FACT_TYPE_13D_EVENT)
        g_events = self.rows(fact_type=ownership.FACT_TYPE_13G_EVENT)
        self.assertEqual(len(d_events), 2)
        self.assertEqual(len(g_events), 1)
        self.assertTrue(all(r.payload["asserts_intent_to_influence"] for r in d_events))
        self.assertFalse(any(r.payload["asserts_intent_to_influence"] for r in g_events))

    def test_cover_page_facts_land_with_the_acceptance_stamp(self):
        self.ingest(CIK_TARGET)
        percents = self.rows(fact_type=ownership.FACT_TYPE_PERCENT_OF_CLASS)
        self.assertEqual(
            sorted(round(r.value_numeric, 2) for r in percents), [7.4, 9.3, 11.2]
        )
        for row in percents:
            self.assertEqual(row.payload["known_at_source"], "acceptanceDateTime")
            self.assertEqual(row.precision, "second")
            self.assertNotEqual(
                (row.known_at_utc.hour, row.known_at_utc.minute), (0, 0)
            )

    def test_unstructured_cover_page_still_produces_the_event(self):
        """A pre-2024 free-text cover page yields the event, never a regex guess.

        Extracting a percentage from free text is how a 4.9% passive stake
        becomes a 49% control position. The event row — "a 13D was accepted
        against this issuer at this instant" — is available for every filing
        ever made and is what lands instead.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("primary_doc.xml"):
                return httpx.Response(404, text="no structured cover page")
            return fixture_handler(request)

        with SECClient(
            USER_AGENT, transport=httpx.MockTransport(handler), sleeper=lambda _s: None
        ) as client:
            ingest_filings(
                self.session, client, CIK_TARGET, settings=EnabledSettings(),
                observed_at=NOW,
            )

        events = self.rows(fact_type=ownership.FACT_TYPE_13D_EVENT)
        self.assertEqual(len(events), 2)
        for row in events:
            self.assertIn(ownership.WARN_NO_STRUCTURED_COVER_PAGE, row.warnings)
            self.assertFalse(row.payload["structured_cover_page"])
        self.assertEqual(self.rows(fact_type=ownership.FACT_TYPE_PERCENT_OF_CLASS), [])


# ---------------------------------------------------------------------------
# 8-K item index
# ---------------------------------------------------------------------------


class EightKItemTests(FilingsPlaneCase):
    def test_items_beyond_202_are_indexed(self):
        coverage = self.ingest(CIK_INSIDER)
        self.assertEqual(
            set(coverage.eight_k_items),
            {"2.02", "5.02", "1.01", "7.01", "4.02", "1.05", "9.99"},
        )
        rows = self.rows(fact_type=eight_k.FACT_TYPE_EIGHT_K_ITEM)
        titles = {r.value_text: r.payload["item_title"] for r in rows}
        self.assertEqual(titles["4.02"], eight_k.EIGHT_K_ITEMS["4.02"])
        self.assertEqual(titles["1.05"], eight_k.EIGHT_K_ITEMS["1.05"])

    def test_unknown_item_is_kept_and_flagged(self):
        """SEC adds items. A dropped one is an event that never happened."""
        self.ingest(CIK_INSIDER)
        unknown = [
            r for r in self.rows(fact_type=eight_k.FACT_TYPE_EIGHT_K_ITEM)
            if r.value_text == "9.99"
        ]
        self.assertEqual(len(unknown), 1)
        self.assertIn(eight_k.WARN_UNKNOWN_ITEM, unknown[0].warnings)
        self.assertIsNone(unknown[0].payload["item_title"])

    def test_exhibit_item_is_not_an_event(self):
        """9.01 rides along with almost every 8-K; it selects for 'filed an 8-K'."""
        self.ingest(CIK_INSIDER)
        codes = {r.value_text for r in self.rows(fact_type=eight_k.FACT_TYPE_EIGHT_K_ITEM)}
        self.assertNotIn("9.01", codes)

    def test_phase_3a_earnings_row_is_untouched(self):
        """The Item 2.02 fact type Phase 3b reads keeps its own name and source."""
        self.assertNotEqual(
            eight_k.FACT_TYPE_EIGHT_K_ITEM, "earnings_release_8k_item_202"
        )
        self.assertNotEqual(eight_k.SOURCE_EIGHT_K_ITEMS, "sec_8k_item_202")


# ---------------------------------------------------------------------------
# Entity history
# ---------------------------------------------------------------------------


class EntityHistoryTests(FilingsPlaneCase):
    def test_ticker_reuse_resolved_by_date(self):
        """A recycled ticker resolves to the right CIK for the event date.

        Spec O section 6. ``RCYC`` was held by ``0002000003`` until 2019 and by
        ``0002000004`` from 2021. A current snapshot answers "0002000004" for a
        2018 event, with no error and no warning, and every price join
        downstream is then against the wrong company.
        """
        # Two snapshots, taken at different times — which is exactly how a
        # snapshot-observed interval gets its end date.
        entities.record_intervals(
            self.session,
            [
                entities.Interval(
                    entity_cik=CIK_OLD_TICKER_HOLDER, attribute=entities.ATTRIBUTE_TICKER,
                    value="RCYC", valid_from=date(2015, 1, 5), valid_to=None,
                    basis=entities.BASIS_SNAPSHOT, valid_from_is_first_observation=True,
                ),
            ],
            observed_at=datetime(2015, 1, 5, tzinfo=timezone.utc),
        )
        entities.record_intervals(
            self.session,
            [
                entities.Interval(
                    entity_cik=CIK_OLD_TICKER_HOLDER, attribute=entities.ATTRIBUTE_TICKER,
                    value="OLDR", valid_from=date(2019, 7, 1), valid_to=None,
                    basis=entities.BASIS_SNAPSHOT, valid_from_is_first_observation=True,
                ),
                entities.Interval(
                    entity_cik=CIK_NEW_TICKER_HOLDER, attribute=entities.ATTRIBUTE_TICKER,
                    value="RCYC", valid_from=date(2021, 3, 1), valid_to=None,
                    basis=entities.BASIS_SNAPSHOT, valid_from_is_first_observation=True,
                ),
            ],
            observed_at=datetime(2021, 3, 1, tzinfo=timezone.utc),
        )

        in_2018 = entities.cik_for_ticker(self.session, "RCYC", date(2018, 5, 9))
        self.assertEqual(in_2018.value, CIK_OLD_TICKER_HOLDER)

        in_2022 = entities.cik_for_ticker(self.session, "RCYC", date(2022, 7, 14))
        self.assertEqual(in_2022.value, CIK_NEW_TICKER_HOLDER)

        # And the gap between the two eras is honestly empty, not filled in.
        in_2020 = entities.cik_for_ticker(self.session, "RCYC", date(2020, 6, 1))
        self.assertFalse(in_2020.resolved)
        self.assertEqual(in_2020.warning, "ticker_unknown_at_date")

    def test_former_names_give_dated_intervals(self):
        """EDGAR states ``from``/``to`` on ``formerNames``; those beat our snapshots."""
        self.ingest(CIK_OLD_TICKER_HOLDER)
        old = entities.name_at(self.session, CIK_OLD_TICKER_HOLDER, date(2015, 6, 1))
        self.assertEqual(old.value, "Recycled Ticker Corp")
        self.assertEqual(old.basis, entities.BASIS_FORMER_NAMES)

        current = entities.name_at(self.session, CIK_OLD_TICKER_HOLDER, date(2024, 6, 1))
        self.assertEqual(current.value, "Recycled Ticker Holdings Inc")

    def test_resolution_before_first_observation_refuses_rather_than_guesses(self):
        """A snapshot's ``valid_from`` is an upper bound, not the mapping's start."""
        self.ingest(CIK_INSIDER, observed_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        before = entities.ticker_at(self.session, CIK_INSIDER, date(2020, 1, 2))
        self.assertFalse(before.resolved)
        self.assertEqual(before.warning, entities.WARN_TICKER_UNRESOLVED)

        after = entities.ticker_at(self.session, CIK_INSIDER, date(2026, 7, 1))
        self.assertEqual(after.value, "INSD")

    def test_snapshot_change_closes_the_open_interval(self):
        entities.record_intervals(
            self.session,
            [entities.Interval(CIK_INSIDER, entities.ATTRIBUTE_TICKER, "AAAA",
                               date(2024, 1, 2), None, entities.BASIS_SNAPSHOT,
                               valid_from_is_first_observation=True)],
            observed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        entities.record_intervals(
            self.session,
            [entities.Interval(CIK_INSIDER, entities.ATTRIBUTE_TICKER, "BBBB",
                               date(2025, 6, 2), None, entities.BASIS_SNAPSHOT,
                               valid_from_is_first_observation=True)],
            observed_at=datetime(2025, 6, 2, tzinfo=timezone.utc),
        )
        rows = (
            self.session.query(EntityHistory)
            .filter(EntityHistory.entity_cik == CIK_INSIDER)
            .order_by(EntityHistory.valid_from)
            .all()
        )
        closed = next(r for r in rows if r.value == "AAAA")
        self.assertEqual(closed.valid_to, date(2025, 6, 2))
        self.assertEqual(
            entities.ticker_at(self.session, CIK_INSIDER, date(2024, 8, 1)).value, "AAAA"
        )
        self.assertEqual(
            entities.ticker_at(self.session, CIK_INSIDER, date(2025, 8, 1)).value, "BBBB"
        )

    def test_phase_3a_snapshot_warnings_can_be_resolved(self):
        """The warning Phase 3a stamps on every row is answerable now.

        Dry run by default: it reports coverage and changes nothing. With
        ``apply=True`` the ticker is corrected and the warning dropped, and
        rows whose date no interval covers keep theirs.
        """
        from filings.observations import Observation, write_observations

        write_observations(self.session, [
            Observation(
                source="sec_xbrl_companyfacts",
                entity_cik=CIK_INSIDER,
                ticker_at_time="STALE",
                fact_type="revenue",
                valid_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                known_at_utc=datetime(2026, 8, 3, 21, 7, 31, tzinfo=timezone.utc),
                known_at_source="acceptanceDateTime",
                precision="second",
                provenance_class="vendor_pit",
                source_url="https://www.sec.gov/x-index.htm",
                source_trust="primary_regulator",
                replay_eligible=True,
                value_numeric=1.0,
                quality_warnings=(entities.WARN_TICKER_SNAPSHOT,),
            )
        ])
        # Observed before the filings, so history covers every row's date.
        self.ingest(CIK_INSIDER, observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        dry = entities.resolve_snapshot_warnings(self.session)
        self.assertEqual(dry.examined, 1, "only the seeded Phase 3a row is stamped")
        self.assertEqual(dry.resolved, 1)
        self.assertFalse(dry.applied)
        still = self.session.query(SourceObservation).filter(
            SourceObservation.fact_type == "revenue"
        ).one()
        self.assertIn(entities.WARN_TICKER_SNAPSHOT, still.warnings)

        applied = entities.resolve_snapshot_warnings(self.session, apply=True)
        self.assertEqual(applied.resolved, 1)
        self.session.expire_all()
        fixed = self.session.query(SourceObservation).filter(
            SourceObservation.fact_type == "revenue"
        ).one()
        self.assertEqual(fixed.ticker_at_time, "INSD")
        self.assertNotIn(entities.WARN_TICKER_SNAPSHOT, fixed.warnings)


# ---------------------------------------------------------------------------
# CUSIP resolution
# ---------------------------------------------------------------------------


UNMAPPED_CUSIP = "000000ZZ9"
AMBIGUOUS_CUSIP = "111111AA1"
MAPPED_CUSIP = "222222BB2"
NON_US_CUSIP = "333333CC3"


def openfigi_transport() -> httpx.MockTransport:
    """OpenFIGI's positional result array, with the three outcomes in it."""
    responses = {
        UNMAPPED_CUSIP: {"warning": "No identifier found."},
        AMBIGUOUS_CUSIP: {
            "data": [
                {"figi": "BBG000AMBG01", "ticker": "AMBA", "name": "Ambiguous Corp Class A",
                 "exchCode": "UN", "securityType": "Common Stock"},
                {"figi": "BBG000AMBG02", "ticker": "AMBB", "name": "Ambiguous Corp Class B",
                 "exchCode": "UN", "securityType": "Common Stock"},
            ]
        },
        MAPPED_CUSIP: {
            "data": [
                {"figi": "BBG000MAPD01", "ticker": "MAPD", "name": "Mapped Corp",
                 "exchCode": "UW", "securityType": "Common Stock"},
                {"figi": "BBG000MAPD02", "ticker": "MAPD", "name": "Mapped Corp",
                 "exchCode": "US", "securityType": "Common Stock"},
            ]
        },
        NON_US_CUSIP: {
            "data": [
                {"figi": "BBG000FRGN01", "ticker": "FRGN", "name": "Foreign Corp",
                 "exchCode": "LN", "securityType": "Common Stock"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json=[responses.get(job["idValue"], {"error": "unknown"}) for job in jobs],
        )

    return httpx.MockTransport(handler)


class CusipTests(unittest.TestCase):
    def resolve(self, cusips):
        with OpenFIGIClient(
            "", transport=openfigi_transport(), sleeper=lambda _s: None
        ) as client:
            return cusip_module.resolve_cusips(client, cusips)

    def test_unmapped_cusip_surfaced(self):
        """An unmappable holding appears in warnings and is **not** dropped.

        Spec O section 6. A position you cannot name is still a position;
        dropping it understates a portfolio in a way nothing downstream can
        detect, which is how a "top holdings" view quietly lies.
        """
        batch = self.resolve([UNMAPPED_CUSIP, MAPPED_CUSIP])
        self.assertIn(UNMAPPED_CUSIP, batch.resolutions)
        unmapped = batch.resolutions[UNMAPPED_CUSIP]
        self.assertEqual(unmapped.status, cusip_module.STATUS_UNMAPPED)
        self.assertIsNone(unmapped.ticker)
        self.assertIn(unmapped, batch.warnings)
        self.assertEqual(batch.unmapped, 1)
        self.assertEqual(len(batch.resolutions), 2, "every input has an entry")

    def test_ambiguous_cusip_surfaced(self):
        """Several FIGIs land in warnings with all candidates, not a silent pick.

        Spec O section 6. Picking the first result is right most of the time,
        which is exactly what makes the wrong times invisible.
        """
        batch = self.resolve([AMBIGUOUS_CUSIP])
        resolution = batch.resolutions[AMBIGUOUS_CUSIP]
        self.assertEqual(resolution.status, cusip_module.STATUS_AMBIGUOUS)
        self.assertIsNone(resolution.ticker, "no pick is made")
        self.assertEqual(
            sorted(c.ticker for c in resolution.candidates), ["AMBA", "AMBB"]
        )
        self.assertEqual(resolution.warning, cusip_module.WARN_CUSIP_AMBIGUOUS)
        self.assertIn(resolution, batch.warnings)

    def test_one_ticker_across_venues_is_not_ambiguous(self):
        """The same ticker on two US venues is one security, not two candidates."""
        batch = self.resolve([MAPPED_CUSIP])
        resolution = batch.resolutions[MAPPED_CUSIP]
        self.assertEqual(resolution.status, cusip_module.STATUS_MAPPED)
        self.assertEqual(resolution.ticker, "MAPD")

    def test_non_us_listing_is_unmapped_not_silently_used(self):
        """A foreign venue sharing a symbol is not a US-listed resolution."""
        batch = self.resolve([NON_US_CUSIP])
        resolution = batch.resolutions[NON_US_CUSIP]
        self.assertEqual(resolution.status, cusip_module.STATUS_UNMAPPED)
        self.assertIn("no US-listed venue", resolution.detail)

    def test_batch_size_follows_the_published_limit(self):
        """10 jobs keyless, 100 with a key — the client will not exceed either."""
        keyless = OpenFIGIClient("", transport=openfigi_transport())
        keyed = OpenFIGIClient("k" * 8, transport=openfigi_transport())
        self.assertEqual(keyless.max_jobs, 10)
        self.assertEqual(keyed.max_jobs, 100)
        with self.assertRaises(OpenFIGIError):
            keyless.map_jobs([{"idType": "ID_CUSIP", "idValue": MAPPED_CUSIP}] * 11)
        keyless.close()
        keyed.close()

    def test_misshapen_result_array_raises(self):
        """Positional alignment is the contract; losing it is not absorbable."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": []}])

        with OpenFIGIClient("", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(OpenFIGIError):
                client.map_jobs([
                    {"idType": "ID_CUSIP", "idValue": MAPPED_CUSIP},
                    {"idType": "ID_CUSIP", "idValue": UNMAPPED_CUSIP},
                ])


# ---------------------------------------------------------------------------
# The read API
# ---------------------------------------------------------------------------


class FilingsApiTests(FilingsPlaneCase):
    def test_filings_recent_returns_a_provenance_block(self):
        self.ingest(CIK_INSIDER)
        answer = filings_recent(self.session, entity_cik=CIK_INSIDER, as_of=LATER)
        self.assertTrue(answer["rows"])
        block = answer["provenance"]
        self.assertIn("as_of_utc", block)
        self.assertIn("sources", block)
        self.assertIn("staleness", block)
        self.assertIn("data_quality", block)
        self.assertIn(form4.SOURCE_FORM4, block["sources"])

    def test_insider_activity_keeps_the_categories_apart(self):
        self.ingest(CIK_INSIDER)
        answer = insider_activity(
            self.session, entity_cik=CIK_INSIDER, as_of=datetime(2026, 4, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(answer["open_market"]["purchased_shares"], 16500.0)
        self.assertIn(
            form4.FACT_TYPE_BY_KEY["award"], answer["not_pooled_with_open_market"]
        )
        self.assertNotIn("insider_award", str(answer["open_market"]))
        self.assertTrue(answer["purchase_clusters"], "two insiders bought within 30 days")
        self.assertEqual(answer["purchase_clusters"][0]["distinct_insiders"], 2)

    def test_cohort_cutoff_excludes_later_filings(self):
        self.ingest(CIK_INSIDER)
        early = filings_recent(
            self.session, entity_cik=CIK_INSIDER,
            as_of=datetime(2026, 3, 4, tzinfo=timezone.utc),
        )
        late = filings_recent(self.session, entity_cik=CIK_INSIDER, as_of=LATER)
        self.assertLess(len(early["rows"]), len(late["rows"]))


if __name__ == "__main__":
    unittest.main()
