"""The delisting-audit resolver (Spec N §12 ruling): symbol resolution,
`unresolved`/`out_of_window` classification, the testable-minimum refusal, and
that `comparables` readers of the stored audit tolerate the two new classes.

`tests/test_price_plane.py::DelistingAuditTests` is untouched — this file adds
coverage rather than changing what already passes.
"""

from __future__ import annotations

import unittest
from datetime import date

from data.prices.audit import (
    CLASSIFICATIONS,
    DEFAULT_MIN_TESTABLE_CASES,
    InsufficientTestableCasesError,
    Resolution,
    classify_case,
    resolve_case,
    run_audit,
)
from data.prices.base import (
    DailyBar,
    PricePlane,
    SecurityMasterRow,
    normalize_company_name,
)
from data.prices.delisting_audit_list import DelistingCase
from tests.dbfixture import init_test_db
from tests.test_price_plane import _sharadar_tables, _StubClient, _StubResponse

SOURCE = "stub"


def _bar(ticker: str, day: date, close: float) -> DailyBar:
    return DailyBar(
        security_uid=f"{SOURCE}:{ticker}",
        ticker=ticker,
        session_date=day,
        raw_open=close, raw_high=close, raw_low=close, raw_close=close,
        volume=1_000.0,
        split_factor=1.0,
        dividend_cash=0.0,
        split_adjusted_close=close,
        total_return_close=close,
        source=SOURCE,
    )


def _declining_series(ticker: str, start: date, sessions: int, terminal_return: float) -> tuple[DailyBar, ...]:
    """`sessions + 1` bars, flat at 100 except the last one, whose return over
    the whole window is exactly `terminal_return`."""
    bars = [_bar(ticker, date.fromordinal(start.toordinal() + i), 100.0) for i in range(sessions)]
    bars.append(_bar(ticker, date.fromordinal(start.toordinal() + sessions), 100.0 * (1 + terminal_return)))
    return tuple(bars)


def _security(ticker: str, name: str) -> SecurityMasterRow:
    return SecurityMasterRow(security_uid=f"{SOURCE}:{ticker}", ticker=ticker, source=SOURCE, name=name)


def _case(ticker: str = "OLDCO", company: str = "Old Company Inc", **kwargs) -> DelistingCase:
    defaults = dict(
        delisting_date=date(2020, 1, 15),
        venue="nyse_amex",
        event="Chapter 11",
        source_kind="sec_form_25",
        source_url="https://www.sec.gov/example",
    )
    defaults.update(kwargs)
    return DelistingCase(ticker, company, **defaults)


class StubPlane(PricePlane):
    """A minimal, fully controllable `PricePlane` for resolver tests."""

    source = SOURCE

    def __init__(self, bars=None, master=None, name_search_rows=(), supports_search=True):
        self._bars = bars or {}
        self._master = {row.ticker: row for row in (master or ())}
        self._name_search_rows = name_search_rows
        self.supports_company_name_search = supports_search
        self.daily_bars_calls: list[str] = []
        self.find_by_company_name_calls: list[tuple[str, str | None]] = []

    def daily_bars(self, ticker, start=None, end=None):
        self.daily_bars_calls.append(ticker)
        return self._bars.get(ticker, ())

    def corporate_actions(self, ticker, start=None, end=None):
        return ()

    def security_master(self, tickers=None):
        if tickers is None:
            return tuple(self._master.values())
        return tuple(self._master[t] for t in tickers if t in self._master)

    def index_membership(self, universe_slug):
        return ()

    def find_by_company_name(self, company, *, original_ticker=None):
        self.find_by_company_name_calls.append((company, original_ticker))
        return self._name_search_rows


class ResolverTests(unittest.TestCase):
    def test_explicit_vendor_symbol_wins_over_everything_else(self):
        case = _case(vendor_symbols={SOURCE: "NEWQ"})
        plane = StubPlane(
            bars={"NEWQ": (_bar("NEWQ", date(2020, 1, 1), 100.0),)},
            master=[_security("OLDCO", "Some Other Name")],  # would also resolve, but must not win
        )
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution, Resolution("NEWQ", "explicit", candidates=("NEWQ",)))

    def test_security_master_hit_resolves_the_ticker_as_is(self):
        case = _case()
        plane = StubPlane(master=[_security("OLDCO", "Anything")])
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution.resolution, "security_master")
        self.assertEqual(resolution.resolved_symbol, "OLDCO")

    def test_name_match_resolves_via_relatedtickers_or_name_scan(self):
        case = _case(ticker="OLDCO", company="Old Company Inc")
        plane = StubPlane(
            name_search_rows=({"ticker": "NEWQ", "name": "OLD COMPANY, INCORPORATED"},),
        )
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution.resolution, "name_match")
        self.assertEqual(resolution.resolved_symbol, "NEWQ")
        self.assertEqual(plane.find_by_company_name_calls, [("Old Company Inc", "OLDCO")])

    def test_a_name_mismatch_is_rejected_not_guessed(self):
        """A candidate search can return rows; only a strict name match is accepted."""
        case = _case(ticker="OLDCO", company="Old Company Inc")
        plane = StubPlane(
            name_search_rows=({"ticker": "NEWQ", "name": "Completely Different Corp"},),
        )
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution.resolution, "unresolved")
        self.assertIsNone(resolution.resolved_symbol)
        self.assertEqual(resolution.candidates, ("NEWQ",))

    def test_no_candidates_at_all_is_unresolved(self):
        case = _case()
        plane = StubPlane()
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution, Resolution(None, "unresolved", candidates=()))

    def test_a_plane_with_no_search_capability_falls_back_to_asis(self):
        """No `security_master` hit, no name-search capability: use the ticker
        as given rather than manufacturing an `unresolved` this plane never
        actually investigated (preserves `FixturePricePlane`'s behaviour)."""
        case = _case()
        plane = StubPlane(supports_search=False)
        resolution = resolve_case(plane, case)
        self.assertEqual(resolution, Resolution("OLDCO", "asis"))
        self.assertEqual(plane.find_by_company_name_calls, [])

    def test_unresolved_case_never_reaches_daily_bars(self):
        case = _case()
        plane = StubPlane()
        result = classify_case(plane, case)
        self.assertEqual(result.classification, "unresolved")
        self.assertIsNone(result.resolved_symbol)
        self.assertEqual(plane.daily_bars_calls, [])

    def test_out_of_window_case_is_never_resolved_or_queried(self):
        case = _case(delisting_date=date(2015, 1, 1))
        plane = StubPlane(master=[_security("OLDCO", "Old Company Inc")])
        result = classify_case(plane, case, history_start=date(2016, 9, 12))
        self.assertEqual(result.classification, "out_of_window")
        self.assertEqual(result.resolution, "unresolved")
        self.assertIsNone(result.resolved_symbol)
        self.assertEqual(plane.daily_bars_calls, [])

    def test_a_within_window_case_still_resolves(self):
        case = _case(delisting_date=date(2020, 1, 15))
        bars = _declining_series("OLDCO", date(2020, 1, 1), 10, terminal_return=-0.10)
        plane = StubPlane(bars={"OLDCO": bars}, master=[_security("OLDCO", "x")])
        result = classify_case(plane, case, history_start=date(2016, 9, 12))
        self.assertEqual(result.classification, "stop")

    def test_resolved_but_no_bars_is_missing_not_unresolved(self):
        case = _case()
        plane = StubPlane(master=[_security("OLDCO", "x")])
        result = classify_case(plane, case)
        self.assertEqual(result.classification, "missing")
        self.assertEqual(result.resolution, "security_master")


def _stub_plane_with(resolved_cases: int, unresolved_cases: int) -> tuple[StubPlane, list[DelistingCase]]:
    cases = []
    master_rows = []
    bars = {}
    for i in range(resolved_cases):
        ticker = f"OK{i}"
        cases.append(_case(ticker=ticker, company=f"Ok Company {i}"))
        master_rows.append(_security(ticker, f"Ok Company {i}"))
        bars[ticker] = _declining_series(ticker, date(2020, 1, 1), 10, terminal_return=-0.10)
    for i in range(unresolved_cases):
        cases.append(_case(ticker=f"BAD{i}", company=f"Bad Company {i}"))
    return StubPlane(bars=bars, master=master_rows), cases


class RunAuditTests(unittest.TestCase):
    def test_summary_reports_testable_count(self):
        plane, cases = _stub_plane_with(resolved_cases=15, unresolved_cases=5)
        result = run_audit(plane, cases=cases, min_testable=1)
        self.assertEqual(result["n_cases"], 20)
        self.assertEqual(result["n_testable"], 15)
        self.assertEqual(result["counts"]["unresolved"], 5)
        self.assertEqual(result["counts"]["stop"], 15)
        self.assertEqual(set(result["counts"]), set(CLASSIFICATIONS))

    def test_refuses_below_the_testable_minimum(self):
        plane, cases = _stub_plane_with(resolved_cases=3, unresolved_cases=17)
        with self.assertRaises(InsufficientTestableCasesError):
            run_audit(plane, cases=cases, min_testable=DEFAULT_MIN_TESTABLE_CASES)

    def test_meeting_the_minimum_exactly_does_not_refuse(self):
        plane, cases = _stub_plane_with(resolved_cases=10, unresolved_cases=10)
        result = run_audit(plane, cases=cases, min_testable=10)
        self.assertEqual(result["n_testable"], 10)

    def test_terminal_returns_synthesised_is_computed_over_testable_cases_only(self):
        """Every testable case is a `stop` here; `unresolved` must not water
        the ratio down or up."""
        plane, cases = _stub_plane_with(resolved_cases=12, unresolved_cases=8)
        result = run_audit(plane, cases=cases, min_testable=1)
        self.assertTrue(result["terminal_returns_must_be_synthesised"])
        self.assertEqual(result["collapse_rate_of_classified"], 0.0)


class CompanyNameNormalizationTests(unittest.TestCase):
    def test_suffixes_and_punctuation_are_stripped(self):
        self.assertEqual(
            normalize_company_name("RadioShack Corporation"),
            normalize_company_name("RADIOSHACK CORP"),
        )
        self.assertEqual(
            normalize_company_name("Bed Bath & Beyond Inc"),
            normalize_company_name("BED BATH BEYOND, INCORPORATED"),
        )

    def test_a_genuinely_different_company_does_not_collapse(self):
        self.assertNotEqual(
            normalize_company_name("Old Company Inc"),
            normalize_company_name("New Company Inc"),
        )


class _TickerFilteringStubClient(_StubClient):
    """`_StubClient` pages the whole configured table regardless of the
    `ticker` query param — fine for the existing adapter tests, which each
    configure exactly one row per ticker, but not precise enough to test a
    method that looks up one ticker and then a *different* one derived from
    the first (`find_by_company_name`'s relatedtickers pass). This applies
    the vendor's own documented `ticker=` filter (comma-separated) to the
    `tickers` table before paging.
    """

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        if table == "tickers" and "ticker" in params:
            wanted = set(str(params["ticker"]).split(","))
            entry = self.tables[table]
            filtered = [row for row in entry if row.get("ticker") in wanted]
            self.calls.append((table, params, dict(headers or {})))
            return _StubResponse({"count": len(filtered), "data": filtered})
        return super().get(url, params=params, headers=headers, timeout=timeout)


class SharadarFindByCompanyNameTests(unittest.TestCase):
    """Exercises the real Sharadar adapter method, not just the stub plane."""

    def test_relatedtickers_pass_finds_the_renamed_symbol(self):
        from data.prices.sharadar import SharadarPricePlane

        tables = _sharadar_tables(tickers=[
            {
                # A name that will not match "Old Company Inc" by itself —
                # isolates the relatedtickers path from the name-scan path.
                "table": "stocks", "permaticker": "1", "ticker": "OLDCO",
                "name": "OLDCO HISTORICAL FILING ENTITY", "exchange": "NYSE", "isdelisted": "Y",
                "firstpricedate": "2000-01-01", "lastpricedate": "2019-01-01",
                "relatedtickers": "NEWCOQ",
            },
            {
                "table": "stocks", "permaticker": "2", "ticker": "NEWCOQ",
                "name": "NEWCO REORGANIZED TRUST", "exchange": "NYSE", "isdelisted": "Y",
                "firstpricedate": "2019-01-02", "lastpricedate": "2020-01-01",
            },
        ])
        client = _TickerFilteringStubClient(tables)
        plane = SharadarPricePlane(api_key="test-key", client=client)

        # A plain name search would find neither row: the post-bankruptcy
        # entity's registered name has nothing to do with the old one. Only
        # `relatedtickers`, keyed off the original ticker, finds it.
        candidates = plane.find_by_company_name("Old Company Inc", original_ticker="OLDCO")
        self.assertEqual({row["ticker"] for row in candidates}, {"NEWCOQ"})

    def test_full_table_name_scan_finds_a_match_with_no_relatedtickers(self):
        from data.prices.sharadar import SharadarPricePlane

        tables = _sharadar_tables(tickers=[
            {
                "table": "stocks", "permaticker": "3", "ticker": "SOMEQ",
                "name": "TARGET COMPANY INCORPORATED", "exchange": "NASDAQ", "isdelisted": "Y",
                "firstpricedate": "2010-01-01", "lastpricedate": "2021-01-01",
            },
        ])
        client = _StubClient(tables)
        plane = SharadarPricePlane(api_key="test-key", client=client)

        candidates = plane.find_by_company_name("Target Company Inc")
        self.assertEqual([row["ticker"] for row in candidates], ["SOMEQ"])
        self.assertTrue(plane.supports_company_name_search)


class StoredBlobTests(unittest.TestCase):
    def test_case_level_resolution_fields_round_trip_through_the_snapshot(self):
        from data.prices import store

        db = init_test_db("delisting_resolver_blob")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        case = _case(ticker="OLDCO", company="Old Company Inc")
        bars = _declining_series("OLDCO", date(2020, 1, 1), 10, terminal_return=-0.90)
        plane = StubPlane(bars={"OLDCO": bars}, master=[_security("OLDCO", "x")])
        result = run_audit(plane, cases=[case], min_testable=1)

        with get_session() as session:
            store.record_snapshot(session, "resolver-blob", plane.source, {}, result)
        with get_session() as session:
            stored = store.get_snapshot(session, "resolver-blob").delisting_audit

        self.assertEqual(stored["n_testable"], 1)
        row = stored["cases"][0]
        self.assertEqual(row["classification"], "collapse")
        self.assertEqual(row["resolved_symbol"], "OLDCO")
        self.assertEqual(row["resolution"], "security_master")


class ComparablesReaderToleranceTests(unittest.TestCase):
    """`comparables.cohort.snapshot_status` must not choke on the two new
    classification counts (grep for `delisting_audit` finds every reader)."""

    def test_snapshot_status_tolerates_unresolved_and_out_of_window_counts(self):
        from comparables import cohort as cohort_mod
        from data.prices import store

        db = init_test_db("delisting_resolver_cohort")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        plane, cases = _stub_plane_with(resolved_cases=11, unresolved_cases=9)
        result = run_audit(plane, cases=cases, min_testable=1)
        self.assertGreater(result["counts"]["unresolved"], 0)

        with get_session() as session:
            store.record_snapshot(session, "resolver-cohort", plane.source, {}, result)
        with get_session() as session:
            status = cohort_mod.snapshot_status(session, "resolver-cohort")

        self.assertTrue(status.exists)
        self.assertTrue(status.audit_recorded)
        self.assertTrue(status.terminal_returns_synthesised)  # every testable case is a stop
        self.assertEqual(status.collapse_rate_of_classified, 0.0)


if __name__ == "__main__":
    unittest.main()
