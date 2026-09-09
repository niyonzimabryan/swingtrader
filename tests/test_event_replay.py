"""Tests for event-anchored replay, parameter sweep, and the shadow adapter (J2-J4).

Uses a deterministic in-memory bar provider (no network) and a seeded scratch DB
so the class table, sweep grid shape, and J4 present/absent paths are all exact.
"""

from __future__ import annotations

import unittest
from collections import namedtuple
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest.event_replay import (
    HOLD_DAYS_GRID,
    STOP_WIDTH_MULTS,
    TARGET_MULTS,
    TradeParams,
    build_report,
    replay_shadow_ledger,
    run_event_replay,
    run_sweep,
    build_plans,
    load_events,
)
from config.settings import Settings
from data.event_extractor import make_dedupe_key
from database.db import get_session
from database.models import HistoricalEvent, ScoredCandidate
from tests.dbfixture import init_test_db

FakeBar = namedtuple("FakeBar", "date open high low close volume")
EVENT_DATE = date(2025, 1, 15)


def _flat_prewindow(anchor: date, price: float, days: int = 25):
    """Flat OHLC bars up to and including the anchor date -> ATR computes to 0."""
    return [
        FakeBar(anchor - timedelta(days=days - i), price, price, price, price, 1000)
        for i in range(days + 1)  # last element is the anchor (signal) bar
    ]


def _make_bars(anchor: date, price: float, forward: list[tuple]):
    """Pre-window (flat) + signal bar (anchor) + forward bars on consecutive days."""
    bars = _flat_prewindow(anchor, price)
    for i, (o, h, l, c) in enumerate(forward, start=1):
        bars.append(FakeBar(anchor + timedelta(days=i), o, h, l, c, 1000))
    return bars


# Forward paths (price 100, ATR 0 -> default 5% stop; long entry 100.1, t1 110.11,
# t2 115.115 / short entry 99.9, stop 104.895).
WIN_LONG = [(100, 120, 100, 118)] + [(118, 119, 117, 118)] * 30    # gaps through T1 & T2
LOSE_LONG = [(100, 101, 90, 92)] + [(92, 93, 91, 92)] * 30          # low 90 < stop 95.095
WIN_SHORT = [(100, 101, 80, 82)] + [(82, 83, 81, 82)] * 30          # low 80 < short T1/T2
FLAT_HOLD = [(100, 101, 99, 100)] * 30                              # rides to time exit


class _Provider:
    def __init__(self, by_ticker):
        self.by_ticker = by_ticker

    def __call__(self, ticker, start, end, session):
        return self.by_ticker.get(ticker.upper(), [])


def _event(session, ticker, event_type, polarity, days_ago_from=EVENT_DATE, magnitude=5.0,
           source_type="fmp_structured", event_date=EVENT_DATE):
    ev = HistoricalEvent(
        ticker=ticker,
        event_type=event_type,
        event_date=event_date,
        polarity=polarity,
        magnitude=magnitude,
        source_type=source_type,
        headline=f"{ticker} {event_type}",
        summary="seed",
        confidence=0.9,
        dedupe_key=make_dedupe_key(ticker, event_type, event_date),
    )
    session.add(ev)
    return ev


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("replay")
        self.settings = Settings()
        self.provider = _Provider({
            "WINR": _make_bars(EVENT_DATE, 100, WIN_LONG),
            "LOSR": _make_bars(EVENT_DATE, 100, LOSE_LONG),
            "SHRT": _make_bars(EVENT_DATE, 100, WIN_SHORT),
            "HOLD": _make_bars(EVENT_DATE, 100, FLAT_HOLD),
        })

    def tearDown(self):
        self.db.cleanup()

    def _seed_basic(self, session):
        _event(session, "WINR", "earnings_beat_structured", "bullish", magnitude=8.0)
        _event(session, "LOSR", "earnings_beat_structured", "bullish", magnitude=1.0)
        _event(session, "SHRT", "earnings_miss_structured", "bearish", magnitude=6.0)
        _event(session, "HOLD", "analyst_upgrade_cluster", "bullish",
               magnitude=2.0, source_type="search")
        session.flush()

    def test_neutral_events_skipped(self):
        with get_session() as session:
            _event(session, "WINR", "product_launch", "neutral")
            session.flush()
            events = load_events(session, None, None, today=EVENT_DATE + timedelta(days=60))
            plans, stats = build_plans(session, events, self.provider, self.settings.max_holding_days)
        self.assertEqual(stats.skipped_neutral, 1)
        self.assertEqual(stats.built, 0)

    def test_class_table_deterministic(self):
        with get_session() as session:
            self._seed_basic(session)
            result = run_event_replay(
                session, self.settings, price_provider=self.provider,
                today=EVENT_DATE + timedelta(days=60),
            )
        by_type = result["report"]["by_event_type"]
        # earnings_beat_structured: WINR win + LOSR loss -> n=2, win_rate 0.5
        beat = by_type["earnings_beat_structured"]
        self.assertEqual(beat["n"], 2)
        self.assertEqual(beat["win_rate"], 0.5)
        self.assertFalse(beat["bias_flag"])
        # earnings_miss_structured: SHRT short win -> n=1, win_rate 1.0 -> bias flag
        miss = by_type["earnings_miss_structured"]
        self.assertEqual(miss["n"], 1)
        self.assertEqual(miss["win_rate"], 1.0)
        self.assertTrue(miss["bias_flag"])
        # source_type split present
        self.assertIn("fmp_structured", result["report"]["by_source_type"])
        self.assertIn("search", result["report"]["by_source_type"])
        # magnitude bucket grouping present
        self.assertTrue(any("|" in k for k in result["report"]["by_type_and_magnitude"]))

    def test_win_and_loss_pnl_signs(self):
        with get_session() as session:
            self._seed_basic(session)
            events = load_events(session, None, None, today=EVENT_DATE + timedelta(days=60))
            plans, _ = build_plans(session, events, self.provider, self.settings.max_holding_days)
            params = TradeParams(self.settings)
            from backtest.event_replay import replay_plans
            trades = {t.ticker: t for t in replay_plans(plans, params, self.settings.max_holding_days)}
        self.assertGreater(trades["WINR"].result.pnl_pct, 0)
        self.assertLess(trades["LOSR"].result.pnl_pct, 0)
        self.assertEqual(trades["LOSR"].result.rule_fired, "stop")
        self.assertGreater(trades["SHRT"].result.pnl_pct, 0)   # short win
        self.assertIn(trades["HOLD"].result.rule_fired, ("time", "t1_then_time"))

    def test_class_filter(self):
        with get_session() as session:
            self._seed_basic(session)
            events = load_events(session, ["upgrades"], None, today=EVENT_DATE + timedelta(days=60))
            types = {e.event_type for e in events}
        self.assertEqual(types, {"analyst_upgrade_cluster"})


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sweep")
        self.settings = Settings()
        self.provider = _Provider({"WINR": _make_bars(EVENT_DATE, 100, WIN_LONG)})

    def tearDown(self):
        self.db.cleanup()

    def test_sweep_grid_shape_and_caution(self):
        with get_session() as session:
            _event(session, "WINR", "earnings_beat_structured", "bullish")
            session.flush()
            events = load_events(session, None, None, today=EVENT_DATE + timedelta(days=60))
            plans, _ = build_plans(session, events, self.provider, self.settings.max_holding_days)
            sweep = run_sweep(plans, TradeParams(self.settings))
        data = sweep["earnings_beat_structured"]
        self.assertEqual(len(data["combos"]), len(STOP_WIDTH_MULTS) * len(TARGET_MULTS) * len(HOLD_DAYS_GRID))
        self.assertEqual(len(data["combos"]), 27)
        self.assertIsNotNone(data["best"])
        self.assertTrue(data["small_sample"])   # n=1 < 30 -> caution


class ShadowAdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.provider = _Provider({
            "WINR": _make_bars(EVENT_DATE, 100, WIN_LONG),
            "LOSR": _make_bars(EVENT_DATE, 100, LOSE_LONG),
        })

    def test_skips_cleanly_when_table_absent(self):
        # A bare in-memory DB with NO tables at all -> clean skip.
        eng = create_engine("sqlite://")
        session = sessionmaker(bind=eng)()
        out = replay_shadow_ledger(session, self.provider, self.settings, TradeParams(self.settings))
        self.assertEqual(out["status"], "skipped_table_absent")

    def test_replays_ledger_rows_when_present(self):
        self.addCleanup(init_test_db("shadow").cleanup)
        scored_at = datetime(2025, 1, 15, 14, 0, 0)
        with get_session() as session:
            session.add(ScoredCandidate(
                ticker="WINR", scored_at=scored_at, direction="bullish", final_score=0.7,
                suggested_stop=95.0, target_1=110.0, cohort="memo",
            ))
            session.add(ScoredCandidate(
                ticker="LOSR", scored_at=scored_at, direction="bullish", final_score=0.4,
                suggested_stop=95.0, target_1=110.0, cohort="exploration",
            ))
            session.flush()
            out = replay_shadow_ledger(session, self.provider, self.settings, TradeParams(self.settings))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["overall"]["n"], 2)
        self.assertIn("memo", out["by_cohort"])
        self.assertIn("exploration", out["by_cohort"])
        # 0.65-1.01 bucket for the 0.7 score row.
        self.assertTrue(any(k.startswith("0.65") for k in out["by_score_bucket"]))


class RenderTests(unittest.TestCase):
    def test_markdown_renders(self):
        from backtest.run_event_replay import render_markdown
        self.addCleanup(init_test_db("render").cleanup)
        settings = Settings()
        provider = _Provider({"WINR": _make_bars(EVENT_DATE, 100, WIN_LONG)})
        with get_session() as session:
            _event(session, "WINR", "earnings_beat_structured", "bullish")
            session.flush()
            result = run_event_replay(session, settings, price_provider=provider,
                                      with_sweep=True, with_shadow=True,
                                      today=EVENT_DATE + timedelta(days=60))
        md = render_markdown(result)
        self.assertIn("# Event-replay backtest", md)
        self.assertIn("Per class", md)
        self.assertIn("J3 — parameter sensitivity", md)
        self.assertIn("J4 — shadow calibration ledger", md)


if __name__ == "__main__":
    unittest.main()
