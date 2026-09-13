"""Funds (Sharadar `funds`/SFP) in the price plane, so SPY can be the benchmark.

Every payload these tests parse is a **live capture** under
`tests/fixtures/sharadar_direct/`, recorded 2026-09-13 with Sharadar's public
`test-api-key` — including the eight SPY sessions in `funds_spy.json`, which
were chosen to straddle a real 2024-03-15 ex-dividend date. The one exception
is `actions_spy_unverified.json`, which could not be captured (`actions` for a
fund is a 403 on that key) and is therefore built from the vendor's own DDL and
marked `unverified_live: true`. **No test below asserts on a number from that
file as a value**; they assert on the table the request went to and on the shape
of what came back, which hold whatever a paid key would return.

The point of the whole change, in one line: `tickers?ticker=SPY` returns
exactly one row and its `table` is `funds`, so an adapter that only ever sent
`table=stocks` could not see the security every abnormal return in Spec N §5.2
is measured against.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from data.prices import store, universes
from data.prices.base import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_FUND,
    PricePlaneConfigError,
    PricePlaneSchemaError,
    SecurityMasterRow,
)
from data.prices.derived import check_reconstruction
from data.prices.sharadar import (
    FUND_DISTRIBUTION_SAFETY,
    FUND_QUOTE_HALF_ULP,
    SharadarPricePlane,
    fund_factors_from_quotes,
)
from tests.test_price_plane import _StubClient, _StubResponse, _sharadar_tables
from tests.dbfixture import init_test_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sharadar_direct"


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _rows(name: str) -> list[dict]:
    return list(_payload(name)["data"])


#: The live SPY capture, ascending, as `(session, close, closeadj, closeunadj)`.
def _spy_quotes() -> list[tuple[date, float, float, float]]:
    rows = sorted(_rows("funds_spy.json"), key=lambda r: r["date"])
    return [
        (date.fromisoformat(r["date"]), float(r["close"]),
         float(r["closeadj"]), float(r["closeunadj"]))
        for r in rows
    ]


def _fund_tables(**overrides):
    """Stub tables carrying the live SPY rows plus the live BBBY equity rows.

    Both instrument classes in one `tickers` table is the interesting case: it
    is what `asset_class=` has to narrow and what auto-detect has to resolve
    per name.
    """
    tables = _sharadar_tables()
    tables["tickers"] = list(tables["tickers"]) + _rows("tickers_spy_all_tables.json")
    tables["funds"] = _rows("funds_spy.json")
    tables["actions"] = list(tables["actions"]) + _rows("actions_spy_unverified.json")
    tables.update(overrides)
    return tables


class _FilteringStubClient(_StubClient):
    """`_StubClient` that honours `table=` and `ticker=`, as the server does.

    The shared stub returns a table wholesale, which is fine for the equity
    tests it was written for — one name, one plan. Funds need more: the whole
    question is whether `security_master` narrows a mixed `tickers` payload the
    way the live API does, and a stub that ignored the filter would let a broken
    filter pass. Confirmed against the real thing:
    `tickers?table=funds&ticker=SPY` returns exactly the one row
    (`tickers_spy_funds.json`).
    """

    #: Columns the API filters on, per table. `tickers` is the only one that
    #: takes `table=`; it is a column there, not a routing prefix.
    FILTERABLE = ("table", "ticker")

    def get(self, url, params=None, headers=None, timeout=None):
        response = super().get(url, params=params, headers=headers, timeout=timeout)
        params = dict(params or {})
        wanted = {
            name: str(params[name]).split(",")
            for name in self.FILTERABLE if params.get(name) is not None
        }
        if not wanted:
            return response
        try:
            payload = response.json()
        except Exception:
            return response
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return response
        kept = [
            row for row in payload["data"]
            if all(str(row.get(name)) in values for name, values in wanted.items()
                   if name in row)
        ]
        return _StubResponse({"count": len(kept), "data": kept})


def _plane(tables=None) -> tuple[SharadarPricePlane, _StubClient]:
    client = _FilteringStubClient(tables or _fund_tables())
    return SharadarPricePlane(api_key="k", client=client), client


# --------------------------------------------------------------------------- #


class SecurityMasterAssetClassTests(unittest.TestCase):
    def test_spy_is_a_fund_and_bbby_is_an_equity(self):
        """The live `tickers` rows, classed by the table each name is in."""
        plane, _ = _plane()
        rows = {r.ticker: r for r in plane.security_master(["SPY", "BBBY"], asset_class=None)}
        self.assertEqual(rows["SPY"].asset_class, ASSET_CLASS_FUND)
        self.assertEqual(rows["BBBY"].asset_class, ASSET_CLASS_EQUITY)
        # permaticker 118691, straight off the live capture.
        self.assertEqual(rows["SPY"].security_uid, "sharadar:118691")

    def test_the_default_is_equities_and_excludes_the_fund(self):
        """A caller written before funds existed keeps what it had."""
        plane, client = _plane()
        tickers = [r.ticker for r in plane.security_master(["SPY", "BBBY"])]
        self.assertEqual(tickers, ["BBBY"])
        self.assertEqual(client.calls[0][1]["table"], "stocks")

    def test_asking_for_funds_sends_table_funds(self):
        plane, client = _plane()
        rows = plane.security_master(["SPY"], asset_class=ASSET_CLASS_FUND)
        self.assertEqual(client.calls[0][1]["table"], "funds")
        self.assertEqual([r.ticker for r in rows], ["SPY"])

    def test_auto_detect_sends_no_table_filter_and_drops_non_price_plans(self):
        """AAPL comes back 3x unfiltered (`stocks`/`fundamentals`/`insiders`).

        Only the price tables are security-master rows; the other two describe
        the same name in a plan that carries no bars.
        """
        tables = _fund_tables(tickers=_rows("tickers_aapl_all_tables.json"))
        plane, client = _plane(tables)
        rows = plane.security_master(["AAPL"], asset_class=None)
        self.assertNotIn("table", client.calls[0][1])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].asset_class, ASSET_CLASS_EQUITY)

    def test_a_name_in_both_price_tables_refuses_rather_than_picking(self):
        both = [
            {"table": "stocks", "permaticker": "1", "ticker": "DUP", "name": "x",
             "exchange": "NYSE", "isdelisted": "N",
             "firstpricedate": "2020-01-02", "lastpricedate": "2024-01-02"},
            {"table": "funds", "permaticker": "2", "ticker": "DUP", "name": "x",
             "exchange": "NYSEARCA", "isdelisted": "N",
             "firstpricedate": "2020-01-02", "lastpricedate": "2024-01-02"},
        ]
        plane, _ = _plane(_fund_tables(tickers=both))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.security_master(["DUP"], asset_class=None)
        self.assertIn("will not pick one", str(caught.exception))

    def test_an_unknown_asset_class_is_a_config_error(self):
        plane, _ = _plane()
        with self.assertRaises(PricePlaneConfigError):
            plane.security_master(["SPY"], asset_class="crypto")

    def test_a_ticker_in_no_price_table_refuses_to_invent_a_uid(self):
        plane, _ = _plane(_fund_tables(tickers=[]))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("NOPE")
        self.assertIn("refusing to invent", str(caught.exception))

    def test_a_tickers_payload_without_the_table_column_fails_loudly(self):
        """`table` is validated, not read best-effort.

        Without the guard an auto-detect read of a payload missing that column
        would resolve every name to nothing at all, quietly, which is the
        module's first rule inverted.
        """
        stripped = [
            {k: v for k, v in row.items() if k != "table"}
            for row in _rows("tickers_spy_all_tables.json")
        ]
        plane, _ = _plane(_fund_tables(tickers=stripped))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.security_master(["SPY"], asset_class=None)
        self.assertIn("table", str(caught.exception))


class FundBarsTests(unittest.TestCase):
    def test_daily_bars_for_a_fund_reads_funds_not_stocks(self):
        plane, client = _plane()
        bars = plane.daily_bars("SPY")
        self.assertTrue(bars)
        price_tables = [t for t, _p, _h in client.calls if t in ("stocks", "funds")]
        self.assertEqual(price_tables, ["funds"])

    def test_daily_bars_for_an_equity_still_reads_stocks(self):
        plane, client = _plane()
        self.assertTrue(plane.daily_bars("BBBY"))
        price_tables = [t for t, _p, _h in client.calls if t in ("stocks", "funds")]
        self.assertEqual(price_tables, ["stocks"])

    def test_the_three_series_reconstruct_for_a_fund_with_a_distribution(self):
        """Spec N §4.3, on the live window straddling SPY's 2024-03-15 ex-date."""
        plane, _ = _plane()
        bars = plane.daily_bars("SPY")
        check_reconstruction(bars)
        paying = [b for b in bars if b.dividend_cash > 0]
        self.assertEqual([b.session_date for b in paying], [date(2024, 3, 15)])

    def test_total_return_outruns_price_return_across_the_ex_date(self):
        """The yield gap is the whole reason the third series exists."""
        plane, _ = _plane()
        bars = plane.daily_bars("SPY")
        first, last = bars[0], bars[-1]
        price = last.raw_close / first.raw_close
        total = last.total_return_close / first.total_return_close
        self.assertGreater(total, price)

    def test_no_fictional_dividend_on_an_ordinary_session(self):
        """287 of 292 live SPY sessions must carry exactly 0.0, not 0.0007.

        Three-decimal quotes cannot resolve a tenth of a cent; storing the
        residue as `dividend_cash` would be a fabricated fact on every
        ordinary day of the year.
        """
        factors = fund_factors_from_quotes(_spy_quotes(), "SPY")
        ordinary = [d for (_s, d), q in zip(factors, _spy_quotes())
                    if q[0] != date(2024, 3, 15)]
        self.assertTrue(ordinary)
        self.assertEqual(set(ordinary), {0.0})

    def test_no_fictional_split_on_an_ordinary_session(self):
        factors = fund_factors_from_quotes(_spy_quotes(), "SPY")
        self.assertEqual({split for split, _d in factors}, {1.0})

    def test_a_real_split_survives_the_snap(self):
        """A 2-for-1 shows up as `closeunadj/close` halving, and must not snap."""
        quotes = [
            (date(2024, 1, 2), 50.0, 50.0, 100.0),   # pre-split: F = 2.0
            (date(2024, 1, 3), 51.0, 51.0, 51.0),    # post-split: F = 1.0
        ]
        factors = fund_factors_from_quotes(quotes, "SPLT")
        self.assertAlmostEqual(factors[1][0], 2.0, places=9)
        self.assertEqual(factors[1][1], 0.0)

    def test_a_negative_distribution_above_the_noise_floor_refuses(self):
        """`closeadj` falling faster than `close` cannot be a cash distribution.

        The three-series model has no way to express it, so the adapter says so
        rather than storing a series that claims the identity holds.
        """
        quotes = [
            (date(2024, 1, 2), 100.0, 100.0, 100.0),
            (date(2024, 1, 3), 100.0, 95.0, 100.0),
        ]
        with self.assertRaises(PricePlaneSchemaError) as caught:
            fund_factors_from_quotes(quotes, "BAD")
        self.assertIn("negative", str(caught.exception))

    def test_the_noise_floor_is_far_below_a_real_distribution(self):
        """Stated as a ratio so the constants cannot drift apart unnoticed.

        Measured live over 292 SPY sessions: implied noise never above
        0.019 bps of price, the five real distributions all above 30 bps.
        """
        prev_unadj = prev_adj = 476.0
        bound = 4 * FUND_QUOTE_HALF_ULP * (prev_unadj / prev_adj)
        floor = FUND_DISTRIBUTION_SAFETY * bound
        self.assertLess(floor, 0.10)              # far below a $1.60 distribution
        self.assertGreater(floor, 0.001)          # and above the $0.0009 noise

    def test_a_non_positive_quote_refuses(self):
        quotes = [
            (date(2024, 1, 2), 100.0, 100.0, 100.0),
            (date(2024, 1, 3), 0.0, 95.0, 100.0),
        ]
        with self.assertRaises(PricePlaneSchemaError):
            fund_factors_from_quotes(quotes, "ZERO")

    def test_corporate_actions_for_a_fund_still_reads_the_actions_table(self):
        """The action log stays the action log.

        Shape only: `actions_spy_unverified.json` is schema-built (403 on the
        public key), so the assertion is that the request went to `actions` and
        parsed, never what the values are.
        """
        plane, client = _plane()
        records = plane.corporate_actions("SPY")
        self.assertEqual([t for t, _p, _h in client.calls][-1], "actions")
        self.assertTrue(records)
        self.assertEqual({r.security_uid for r in records}, {"sharadar:118691"})
        self.assertEqual(
            [r.ex_date for r in records], sorted(r.ex_date for r in records)
        )

    def test_an_empty_funds_slice_is_empty_not_an_error(self):
        plane, _ = _plane(_fund_tables(funds=[]))
        self.assertEqual(plane.daily_bars("SPY"), ())

    def test_a_renamed_funds_column_fails_loudly(self):
        renamed = [
            {k if k != "closeadj" else "close_adj": v for k, v in row.items()}
            for row in _rows("funds_spy.json")
        ]
        plane, _ = _plane(_fund_tables(funds=renamed))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("SPY")
        self.assertIn("closeadj", str(caught.exception))


class FundsAreNeverUniverseMembersTests(unittest.TestCase):
    """A benchmark ETF inside `liquid_us_equity_v1` is a cohort measured against
    itself (Spec N §5.2). The fund is deliberately the *most* liquid name here,
    so a rule that ranked it would rank it first."""

    SESSIONS = [date(2024, 1, d) for d in range(2, 32) if date(2024, 1, d).weekday() < 5]

    def _seed(self, session):
        rows = [
            SecurityMasterRow(
                security_uid="u-fund", ticker="SPY", source="fixture",
                venue="nyse_amex", asset_class=ASSET_CLASS_FUND,
                ticker_valid_from=self.SESSIONS[0],
            ),
            SecurityMasterRow(
                security_uid="u-eq", ticker="ACME", source="fixture",
                venue="nasdaq", asset_class=ASSET_CLASS_EQUITY,
                ticker_valid_from=self.SESSIONS[0],
            ),
        ]
        store.upsert_securities(session, rows)
        from data.prices.base import DailyBar

        bars = []
        for uid, ticker, volume in (("u-fund", "SPY", 80_000_000.0),
                                    ("u-eq", "ACME", 1_000_000.0)):
            for i, day in enumerate(self.SESSIONS):
                close = 100.0 + i
                bars.append(DailyBar(
                    security_uid=uid, ticker=ticker, session_date=day,
                    raw_open=close, raw_high=close, raw_low=close, raw_close=close,
                    volume=volume, split_factor=1.0, dividend_cash=0.0,
                    split_adjusted_close=close, total_return_close=close,
                    source="fixture",
                ))
        store.upsert_bars(session, bars)

    def test_rebuild_ranks_the_equity_and_not_the_fund(self):
        db = init_test_db("funds_universe")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            self._seed(session)
        with get_session() as session:
            universes.rebuild(session, top_n=10, window=20)
            uids = {
                m.security_uid for m in store.members_as_of(
                    session, universes.UNIVERSE_SLUG, self.SESSIONS[-1]
                )
            }
        self.assertIn("u-eq", uids)
        self.assertNotIn("u-fund", uids)

    def test_compute_membership_refuses_an_ineligible_uid_outright(self):
        """The belt, independent of the braces: even if the filter were bypassed."""
        from data.prices.base import DailyBar

        bars = {
            "u-fund": tuple(
                DailyBar(
                    security_uid="u-fund", ticker="SPY", session_date=day,
                    raw_open=100.0, raw_high=100.0, raw_low=100.0, raw_close=100.0,
                    volume=1_000.0, split_factor=1.0, dividend_cash=0.0,
                    split_adjusted_close=100.0, total_return_close=100.0,
                    source="fixture",
                )
                for day in self.SESSIONS
            )
        }
        # Ranked normally it is the only member there is...
        self.assertTrue(universes.compute_membership(bars, self.SESSIONS, top_n=10, window=20))
        # ...and with an empty eligible set it produces nothing rather than
        # raising, because nothing was ranked in the first place.
        self.assertEqual(
            universes.compute_membership(
                bars, self.SESSIONS, top_n=10, window=20, eligible_uids=frozenset()
            ),
            (),
        )

    def test_an_uncatalogued_uid_is_still_ranked(self):
        """`rebuild` reads stored bars and nothing else, and still must.

        The filter is an exclusion of *known* funds, not an allow-list of known
        equities: an allow-list would silently empty `universe_membership`
        whenever a price file had been loaded ahead of its master.
        """
        db = init_test_db("funds_universe_nomaster")
        self.addCleanup(db.cleanup)
        from database.db import get_session
        from data.prices.base import DailyBar

        with get_session() as session:
            store.upsert_bars(session, [
                DailyBar(
                    security_uid="u-orphan", ticker="ORPH", session_date=day,
                    raw_open=10.0, raw_high=10.0, raw_low=10.0, raw_close=10.0,
                    volume=500_000.0, split_factor=1.0, dividend_cash=0.0,
                    split_adjusted_close=10.0, total_return_close=10.0,
                    source="fixture",
                )
                for day in self.SESSIONS
            ])
        with get_session() as session:
            self.assertGreater(universes.rebuild(session, top_n=10, window=20), 0)


class StoreAssetClassTests(unittest.TestCase):
    def test_upsert_round_trips_the_class_and_defaults_to_equity(self):
        db = init_test_db("asset_class_store")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            store.upsert_securities(session, [
                SecurityMasterRow(security_uid="a", ticker="SPY", source="s",
                                  asset_class=ASSET_CLASS_FUND),
                SecurityMasterRow(security_uid="b", ticker="ACME", source="s"),
            ])
        with get_session() as session:
            self.assertEqual(
                store.asset_class_by_uid(session),
                {"a": ASSET_CLASS_FUND, "b": ASSET_CLASS_EQUITY},
            )
            self.assertEqual(store.non_equity_security_uids(session), frozenset({"a"}))

    def test_two_classes_for_one_uid_refuse_rather_than_pick(self):
        db = init_test_db("asset_class_clash")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            store.upsert_securities(session, [
                SecurityMasterRow(security_uid="a", ticker="OLD", source="s",
                                  ticker_valid_from=date(2020, 1, 2),
                                  asset_class=ASSET_CLASS_FUND),
                SecurityMasterRow(security_uid="a", ticker="NEW", source="s",
                                  ticker_valid_from=date(2022, 1, 3),
                                  asset_class=ASSET_CLASS_EQUITY),
            ])
        with get_session() as session:
            with self.assertRaises(ValueError):
                store.asset_class_by_uid(session)

    def test_an_unknown_asset_class_is_rejected_at_the_record(self):
        with self.assertRaises(PricePlaneSchemaError):
            SecurityMasterRow(security_uid="a", ticker="X", source="s",
                              asset_class="bond")


if __name__ == "__main__":
    unittest.main()


class FundEntryPointTests(unittest.TestCase):
    """`--asset-class` on the backfill, and `scripts/benchmark_uid.py`.

    Driven against `FixturePricePlane`, whose committed rows are all equities,
    so these hold the *gating* and the *reporting* down rather than re-testing
    the vendor parsing above. The gate matters on its own: fund support is new
    capability, so it ships off, and this is what proves production behaves
    identically with no new variable set.
    """

    def _settings(self, **overrides):
        from config.settings import Settings

        kwargs = dict(
            price_plane_enabled=True,
            price_plane_source="fixture",
            price_plane_snapshot="funds_e2e",
            database_url=self.db.url,
        )
        kwargs.update(overrides)
        return Settings(**kwargs)

    def _install(self, settings):
        from data.prices import config as plane_config

        original = plane_config.get_settings
        plane_config.get_settings = lambda: settings
        self.addCleanup(lambda: setattr(plane_config, "get_settings", original))

    def setUp(self):
        self.db = init_test_db("funds_entry_points")
        self.addCleanup(self.db.cleanup)

    def _run(self, module, argv):
        buffer = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = module.main(argv)
        return code, buffer.getvalue(), errors.getvalue()

    def test_the_default_asset_class_needs_no_new_flag(self):
        """Production must not change when this merges with nothing set."""
        from scripts import price_backfill

        self._install(self._settings())
        code, _out, _err = self._run(price_backfill, ["--source", "fixture"])
        self.assertEqual(code, 0)

    def test_asking_for_funds_refuses_while_the_flag_is_off(self):
        from scripts import price_backfill

        self._install(self._settings())
        code, _out, err = self._run(
            price_backfill, ["--source", "fixture", "--asset-class", "fund"]
        )
        self.assertEqual(code, 2)
        self.assertIn("PRICE_PLANE_FUNDS_ENABLED", err)

    def test_auto_detect_is_gated_too(self):
        from scripts import price_backfill

        self._install(self._settings())
        code, _out, err = self._run(
            price_backfill, ["--source", "fixture", "--asset-class", "auto"]
        )
        self.assertEqual(code, 2)
        self.assertIn("PRICE_PLANE_FUNDS_ENABLED", err)

    def test_bulk_refuses_a_fund_rather_than_loading_equities(self):
        """`--bulk` parses the `stocks` zip. Pairing it with `fund` would
        otherwise report success over the wrong table."""
        from scripts import price_backfill

        self._install(self._settings(price_plane_funds_enabled=True,
                                     price_plane_source="sharadar",
                                     nasdaq_data_link_api_key="k"))
        code, _out, err = self._run(
            price_backfill,
            ["--source", "sharadar", "--bulk", "years=10", "--asset-class", "fund"],
        )
        self.assertEqual(code, 2)
        self.assertIn("stocks", err)

    def test_a_fund_run_prints_the_security_uid_to_paste(self):
        """The uid printout is the whole point of a fund backfill.

        `COMPARABLE_BENCHMARK_SECURITY_UID` is a uid, not a ticker, and nothing
        else in the pipeline shows one to a human.
        """
        from scripts import price_backfill

        summary = {
            "bars_written": 3, "actions_written": 0, "securities_written": 1,
            "tickers_empty": [],
            "security_uid_by_ticker": {"SPY": "sharadar:118691"},
            "asset_class_by_ticker": {"SPY": ASSET_CLASS_FUND},
        }
        self._install(self._settings(price_plane_funds_enabled=True))
        with mock.patch.object(price_backfill, "backfill", return_value=summary):
            code, out, _err = self._run(
                price_backfill,
                ["--source", "fixture", "--tickers", "SPY", "--asset-class", "fund"],
            )
        self.assertEqual(code, 0)
        self.assertIn("sharadar:118691", out)
        self.assertIn("COMPARABLE_BENCHMARK_SECURITY_UID=sharadar:118691", out)

    def test_backfill_passes_the_class_to_the_master_and_not_to_daily_bars(self):
        """One source of truth for "what kind of instrument is this".

        `daily_bars` resolves the table through the same master, so the two
        calls cannot disagree about a name.
        """
        from scripts import price_backfill

        self._install(self._settings(price_plane_funds_enabled=True))

        seen = {}
        plane = mock.MagicMock()
        plane.source = "fixture"

        def _master(tickers, *, asset_class):
            seen["asset_class"] = asset_class
            return ()

        plane.security_master.side_effect = _master
        plane.daily_bars.return_value = ()

        from database.db import init_db

        init_db(self.db.url)
        summary = price_backfill.backfill(
            plane, ["SPY"], None, asset_class=ASSET_CLASS_FUND
        )
        self.assertEqual(seen["asset_class"], ASSET_CLASS_FUND)
        plane.daily_bars.assert_called_once_with("SPY", None, None)
        self.assertEqual(summary["asset_class"], ASSET_CLASS_FUND)

    def test_benchmark_uid_refuses_while_the_flag_is_off(self):
        from scripts import benchmark_uid

        self._install(self._settings())
        code, _out, err = self._run(benchmark_uid, ["SPY"])
        self.assertEqual(code, 2)
        self.assertIn("PRICE_PLANE_FUNDS_ENABLED", err)

    def test_benchmark_uid_prints_a_stored_fund_and_only_a_fund(self):
        from scripts import benchmark_uid
        from database.db import get_session, init_db

        self._install(self._settings(price_plane_funds_enabled=True))
        init_db(self.db.url)
        with get_session() as session:
            store.upsert_securities(session, [
                SecurityMasterRow(security_uid="sharadar:118691", ticker="SPY",
                                  source="sharadar", venue="nyse_amex",
                                  asset_class=ASSET_CLASS_FUND),
                SecurityMasterRow(security_uid="sharadar:320193", ticker="AAPL",
                                  source="sharadar", venue="nasdaq"),
            ])

        code, out, _err = self._run(benchmark_uid, [])
        self.assertEqual(code, 0)
        self.assertIn("sharadar:118691", out)
        self.assertNotIn("AAPL", out)
        self.assertIn("COMPARABLE_BENCHMARK_SECURITY_UID=sharadar:118691", out)

    def test_benchmark_uid_says_what_to_run_when_nothing_is_stored(self):
        from scripts import benchmark_uid
        from database.db import init_db

        self._install(self._settings(price_plane_funds_enabled=True))
        init_db(self.db.url)
        code, _out, err = self._run(benchmark_uid, ["SPY"])
        self.assertEqual(code, 1)
        self.assertIn("--asset-class fund", err)
