"""SQLite -> Postgres round-trip parity (Spec K §3.2, §8 ``test_sqlite_migration_roundtrip``).

A populated SQLite fixture is migrated into a real Postgres schema and both
sides are compared on row count and per-table content hash. The fixture is built
to be hostile to a naive copy: booleans, whole numbers in float columns, NULLs
next to defaults, naive UTC timestamps, dates, JSON-in-Text, unicode, and rows
whose foreign keys only resolve if the tables are written parents-first.

These tests need both engines at once, so they follow ``TEST_POSTGRES_URL``
rather than ``TEST_DATABASE_URL`` — CI sets it on both matrix entries. Without a
Postgres they skip loudly rather than pass vacuously.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from scripts import migrate_sqlite_to_postgres as migrate
from tests.dbfixture import TestDatabase, postgres_url, sqlite_url

#: Tables the fixture deliberately populates. The round-trip assertion checks
#: that each of these actually carried rows across — a hash comparison over two
#: empty tables proves nothing, and this is the list the checkpoint log reports.
POPULATED_TABLES = [
    "tickers",
    "memos",
    "trades",
    "order_events",
    "scored_candidates",
    "historical_events",
    "event_outcomes",
    "macro_regime",
    "watchlist_tickers",
    "workspace_tokens",
    "source_observations",
]


def _populate(url: str) -> None:
    """Build the source database through the ORM, then fill it."""
    from database.db import get_session, init_db
    from database.models import (
        EventOutcome,
        HistoricalEvent,
        MacroRegime,
        Memo,
        OrderEvent,
        ScoredCandidate,
        SourceObservation,
        Ticker,
        Trade,
        WatchlistTicker,
        WorkspaceToken,
    )

    init_db(url)
    with get_session() as session:
        session.add_all(
            [
                Ticker(
                    symbol="AAPL",
                    name="Apple Inc.",
                    sector="Technology",
                    market_cap=3_000_000_000_000.0,
                    in_universe=True,
                    added_at=datetime(2026, 1, 2, 13, 30, 15),
                    updated_at=datetime(2026, 1, 2, 13, 30, 15),
                ),
                # market_cap is a whole number in a Float column: SQLite returns
                # int, Postgres returns float. Unnormalised, the hashes differ.
                Ticker(
                    symbol="MSFT",
                    name="Microsoft — Ünïcode ✓",
                    sector="Technology",
                    market_cap=2_000_000_000_000,
                    in_universe=False,
                    added_at=datetime(2026, 1, 3, 9, 0, 0),
                    updated_at=datetime(2026, 1, 3, 9, 0, 0),
                ),
            ]
        )
    with get_session() as session:
        session.add_all(
            [
                Memo(
                    ticker_id=1,
                    composite_score=0.6125,
                    classification="high_conviction",
                    full_text="line one\nline two\ttabbed",
                    memo_data_json='{"a": 1, "b": null}',
                    status="approved",
                    telegram_message_id=None,
                    created_at=datetime(2026, 2, 1, 12, 0, 0),
                ),
                Memo(
                    ticker_id=2,
                    composite_score=0.0,
                    classification="",
                    status="pending",
                    telegram_message_id=8812,
                    created_at=datetime(2026, 2, 2, 12, 0, 0),
                ),
            ]
        )
    with get_session() as session:
        session.add_all(
            [
                Trade(
                    ticker_id=1,
                    memo_id=1,
                    entry_price=190.5,
                    exit_price=None,
                    entry_date=datetime(2026, 2, 3, 14, 31, 0),
                    shares=10,
                    status="open",
                    t1_hit=True,
                    t2_hit=False,
                    pnl_pct=None,
                    created_at=datetime(2026, 2, 3, 14, 31, 0),
                    updated_at=datetime(2026, 2, 3, 14, 31, 0),
                ),
                Trade(
                    ticker_id=2,
                    memo_id=None,
                    entry_price=300,
                    exit_price=310.25,
                    entry_date=datetime(2026, 2, 4, 14, 31, 0),
                    exit_date=datetime(2026, 2, 10, 20, 0, 0),
                    shares=0,
                    status="closed",
                    exit_reason="target_1",
                    pnl_pct=3.4166666666666665,
                    created_at=datetime(2026, 2, 4, 14, 31, 0),
                    updated_at=datetime(2026, 2, 10, 20, 0, 0),
                ),
            ]
        )
    with get_session() as session:
        session.add_all(
            [
                OrderEvent(
                    trade_id=1,
                    broker="robinhood",
                    order_id="ord-1",
                    event_type="submitted",
                    notional=1905.0,
                    raw_payload='{"ref_id": "abc"}',
                    created_at=datetime(2026, 2, 3, 14, 31, 1),
                ),
                OrderEvent(
                    trade_id=None,
                    broker="alpaca",
                    order_id=None,
                    event_type="reconcile",
                    notional=None,
                    created_at=datetime(2026, 2, 3, 14, 32, 0),
                ),
                ScoredCandidate(
                    run_id="run-1",
                    ticker="AAPL",
                    scored_at=datetime(2026, 2, 3, 11, 0, 0),
                    final_score=0.61,
                    memo_generated=True,
                    paper_traded=False,
                    ret_t1=None,
                    ret_t5=-0.0,
                    created_at=datetime(2026, 2, 3, 11, 0, 0),
                ),
                MacroRegime(
                    date=date(2026, 2, 3),
                    regime="risk-on",
                    confidence=0.8,
                    max_positions=6,
                    created_at=datetime(2026, 2, 3, 8, 0, 0),
                ),
                WatchlistTicker(
                    ticker="NVDA",
                    active=True,
                    added_at=datetime(2026, 2, 3, 8, 0, 0),
                ),
                WorkspaceToken(
                    label="codex-laptop",
                    token_hash="a" * 64,
                    token_prefix="swt_abcd",
                    scopes="read,research:write",
                    created_at=datetime(2026, 2, 3, 8, 0, 0),
                ),
            ]
        )
    with get_session() as session:
        session.add_all(
            [
                # Phase 3a's table. Booleans, a nullable self-referencing FK,
                # and a Date beside two UtcDateTimes — the canonicaliser has to
                # render all four identically on both engines.
                SourceObservation(
                    source="sec_xbrl",
                    source_trust="regulator",
                    entity_cik="0000320193",
                    fact_type="Revenues",
                    value_numeric=94_836_000_000.0,
                    unit="USD",
                    period_start=date(2025, 10, 1),
                    valid_at=datetime(2025, 12, 28, 0, 0, 0),
                    known_at_utc=datetime(2026, 1, 30, 22, 30, 5),
                    precision="second",
                    provenance_class="observed",
                    replay_eligible=True,
                    payload_hash="b" * 64,
                    ingested_at=datetime(2026, 1, 31, 1, 0, 0),
                ),
                SourceObservation(
                    source="sec_xbrl",
                    source_trust="regulator",
                    entity_cik="0000320193",
                    fact_type="EntityCommonStockSharesOutstanding",
                    value_numeric=None,
                    value_text="unavailable",
                    valid_at=datetime(2025, 12, 28, 0, 0, 0),
                    known_at_utc=datetime(2026, 1, 30, 22, 30, 5),
                    precision="day",
                    provenance_class="archival_reconstructed",
                    replay_eligible=False,
                    payload_hash="c" * 64,
                    ingested_at=datetime(2026, 1, 31, 1, 0, 0),
                ),
            ]
        )
    with get_session() as session:
        session.add(
            HistoricalEvent(
                ticker="AAPL",
                event_type="earnings_beat",
                event_date=date(2026, 1, 30),
                event_timestamp=datetime(2026, 1, 30, 21, 5, 0),
                magnitude=None,
                confidence=0.5,
                dedupe_key="dedupe-1",
                created_at=datetime(2026, 1, 31, 0, 0, 0),
                updated_at=datetime(2026, 1, 31, 0, 0, 0),
            )
        )
    with get_session() as session:
        session.add(
            EventOutcome(
                event_id=1,
                ticker="AAPL",
                anchor_price=185.0,
                anchor_trade_date=date(2026, 1, 31),
                return_t1=1.5,
                return_t3=None,
            )
        )


class MigrationRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.postgres = postgres_url()
        if cls.postgres is None:
            raise unittest.SkipTest(
                "No Postgres available. Set TEST_POSTGRES_URL (or run the "
                "Postgres matrix entry) to exercise the cutover round-trip."
            )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source_url = sqlite_url(Path(self._tmp.name), "prod_archive")
        _populate(self.source_url)

        self.target = TestDatabase("cutover", base_url=self.postgres)
        self.addCleanup(self.target.cleanup)

        self.report_dir = Path(self._tmp.name) / "audits"

    def _run(self, **kwargs):
        return migrate.run(
            self.source_url,
            self.target.url,
            report_dir=self.report_dir,
            **kwargs,
        )

    def test_round_trip_matches_row_counts_and_content_hashes(self):
        summary = self._run()

        mismatches = [r for r in summary["results"] if not r["ok"]]
        self.assertEqual(mismatches, [], "content hash or row count differed")
        self.assertTrue(summary["ok"])

        by_table = {r["table"]: r for r in summary["results"]}
        # Checkpoint log: which tables actually carried rows across.
        verified = sorted(t for t, r in by_table.items() if r["source_rows"])
        print(f"\nverified parity on populated tables: {verified}")

        for name in POPULATED_TABLES:
            with self.subTest(table=name):
                self.assertIn(name, by_table)
                self.assertGreater(
                    by_table[name]["source_rows"],
                    0,
                    f"{name} is in POPULATED_TABLES but the fixture left it empty",
                )
                self.assertEqual(
                    by_table[name]["source_hash"], by_table[name]["target_hash"]
                )

    def test_target_holds_the_same_values_not_merely_the_same_hash(self):
        """Guard against a hash function that agrees with itself about nothing."""
        self._run()
        from database.models import Ticker, Trade

        engine = create_engine(self.target.url)
        self.addCleanup(engine.dispose)
        with engine.connect() as conn:
            rows = conn.execute(
                select(Ticker.symbol, Ticker.market_cap, Ticker.in_universe).order_by(
                    Ticker.id
                )
            ).all()
            self.assertEqual(
                rows,
                [("AAPL", 3e12, True), ("MSFT", 2e12, False)],
            )
            trade = conn.execute(
                select(Trade.exit_price, Trade.t1_hit, Trade.pnl_pct).where(Trade.id == 1)
            ).one()
            self.assertEqual(trade, (None, True, None))

    def test_a_non_empty_target_is_refused_without_force(self):
        self._run()
        with self.assertRaises(migrate.MigrationError) as caught:
            self._run()
        self.assertIn("--force", str(caught.exception))

    def test_force_is_idempotent(self):
        first = self._run()
        second = self._run(force=True)

        self.assertTrue(second["ok"])
        self.assertTrue(second["forced"])
        self.assertEqual(
            [(r["table"], r["target_rows"], r["target_hash"]) for r in first["results"]],
            [(r["table"], r["target_rows"], r["target_hash"]) for r in second["results"]],
            "a second --force run did not reproduce the first run's target",
        )

    def test_identity_sequences_are_past_the_copied_ids(self):
        """The classic dump-and-load footgun: the next insert must not collide."""
        self._run()
        from database.db import get_session, init_db
        from database.models import Ticker

        init_db(self.target.url)
        with get_session() as session:
            session.add(Ticker(symbol="TSLA"))
        with get_session() as session:
            self.assertEqual(session.query(Ticker).count(), 3)

    def test_the_source_is_opened_read_only(self):
        """The SQLite file is an archive; this script cannot be what damages it."""
        engine = create_engine(migrate.read_only_source_url(self.source_url))
        self.addCleanup(engine.dispose)
        from database.models import Ticker

        with self.assertRaises(OperationalError) as caught:
            with engine.begin() as conn:
                conn.execute(Ticker.__table__.insert().values(symbol="ZZZZ"))
        self.assertIn("readonly", str(caught.exception).lower())

    def test_a_report_is_written_to_the_audit_directory(self):
        summary = self._run()
        self.assertIsNotNone(summary["report_path"])
        text = summary["report_path"].read_text(encoding="utf-8")
        self.assertIn("PASS", text)
        self.assertIn("| `tickers` |", text)
        # The password must not reach a file that gets committed.
        self.assertNotIn("postgres:postgres@", text)

    def test_verify_only_writes_nothing(self):
        summary = self._run(verify_only=True)
        self.assertFalse(summary["ok"], "an empty target cannot match a populated source")
        self.assertEqual(
            [r["target_rows"] for r in summary["results"] if r["target_rows"]], []
        )


if __name__ == "__main__":
    unittest.main()
