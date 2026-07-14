"""Tests for the event-anchored replay, sweep, and shadow-ledger adapter (J2–J4).

Uses seeded synthetic events + a fake price cache so outcomes are deterministic
and offline (no yfinance/FMP).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from backtest.event_replay import (
    _sweep_levels,
    aggregate,
    replay_events,
    replay_shadow_ledger,
    run_sweep,
    shadow_ledger_available,
)
from data.event_outcomes import PriceBar
from database.db import get_session, init_db
from database.models import HistoricalEvent, ScoredCandidate
from memo.generator import MemoGenerator


def _settings(**over):
    base = dict(
        default_stop_loss_pct=0.05, max_stop_loss_pct=0.08, max_holding_days=20,
        portfolio_value=100_000.0, base_position_pct=0.05,
        min_position_pct=0.02, max_position_pct=0.10,
        backtest_slippage_bps=10.0, pattern_price_source="fmp", fmp_api_key="",
    )
    base.update(over)
    return SimpleNamespace(**base)


SIGNAL_DATE = date(2025, 3, 3)


def _win_bars() -> list[PriceBar]:
    """Flat ~100 warmup (ATR→default 5% stop), then ramp through both targets."""
    bars = []
    start = SIGNAL_DATE - timedelta(days=20)
    for i in range(20):  # warmup incl. the signal bar (last one)
        d = start + timedelta(days=i)
        bars.append(PriceBar(d, 100, 100.5, 99.5, 100, 1_000))
    # forward bars (T+1 onward) ramp up: entry ~100.1, stop ~95.1, t1 ~110.1, t2 ~115.1
    forward = [(100, 105, 99, 104), (104, 111, 103, 110), (110, 116, 109, 115)]
    for j, (o, h, l, c) in enumerate(forward, start=1):
        bars.append(PriceBar(SIGNAL_DATE + timedelta(days=j), o, h, l, c, 1_000))
    return bars


def _lose_bars() -> list[PriceBar]:
    bars = []
    start = SIGNAL_DATE - timedelta(days=20)
    for i in range(20):
        d = start + timedelta(days=i)
        bars.append(PriceBar(d, 100, 100.5, 99.5, 100, 1_000))
    forward = [(100, 101, 99, 100), (99, 100, 94, 95)]  # drops through the ~95.1 stop
    for j, (o, h, l, c) in enumerate(forward, start=1):
        bars.append(PriceBar(SIGNAL_DATE + timedelta(days=j), o, h, l, c, 1_000))
    return bars


class FakePriceCache:
    def __init__(self, ticker_bars: dict[str, list[PriceBar]]):
        self.ticker_bars = ticker_bars

    def get_bars(self, ticker, start, end, session=None):
        return [b for b in self.ticker_bars.get(ticker.upper(), []) if start <= b.date <= end]


def _mk_event(ticker, event_type, polarity, magnitude, source_type="fmp_structured"):
    return HistoricalEvent(
        ticker=ticker, event_type=event_type, event_date=SIGNAL_DATE, polarity=polarity,
        magnitude=magnitude, source_type=source_type,
        dedupe_key=f"{ticker}:{event_type}:{SIGNAL_DATE}", headline="x", summary="y",
    )


class EventReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'replay.db'}")
        self.settings = _settings()
        self.memo = MemoGenerator(self.settings)
        # 5 long events: earnings group (2 win, 1 lose), upgrade group (2 win).
        self.events = [
            _mk_event("EW1", "earnings_beat_structured", "bullish", 2.5),
            _mk_event("EW2", "earnings_beat_structured", "bullish", 6.0),
            _mk_event("EL1", "earnings_beat_structured", "bullish", 0.5),
            _mk_event("UW1", "analyst_upgrade_cluster", "bullish", 4.0),
            _mk_event("UW2", "analyst_upgrade_cluster", "bullish", 12.0),
        ]
        with get_session() as s:
            for e in self.events:
                s.add(e)
        self.cache = FakePriceCache({
            "EW1": _win_bars(), "EW2": _win_bars(), "UW1": _win_bars(), "UW2": _win_bars(),
            "EL1": _lose_bars(),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _replay(self):
        with get_session() as s:
            return replay_events(s, self.settings, price_cache=self.cache, memo_gen=self.memo)

    def test_replay_produces_deterministic_class_table(self):
        records, contexts, skipped = self._replay()
        self.assertEqual(len(records), 5)
        self.assertEqual(len(contexts), 5)
        self.assertEqual(skipped, {})

        agg = aggregate(records)
        et = agg["by_event_type"]
        self.assertEqual(et["earnings_beat_structured"]["n"], 3)
        self.assertAlmostEqual(et["earnings_beat_structured"]["win_rate"], 2 / 3, places=3)
        self.assertFalse(et["earnings_beat_structured"]["win_rate_bias_flag"])
        self.assertEqual(et["analyst_upgrade_cluster"]["n"], 2)
        self.assertEqual(et["analyst_upgrade_cluster"]["win_rate"], 1.0)
        self.assertTrue(et["analyst_upgrade_cluster"]["win_rate_bias_flag"])  # >75%

        # Winners fire t1_then_t2; the loser fires a stop.
        self.assertEqual(
            et["analyst_upgrade_cluster"]["rule_dist"], {"t1_then_t2": 2}
        )
        self.assertEqual(agg["overall"]["n"], 5)

        # source_type grouping present.
        self.assertIn("fmp_structured", agg["by_source_type"])

    def test_replay_is_reproducible(self):
        a1 = aggregate(self._replay()[0])
        a2 = aggregate(self._replay()[0])
        self.assertEqual(a1, a2)

    def test_neutral_polarity_skipped(self):
        with get_session() as s:
            s.add(_mk_event("NEU", "earnings_beat_structured", "neutral", 1.0))
        records, _, skipped = self._replay()
        self.assertEqual(len(records), 5)  # neutral excluded
        self.assertEqual(skipped.get("neutral_polarity"), 1)

    def test_classes_filter(self):
        with get_session() as s:
            recs, _, _ = replay_events(
                s, self.settings, classes=["upgrades"], price_cache=self.cache, memo_gen=self.memo
            )
        self.assertEqual({r.event_type for r in recs}, {"analyst_upgrade_cluster"})

    def test_sweep_grid_shape_and_caution(self):
        _, contexts, _ = self._replay()
        sweep = run_sweep(contexts, slippage_bps=10.0)
        self.assertEqual(sweep["grid"]["combo_count"], 27)  # 3 * 3 * 3
        for cls, data in sweep["per_class"].items():
            self.assertEqual(len(data["combos"]), 27)
            self.assertTrue(data["caution_small_sample"])  # n < 30
            self.assertIsNotNone(data["best"])
            self.assertIn("stop_mult", data["best"]["combo"])

    def test_sweep_baseline_reproduces_memo_levels(self):
        # _sweep_levels at (1.0, 1.0) must equal the memo's stop/target exactly.
        params = self.memo._compute_trade_params(
            100.0, 0.0, {}, 0.0, "moderate", direction="long"
        )
        stop, t1, t2 = _sweep_levels(
            params["entry_price"], params["stop_loss"], params["target_1"], params["target_2"],
            is_long=True, stop_mult=1.0, target_scale=1.0,
        )
        self.assertEqual((stop, t1, t2), (params["stop_loss"], params["target_1"], params["target_2"]))


class ShadowLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'shadow.db'}")
        self.settings = _settings()

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_table_skips_cleanly(self):
        # init_db creates scored_candidates (Spec I model); drop it to exercise
        # the pre-Spec-I / other-DB skip path.
        with get_session() as s:
            s.execute(text("DROP TABLE scored_candidates"))
        with get_session() as s:
            self.assertFalse(shadow_ledger_available(s))
            self.assertIsNone(replay_shadow_ledger(s, self.settings))

    def test_present_table_replays_real_schema(self):
        # Real Spec I schema: suggested_stop + single target_1, final_score, cohort.
        # A neutral-direction row must be skipped.
        with get_session() as s:
            s.add(ScoredCandidate(
                ticker="UW1", scored_at=datetime(SIGNAL_DATE.year, SIGNAL_DATE.month, SIGNAL_DATE.day),
                direction="long", suggested_stop=95.1, target_1=110.1,
                cohort="exploration", final_score=0.7,
            ))
            s.add(ScoredCandidate(
                ticker="NEU", scored_at=datetime(SIGNAL_DATE.year, SIGNAL_DATE.month, SIGNAL_DATE.day),
                direction="neutral", suggested_stop=95.1, target_1=110.1,
                cohort="below", final_score=0.3,
            ))
        cache = FakePriceCache({"UW1": _win_bars()})
        with get_session() as s:
            self.assertTrue(shadow_ledger_available(s))
            result = replay_shadow_ledger(s, self.settings, price_cache=cache)
        self.assertIsNotNone(result)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["replayed"], 1)          # neutral skipped
        self.assertIn("exploration", result["by_cohort"])
        self.assertIn("0.6-0.8", result["by_score_bucket"])  # final_score 0.7


if __name__ == "__main__":
    unittest.main()
