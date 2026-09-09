"""Phase 3a — the minimum SEC ingestion contract.

The named tests from the Spec O section 6 test plan that Phase 3a is
responsible for:

``test_xbrl_known_at_from_acceptance``
``test_filing_date_never_known_at``
``test_xbrl_alias_coverage_alert``
``test_8k_202_timestamp``
``test_market_cap_has_share_source``
``test_adapter_schema_change_fails_loudly``
``test_day_precision_known_at_close``

Everything runs offline: SEC responses come from ``tests/fixtures/sec/``
through an ``httpx.MockTransport``, so the client's throttle, headers, retry
and decode all execute without a network.
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from database.db import get_session
from database.models import SourceObservation
from filings import client as sec_client
from filings.client import (
    SEC_MAX_REQUESTS_PER_SECOND,
    SECClient,
    SECClientError,
    SECRateLimitError,
    validate_user_agent,
)
from filings.errors import AdapterSchemaError, PlaneDisabled
from filings.observations import (
    PRECISION_DAY,
    PRECISION_SECOND,
    PROVENANCE_ARCHIVAL,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    LedgerWriteRejected,
    Observation,
    day_precision_known_at,
    observations_known_at,
    parse_acceptance_datetime,
    write_observations,
)
from filings.sec_minimal import (
    FACT_TYPE_EARNINGS_RELEASE,
    SOURCE_COMPANYFACTS,
    SOURCE_EIGHT_K_INDEX,
    WARN_SHARES_MULTI_CLASS,
    eight_k_observations,
    fetch_company,
    ingest_company,
    load_ticker_map,
    market_cap_as_of,
    parse_submissions,
    shares_outstanding_as_of,
    xbrl_observations,
)
from filings.xbrl_aliases import (
    FACT_TYPES,
    coverage_alerts,
    facts_for,
    has_continuous_series,
)
from tests.dbfixture import init_test_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec"

CIK_TAG_MIGRATION = "0001000001"
CIK_GAP = "0001000002"
CIK_DUAL_CLASS = "0001000003"

USER_AGENT = "SwingTrader Test suite@example.invalid"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def submissions(cik: str):
    return load_fixture(f"submissions_CIK{cik}.json")


def companyfacts(cik: str):
    return load_fixture(f"companyfacts_CIK{cik}.json")


def company(cik: str):
    return parse_submissions(submissions(cik))


def fixture_transport(overrides: dict | None = None) -> httpx.MockTransport:
    """Serve the recorded fixtures by the last path segment of the URL."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = sec_client.fixture_name_for_url(str(request.url))
        if name in overrides:
            return httpx.Response(200, json=overrides[name])
        path = FIXTURES / name
        if not path.exists():
            return httpx.Response(404, json={"error": "not recorded"})
        return httpx.Response(200, content=path.read_bytes())

    return httpx.MockTransport(handler)


def fixture_client(**kwargs) -> SECClient:
    kwargs.setdefault("transport", fixture_transport())
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return SECClient(USER_AGENT, **kwargs)


class EnabledSettings:
    plane_sec_minimal_enabled = True
    sec_user_agent = USER_AGENT
    sec_max_requests_per_second = 10.0
    sec_request_timeout_s = 5.0
    sec_max_retries = 2


class DisabledSettings(EnabledSettings):
    plane_sec_minimal_enabled = False


def sample_observation(**overrides) -> Observation:
    base = dict(
        source=SOURCE_COMPANYFACTS,
        entity_cik=CIK_TAG_MIGRATION,
        fact_type="revenue",
        valid_at=datetime(2019, 12, 31, tzinfo=timezone.utc),
        known_at_utc=datetime(2020, 2, 3, 21, 7, 31, tzinfo=timezone.utc),
        known_at_source="acceptanceDateTime",
        precision=PRECISION_SECOND,
        provenance_class=PROVENANCE_VENDOR_PIT,
        source_url="https://www.sec.gov/Archives/edgar/data/1000001/x-index.htm",
        source_trust=TRUST_PRIMARY_REGULATOR,
        replay_eligible=True,
        value_numeric=4_000_000_000.0,
        unit="USD",
    )
    base.update(overrides)
    return Observation(**base)


# ---------------------------------------------------------------------------
# The stop-condition tests
# ---------------------------------------------------------------------------


class KnownAtTests(unittest.TestCase):
    """``known_at_utc`` is the acceptance timestamp, and only that."""

    def test_xbrl_known_at_from_acceptance(self):
        """Every companyfacts row is stamped with its filing's acceptance time.

        Spec O section 6. The join is the whole point-in-time claim: without it
        an XBRL fact has no defensible availability time at all.
        """
        subject = company(CIK_TAG_MIGRATION)
        acceptance = subject.acceptance_index()
        build = xbrl_observations(companyfacts(CIK_TAG_MIGRATION), subject)

        self.assertTrue(build.observations)
        self.assertFalse(build.unresolved_accessions)

        for obs in build.observations:
            filing = acceptance[obs.accession]
            self.assertEqual(obs.known_at_utc, filing.acceptance)
            self.assertEqual(obs.known_at_source, "acceptanceDateTime")
            self.assertEqual(obs.precision, PRECISION_SECOND)
            self.assertEqual(obs.provenance_class, PROVENANCE_VENDOR_PIT)
            self.assertTrue(obs.replay_eligible)
            # The acceptance stamp is a real time of day, not a promoted date.
            self.assertNotEqual(
                (obs.known_at_utc.hour, obs.known_at_utc.minute, obs.known_at_utc.second),
                (0, 0, 0),
            )

    def test_filing_date_never_known_at(self):
        """A filing date can never become a ``known_at_utc``.

        Three ways it could be smuggled in, all refused: naming the field,
        naming a different field but handing over a midnight timestamp, and
        naming a non-EDGAR field on an EDGAR source.
        """
        filing = next(
            f for f in company(CIK_TAG_MIGRATION).filings if f.form.startswith("10-")
        )
        midnight = datetime.combine(filing.filing_date, datetime.min.time(), tzinfo=timezone.utc)

        with self.assertRaises(LedgerWriteRejected) as named:
            sample_observation(known_at_source="filingDate", known_at_utc=midnight)
        self.assertIn("filingDate", str(named.exception))

        with self.assertRaises(LedgerWriteRejected) as disguised:
            sample_observation(known_at_utc=midnight)
        self.assertIn("midnight", str(disguised.exception))

        with self.assertRaises(LedgerWriteRejected):
            sample_observation(known_at_source="filed", known_at_utc=midnight)

        with self.assertRaises(LedgerWriteRejected):
            sample_observation(known_at_source="reportDate")

        # And the honest version of the same filing is accepted.
        accepted = sample_observation(known_at_utc=filing.acceptance)
        self.assertEqual(accepted.known_at_utc, filing.acceptance)

    def test_filing_date_of_an_after_close_filing_is_a_different_day_of_trading(self):
        """The concrete leak claim 13 documented, on the fixture set.

        A filing dated D but accepted at 22:30 UTC was not available at any
        point during D's session. Keying on the date makes it look like it was.
        """
        late = [
            f
            for f in company(CIK_TAG_MIGRATION).filings
            if f.acceptance.hour >= 22 and f.acceptance.date() == f.filing_date
        ]
        self.assertTrue(late, "fixture should contain an after-the-close acceptance")
        for filing in late:
            session_close = datetime.combine(
                filing.filing_date, datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(hours=20)  # 16:00 ET in winter
            self.assertGreater(filing.acceptance, session_close)


class EightKTests(unittest.TestCase):
    def test_8k_202_timestamp(self):
        """An earnings event is stamped at its Item 2.02 8-K's acceptance."""
        subject = company(CIK_TAG_MIGRATION)
        observations = eight_k_observations(subject)
        self.assertTrue(observations)

        by_accession = subject.acceptance_index()
        for obs in observations:
            filing = by_accession[obs.accession]
            self.assertTrue(filing.form.upper().startswith("8-K"))
            self.assertIn("2.02", filing.items)
            self.assertEqual(obs.fact_type, FACT_TYPE_EARNINGS_RELEASE)
            self.assertEqual(obs.source, SOURCE_EIGHT_K_INDEX)
            self.assertEqual(obs.known_at_utc, filing.acceptance)
            self.assertEqual(obs.valid_at, filing.acceptance)
            self.assertEqual(obs.precision, PRECISION_SECOND)
            self.assertEqual(obs.known_at_source, "acceptanceDateTime")

    def test_non_earnings_8ks_and_other_forms_are_not_earnings_events(self):
        subject = company(CIK_TAG_MIGRATION)
        emitted = {obs.accession for obs in eight_k_observations(subject)}

        other_8k = [
            f for f in subject.filings
            if f.form.upper().startswith("8-K") and "2.02" not in f.items
        ]
        self.assertTrue(other_8k, "fixture should contain a non-earnings 8-K")
        for filing in other_8k:
            self.assertNotIn(filing.accession, emitted)

        form_4s = [f for f in subject.filings if f.form == "4"]
        self.assertTrue(form_4s, "fixture should contain a Form 4")
        for filing in form_4s:
            self.assertNotIn(filing.accession, emitted)


class AliasCoverageTests(unittest.TestCase):
    def test_xbrl_alias_coverage_alert(self):
        """A tag migration yields a continuous series *and* an alert."""
        facts = companyfacts(CIK_TAG_MIGRATION)
        alerts = coverage_alerts(facts, "revenue", CIK_TAG_MIGRATION)
        kinds = [alert.kind for alert in alerts]

        self.assertIn("tag_migration", kinds)
        migration = next(a for a in alerts if a.kind == "tag_migration")
        self.assertEqual(migration.context["from_tag"], "Revenues")
        self.assertEqual(
            migration.context["to_tag"],
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        )

        # Continuous: the migration is not a gap.
        self.assertNotIn("series_gap", kinds)
        self.assertTrue(has_continuous_series(facts, "revenue", CIK_TAG_MIGRATION))

        merged, tags_used = facts_for(facts, "revenue")
        self.assertEqual(len(tags_used), 2)
        ends = sorted({fact.period_end for fact in merged})
        for previous, current in zip(ends, ends[1:]):
            self.assertLessEqual((current - previous).days, 130)

    def test_a_real_gap_is_reported_as_a_gap_not_absorbed(self):
        facts = companyfacts(CIK_GAP)
        alerts = coverage_alerts(facts, "revenue", CIK_GAP)
        gaps = [a for a in alerts if a.kind == "series_gap"]
        self.assertEqual(len(gaps), 1)
        self.assertGreater(gaps[0].context["days"], 130)
        self.assertFalse(has_continuous_series(facts, "revenue", CIK_GAP))

    def test_reading_one_tag_alone_would_have_shown_a_false_gap(self):
        """The bias the alias map exists to remove, demonstrated."""
        facts = companyfacts(CIK_TAG_MIGRATION)
        single_tag = facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        ends = sorted(date.fromisoformat(entry["end"]) for entry in single_tag)
        merged, _ = facts_for(facts, "revenue")
        self.assertLess(len(ends), len({f.period_end for f in merged}))

    def test_a_company_reporting_no_alias_is_no_coverage_not_silence(self):
        facts = deepcopy(companyfacts(CIK_TAG_MIGRATION))
        facts["facts"]["us-gaap"].pop("Revenues", None)
        facts["facts"]["us-gaap"].pop(
            "RevenueFromContractWithCustomerExcludingAssessedTax", None
        )
        alerts = coverage_alerts(facts, "revenue", CIK_TAG_MIGRATION)
        self.assertEqual([a.kind for a in alerts], ["no_coverage"])

    def test_every_alias_chain_is_well_formed(self):
        for fact_type, spec in FACT_TYPES.items():
            self.assertEqual(spec.fact_type, fact_type)
            self.assertTrue(spec.tags, fact_type)
            self.assertEqual(len(set(spec.tags)), len(spec.tags), fact_type)
            self.assertTrue(spec.units, fact_type)
            self.assertIn(spec.taxonomy, {"us-gaap", "dei"}, fact_type)


class AdapterStrictnessTests(unittest.TestCase):
    def test_adapter_schema_change_fails_loudly(self):
        """An unexpected payload shape raises; it never becomes a null row."""
        subject = company(CIK_TAG_MIGRATION)

        # 1. A fact entry that lost its accession number.
        facts = deepcopy(companyfacts(CIK_TAG_MIGRATION))
        del facts["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][0]["accn"]
        with self.assertRaises(AdapterSchemaError) as missing_accn:
            xbrl_observations(facts, subject)
        self.assertIn("accn", str(missing_accn.exception))

        # 2. A value that stopped being numeric.
        facts = deepcopy(companyfacts(CIK_TAG_MIGRATION))
        facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]["val"] = None
        with self.assertRaises(AdapterSchemaError):
            xbrl_observations(facts, subject)

        # 3. The whole facts block renamed.
        facts = deepcopy(companyfacts(CIK_TAG_MIGRATION))
        facts["financial_facts"] = facts.pop("facts")
        with self.assertRaises(AdapterSchemaError):
            xbrl_observations(facts, subject)

        # 4. submissions field arrays that no longer line up positionally.
        payload = deepcopy(submissions(CIK_TAG_MIGRATION))
        payload["filings"]["recent"]["items"].pop()
        with self.assertRaises(AdapterSchemaError) as ragged:
            parse_submissions(payload)
        self.assertIn("same length", str(ragged.exception))

        # 5. acceptanceDateTime gone, which is the field the plane exists for.
        payload = deepcopy(submissions(CIK_TAG_MIGRATION))
        payload["filings"]["recent"].pop("acceptanceDateTime")
        with self.assertRaises(AdapterSchemaError):
            parse_submissions(payload)

        # 6. acceptanceDateTime present but null.
        payload = deepcopy(submissions(CIK_TAG_MIGRATION))
        payload["filings"]["recent"]["acceptanceDateTime"][0] = None
        with self.assertRaises(LedgerWriteRejected):
            parse_submissions(payload)

        # 7. acceptanceDateTime that lost its UTC marker.
        payload = deepcopy(submissions(CIK_TAG_MIGRATION))
        payload["filings"]["recent"]["acceptanceDateTime"][0] = "2020-02-03 21:07:31"
        with self.assertRaises(LedgerWriteRejected):
            parse_submissions(payload)

    def test_an_observation_with_no_value_is_refused(self):
        with self.assertRaises(LedgerWriteRejected) as caught:
            sample_observation(value_numeric=None, value_text=None)
        self.assertIn("numeric or a text value", str(caught.exception))

    def test_an_unknown_precision_or_provenance_class_is_refused(self):
        with self.assertRaises(LedgerWriteRejected):
            sample_observation(precision="minute")
        with self.assertRaises(LedgerWriteRejected):
            sample_observation(provenance_class="probably_fine")

    def test_a_shares_fact_carrying_a_period_is_a_shape_change(self):
        facts = deepcopy(companyfacts(CIK_TAG_MIGRATION))
        entries = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        entries[0]["start"] = "2019-01-01"
        with self.assertRaises(AdapterSchemaError):
            facts_for(facts, "shares_outstanding")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_ledger")
        self.addCleanup(self.db.cleanup)

    def _write(self, observations):
        with get_session() as session:
            return write_observations(session, observations)

    def test_day_precision_known_at_close(self):
        """A day-precision fact is invisible to a 10:00 cutoff on its date."""
        vintage_day = date(2024, 3, 15)
        observation = sample_observation(
            source="fred_alfred",
            fact_type="cpi_yoy",
            known_at_source="vintage_date",
            known_at_utc=day_precision_known_at(vintage_day),
            precision=PRECISION_DAY,
            provenance_class=PROVENANCE_ARCHIVAL,
            replay_eligible=False,
            source_url="https://alfred.stlouisfed.org/series?seid=CPIAUCSL",
            source_trust="statistical_agency",
            valid_at=datetime(2024, 2, 29, tzinfo=timezone.utc),
            value_numeric=3.2,
            unit="percent",
        )
        self._write([observation])

        morning = datetime(2024, 3, 15, 10, 0, tzinfo=timezone.utc)
        end_of_day = datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)
        next_day = datetime(2024, 3, 16, 9, 30, tzinfo=timezone.utc)

        with get_session() as session:
            self.assertEqual(
                observations_known_at(session, fact_type="cpi_yoy", cutoff=morning), []
            )
            self.assertEqual(
                len(observations_known_at(session, fact_type="cpi_yoy", cutoff=end_of_day)), 0
            )
            self.assertEqual(
                len(observations_known_at(session, fact_type="cpi_yoy", cutoff=next_day)), 1
            )

    def test_day_precision_known_at_is_the_end_of_its_day(self):
        stamp = day_precision_known_at(date(2024, 3, 15))
        self.assertEqual(stamp.date(), date(2024, 3, 15))
        self.assertEqual((stamp.hour, stamp.minute), (23, 59))
        self.assertEqual(stamp.tzinfo, timezone.utc)

    def test_writes_are_idempotent(self):
        subject = company(CIK_TAG_MIGRATION)
        observations = eight_k_observations(subject)

        first = self._write(observations)
        self.assertEqual(first.inserted, len(observations))
        self.assertEqual(first.duplicates, 0)

        second = self._write(observations)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, len(observations))

        with get_session() as session:
            self.assertEqual(session.query(SourceObservation).count(), len(observations))

    def test_a_restatement_is_a_new_row_beside_the_original(self):
        original = sample_observation(value_numeric=4_000_000_000.0)
        restated = sample_observation(
            value_numeric=3_950_000_000.0,
            accession="0001000001-20-000999",
            known_at_utc=datetime(2020, 8, 4, 20, 15, 3, tzinfo=timezone.utc),
        )
        self._write([original, restated])

        before = datetime(2020, 5, 1, tzinfo=timezone.utc)
        after = datetime(2021, 1, 1, tzinfo=timezone.utc)
        with get_session() as session:
            self.assertEqual(
                [row.value_numeric for row in observations_known_at(
                    session, fact_type="revenue", cutoff=before)],
                [4_000_000_000.0],
            )
            self.assertEqual(
                len(observations_known_at(session, fact_type="revenue", cutoff=after)), 2
            )

    def test_stored_rows_carry_precision_provenance_and_a_source_url(self):
        self._write(eight_k_observations(company(CIK_TAG_MIGRATION)))
        with get_session() as session:
            rows = session.query(SourceObservation).all()
            self.assertTrue(rows)
            for row in rows:
                self.assertIn(row.precision, {"second", "day"})
                self.assertIn(
                    row.provenance_class,
                    {"observed_live", "vendor_pit", "archival_reconstructed"},
                )
                self.assertTrue(row.source_url.startswith("https://www.sec.gov/"))
                self.assertEqual(row.source_trust, TRUST_PRIMARY_REGULATOR)
                self.assertIsNotNone(row.known_at_utc)
                # filingDate is kept for audit and is not the timestamp.
                self.assertIn("filing_date", row.payload)
                self.assertEqual(row.payload["known_at_source"], "acceptanceDateTime")


class MarketCapTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_market_cap")
        self.addCleanup(self.db.cleanup)
        subject = company(CIK_DUAL_CLASS)
        build = xbrl_observations(companyfacts(CIK_DUAL_CLASS), subject)
        self.shares = [o for o in build.observations if o.fact_type == "shares_outstanding"]
        with get_session() as session:
            write_observations(session, self.shares)
        self.first_known = min(o.known_at_utc for o in self.shares)

    def test_market_cap_has_share_source(self):
        """No known share count at a date means no market cap at that date."""
        before_any = self.first_known - timedelta(days=1)
        with get_session() as session:
            self.assertIsNone(
                shares_outstanding_as_of(
                    session, entity_cik=CIK_DUAL_CLASS, as_of=before_any
                )
            )
            self.assertIsNone(
                market_cap_as_of(
                    session, entity_cik=CIK_DUAL_CLASS, price=100.0, as_of=before_any
                )
            )

        after = self.first_known + timedelta(seconds=1)
        with get_session() as session:
            cap = market_cap_as_of(
                session, entity_cik=CIK_DUAL_CLASS, price=100.0, as_of=after
            )
            self.assertIsNotNone(cap)
            self.assertEqual(cap.value, 100.0 * cap.shares_outstanding)
            self.assertLessEqual(cap.shares_known_at_utc, after.replace(tzinfo=None))
            self.assertIsNotNone(cap.shares_accession)

        # A company with nothing in the ledger has no market cap either.
        with get_session() as session:
            self.assertIsNone(
                market_cap_as_of(
                    session, entity_cik="0009999999", price=100.0, as_of=after
                )
            )

    def test_dual_class_share_counts_are_summed_and_flagged(self):
        observation = self.shares[0]
        self.assertEqual(observation.payload["share_class_count"], 2)
        self.assertEqual(
            observation.value_numeric, sum(observation.payload["share_class_values"])
        )
        self.assertIn(WARN_SHARES_MULTI_CLASS, observation.quality_warnings)

    def test_a_single_class_share_count_is_not_flagged(self):
        build = xbrl_observations(
            companyfacts(CIK_TAG_MIGRATION), company(CIK_TAG_MIGRATION)
        )
        single = [o for o in build.observations if o.fact_type == "shares_outstanding"]
        self.assertTrue(single)
        for observation in single:
            self.assertEqual(observation.payload["share_class_count"], 1)
            self.assertNotIn(WARN_SHARES_MULTI_CLASS, observation.quality_warnings)


# ---------------------------------------------------------------------------
# Client, flag and end-to-end
# ---------------------------------------------------------------------------


class ClientTests(unittest.TestCase):
    def test_user_agent_must_identify_a_contact(self):
        with self.assertRaises(SECClientError):
            validate_user_agent("")
        with self.assertRaises(SECClientError):
            validate_user_agent("SwingTrader")
        self.assertEqual(validate_user_agent(USER_AGENT), USER_AGENT)

    def test_no_real_contact_address_is_hardcoded_in_the_plane(self):
        """The User-Agent comes from settings; the default is empty."""
        from config.settings import Settings

        self.assertEqual(Settings.model_fields["sec_user_agent"].default, "")
        source = (Path(__file__).resolve().parents[1] / "filings" / "client.py").read_text()
        self.assertNotIn("@swingtrader", source)
        self.assertNotIn("@gmail", source)

    def test_the_published_rate_limit_is_a_ceiling(self):
        with self.assertRaises(SECClientError):
            SECClient(USER_AGENT, max_requests_per_second=SEC_MAX_REQUESTS_PER_SECOND + 1)
        with self.assertRaises(SECClientError):
            SECClient(USER_AGENT, max_requests_per_second=0)

    def test_requests_are_spaced_to_the_configured_rate(self):
        slept: list[float] = []
        ticks = iter([0.0] * 40)

        client = SECClient(
            USER_AGENT,
            max_requests_per_second=10.0,
            transport=fixture_transport(),
            clock=lambda: next(ticks),
            sleeper=slept.append,
        )
        self.addCleanup(client.close)
        for _ in range(3):
            client.get_json(sec_client.submissions_url(CIK_TAG_MIGRATION))

        # A frozen clock means every request after the first has to wait a
        # full interval.
        self.assertEqual(len(slept), 2)
        for delay in slept:
            self.assertAlmostEqual(delay, 0.1, places=6)

    def test_every_request_sends_the_user_agent(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("user-agent", ""))
            return httpx.Response(200, json={"ok": True})

        client = SECClient(
            USER_AGENT, transport=httpx.MockTransport(handler), sleeper=lambda _s: None
        )
        self.addCleanup(client.close)
        client.get_json("https://data.sec.gov/anything.json")
        self.assertEqual(seen, [USER_AGENT])

    def test_back_pressure_is_retried_then_surfaced(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(429, json={"error": "slow down"})

        client = SECClient(
            USER_AGENT,
            transport=httpx.MockTransport(handler),
            max_retries=2,
            sleeper=lambda _s: None,
        )
        self.addCleanup(client.close)
        with self.assertRaises(SECRateLimitError):
            client.get_json("https://data.sec.gov/anything.json")
        self.assertEqual(attempts["n"], 3)

    def test_a_transient_503_recovers(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        client = SECClient(
            USER_AGENT, transport=httpx.MockTransport(handler), sleeper=lambda _s: None
        )
        self.addCleanup(client.close)
        self.assertEqual(client.get_json("https://data.sec.gov/x.json"), {"ok": True})

    def test_a_missing_companyfacts_document_is_absence_not_failure(self):
        client = fixture_client()
        self.addCleanup(client.close)
        self.assertIsNone(
            client.get_json_or_none(sec_client.companyfacts_url("0009999999"))
        )

    def test_cik_normalisation(self):
        self.assertEqual(sec_client.normalise_cik(320193), "0000320193")
        self.assertEqual(sec_client.normalise_cik("CIK0000320193"), "0000320193")
        with self.assertRaises(SECClientError):
            sec_client.normalise_cik("AAPL")


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_flag")
        self.addCleanup(self.db.cleanup)

    def test_the_plane_flag_defaults_off(self):
        from config.settings import Settings

        self.assertIs(Settings.model_fields["plane_sec_minimal_enabled"].default, False)

    def test_ingest_refuses_while_the_flag_is_off(self):
        client = fixture_client()
        self.addCleanup(client.close)
        with get_session() as session:
            with self.assertRaises(PlaneDisabled):
                ingest_company(
                    session, client, CIK_TAG_MIGRATION, settings=DisabledSettings()
                )
            self.assertEqual(session.query(SourceObservation).count(), 0)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_e2e")
        self.addCleanup(self.db.cleanup)
        self.client = fixture_client()
        self.addCleanup(self.client.close)

    def test_ingest_writes_all_three_feeds_and_reports_coverage(self):
        with get_session() as session:
            coverage = ingest_company(
                session, self.client, CIK_TAG_MIGRATION, settings=EnabledSettings()
            )

        self.assertTrue(coverage.ok)
        self.assertGreater(coverage.written, 0)
        self.assertEqual(coverage.duplicates, 0)
        self.assertTrue(coverage.has_core_series)
        self.assertGreater(coverage.share_count_rows, 0)
        self.assertGreater(coverage.eight_k_item_202, 0)
        self.assertGreater(coverage.eight_k_total, coverage.eight_k_item_202)
        self.assertEqual(coverage.unresolved_accessions, 0)
        self.assertIn("tag_migration", {a.kind for a in coverage.alerts})

        with get_session() as session:
            sources = {row[0] for row in session.query(SourceObservation.source).distinct()}
        self.assertEqual(sources, {SOURCE_COMPANYFACTS, SOURCE_EIGHT_K_INDEX})

    def test_a_second_ingest_writes_nothing_new(self):
        for _ in range(2):
            with get_session() as session:
                coverage = ingest_company(
                    session, self.client, CIK_GAP, settings=EnabledSettings()
                )
        self.assertEqual(coverage.written, 0)
        self.assertGreater(coverage.duplicates, 0)

    def test_since_is_an_acceptance_cutoff(self):
        cutoff = date(2019, 1, 1)
        with get_session() as session:
            ingest_company(
                session,
                self.client,
                CIK_TAG_MIGRATION,
                settings=EnabledSettings(),
                since=cutoff,
            )
        with get_session() as session:
            rows = session.query(SourceObservation).all()
            self.assertTrue(rows)
            for row in rows:
                self.assertGreaterEqual(row.known_at_utc.date(), cutoff)

    def test_fetch_company_reads_both_documents_through_the_client(self):
        feeds = fetch_company(self.client, CIK_DUAL_CLASS)
        self.assertEqual(feeds.cik, CIK_DUAL_CLASS)
        self.assertEqual(feeds.company.primary_ticker, "DUAL")
        self.assertIsNotNone(feeds.companyfacts_payload)
        self.assertEqual(self.client.request_count, 2)

    def test_ticker_map_resolves_the_fixture_universe(self):
        mapping = load_ticker_map(self.client)
        self.assertEqual(mapping["TAGM"], CIK_TAG_MIGRATION)
        self.assertEqual(mapping["DUAL"], CIK_DUAL_CLASS)


class NoModelInThisPathTests(unittest.TestCase):
    """Spec: never write code that lets a model produce a statistic."""

    def test_the_filings_package_imports_no_model_client(self):
        import ast

        forbidden = ("anthropic", "google", "genai", "openai", "firecrawl", "perplexity")
        package = Path(__file__).resolve().parents[1] / "filings"
        offenders = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    root = name.split(".")[0].lower()
                    if root in forbidden or root in {"agents", "scoring", "execution"}:
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
