"""Hand-computed unit tests for the exit-engine simulator (Spec J1).

Every expected value below is worked out by hand from the bar sequence so the
test fails when the exit semantics drift from order_monitor's, not merely when
numbers change. Slippage is 0 in the semantic cases (verified separately).
"""

from __future__ import annotations

import unittest
from collections import namedtuple
from datetime import date, timedelta

from backtest.simulator import (
    RULE_STOP,
    RULE_T1_THEN_STOP,
    RULE_T1_THEN_T2,
    RULE_T1_THEN_TIME,
    RULE_TIME,
    simulate_trade,
)

Bar = namedtuple("Bar", "date open high low close")
D0 = date(2024, 1, 1)


def _bars(rows):
    """rows: list of (open, high, low, close) on consecutive calendar days from D0."""
    return [Bar(D0 + timedelta(days=i), o, h, l, c) for i, (o, h, l, c) in enumerate(rows)]


class GapThroughStopTests(unittest.TestCase):
    def test_long_gap_through_stop_fills_at_open_not_stop(self):
        # Signal idx0; entry idx1 open=100. Bar idx2 gaps to open 93, below stop 95.
        bars = _bars([
            (100, 101, 99, 100),   # 0 signal
            (100, 101, 98, 99),    # 1 entry (T+1 open=100), no touch
            (93, 94, 90, 92),      # 2 gap-through stop -> fill at open 93
        ])
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=115, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_STOP)
        self.assertAlmostEqual(r.exit_price, 93.0)          # open, NOT the 95 stop
        self.assertAlmostEqual(r.entry_price, 100.0)
        self.assertAlmostEqual(r.pnl_pct, -7.0)
        self.assertEqual(r.exit_date, D0 + timedelta(days=2))
        self.assertEqual(r.holding_days, 1)
        self.assertAlmostEqual(r.mfe, 1.0)                  # (101-100)/100 on entry bar
        self.assertAlmostEqual(r.mae, -10.0)               # (90-100)/100 on gap bar

    def test_long_intrabar_stop_fills_at_stop_price(self):
        bars = _bars([
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94, 95),     # low 94 crosses stop 95, open 99 above -> fill at 95
        ])
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=115, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_STOP)
        self.assertAlmostEqual(r.exit_price, 95.0)
        self.assertAlmostEqual(r.pnl_pct, -5.0)


class TargetSequenceTests(unittest.TestCase):
    def test_t1_then_t2_blended_exit(self):
        bars = _bars([
            (100, 101, 99, 100),   # 0 signal
            (100, 108, 98, 107),   # 1 entry open=100, no target yet
            (109, 112, 108, 111),  # 2 T1 hit at 110 (open 109 < 110)
            (113, 116, 112, 115),  # 3 T2 hit at 115 (open 113 < 115)
        ])
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=115, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_T1_THEN_T2)
        # blended = 0.5*110 + 0.5*115 = 112.5
        self.assertAlmostEqual(r.exit_price, 112.5)
        self.assertAlmostEqual(r.pnl_pct, 12.5)
        self.assertEqual(r.exit_date, D0 + timedelta(days=3))
        self.assertEqual(r.holding_days, 2)
        self.assertEqual([leg[2] for leg in r.legs], ["t1", "t2"])

    def test_t1_then_time_remainder_exits_at_close(self):
        rows = [
            (100, 101, 99, 100),   # 0 signal
            (100, 111, 99, 110),   # 1 entry open=100, T1 hit at 110
            (110, 112, 108, 109),  # 2
            (109, 111, 108, 110),  # 3
            (110, 111, 108, 109),  # 4
            (109, 110, 108, 109),  # 5
            (109, 110, 108, 109),  # 6 time-exit bar (date >= entry+5d), close 109
            (109, 110, 108, 109),  # 7 (never reached)
        ]
        bars = _bars(rows)
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=200, max_holding_days=5, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_T1_THEN_TIME)
        # blended = 0.5*110 (T1) + 0.5*109 (close of the time-exit bar)
        self.assertAlmostEqual(r.exit_price, 109.5)
        self.assertAlmostEqual(r.pnl_pct, 9.5)
        self.assertEqual(r.exit_date, D0 + timedelta(days=6))  # entry D1 + 5 calendar days
        self.assertEqual(r.holding_days, 5)


class SameBarConflictTests(unittest.TestCase):
    def test_same_bar_stop_and_target_resolves_pessimistically_stop_first(self):
        bars = _bars([
            (100, 101, 99, 100),   # 0 signal
            (100, 106, 94, 100),   # 1 entry: range hits BOTH stop 95 and target1 105
        ])
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=105, target_2=110, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_STOP)      # stop wins, not t1
        self.assertAlmostEqual(r.exit_price, 95.0)
        self.assertAlmostEqual(r.pnl_pct, -5.0)
        self.assertEqual(r.holding_days, 0)


class TimeExitTests(unittest.TestCase):
    def test_pure_time_exit_no_target_no_stop(self):
        rows = [(100, 101, 99, 100)] + [(100, 102, 98, 101)] * 6
        bars = _bars(rows)
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=90,
                           target_1=130, target_2=140, max_holding_days=5, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_TIME)
        self.assertAlmostEqual(r.exit_price, 101.0)     # close of the time-exit bar
        self.assertAlmostEqual(r.pnl_pct, 1.0)


class ShortTests(unittest.TestCase):
    def test_short_gap_through_stop_fills_at_open(self):
        bars = _bars([
            (100, 101, 99, 100),   # 0 signal
            (100, 104, 96, 98),    # 1 entry short open=100
            (107, 108, 106, 107),  # 2 gap UP through stop 105 -> fill at open 107
        ])
        r = simulate_trade(bars, entry_idx=0, direction="short", stop=105,
                           target_1=90, target_2=85, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_STOP)
        self.assertAlmostEqual(r.exit_price, 107.0)
        self.assertAlmostEqual(r.pnl_pct, -7.0)         # (100-107)/100 for a short
        self.assertAlmostEqual(r.mfe, 4.0)              # (100-96)/100
        self.assertAlmostEqual(r.mae, -8.0)             # (100-108)/100

    def test_short_target_sequence(self):
        bars = _bars([
            (100, 101, 99, 100),   # 0 signal
            (100, 101, 89, 92),    # 1 entry short; low 89 hits T1 at 90
            (88, 89, 84, 85),      # 2 low 84 hits T2 at 85
        ])
        r = simulate_trade(bars, entry_idx=0, direction="short", stop=110,
                           target_1=90, target_2=85, max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_T1_THEN_T2)
        # T1 fill 90 (open 100 not below 90), T2 gap: open 88 <= 85? no -> 85
        self.assertAlmostEqual(r.exit_price, 87.5)      # 0.5*90 + 0.5*85
        self.assertAlmostEqual(r.pnl_pct, 12.5)


class SlippageTests(unittest.TestCase):
    def test_slippage_worsens_entry_and_exit(self):
        bars = _bars([
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (99, 100, 94, 95),     # stop 95 hit intrabar
        ])
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=115, max_holding_days=20, slippage_bps=10)
        # entry 100 * 1.001 = 100.1 ; stop sell 95 * 0.999 = 94.905
        self.assertAlmostEqual(r.entry_price, 100.1)
        self.assertAlmostEqual(r.exit_price, 94.905)
        self.assertAlmostEqual(r.pnl_pct, round((94.905 - 100.1) / 100.1 * 100, 2))


class TargetlessRemainderTests(unittest.TestCase):
    def test_t1_then_time_when_target_2_absent(self):
        # J4 shape: only a single target stored -> remainder rides to time exit.
        rows = [
            (100, 101, 99, 100),   # 0 signal
            (100, 111, 99, 110),   # 1 entry, T1 at 110
        ] + [(110, 112, 108, 110)] * 5  # 2..6
        bars = _bars(rows)
        r = simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=None, max_holding_days=5, slippage_bps=0)
        self.assertEqual(r.rule_fired, RULE_T1_THEN_TIME)
        self.assertEqual([leg[2] for leg in r.legs], ["t1", "time"])


class GuardTests(unittest.TestCase):
    def test_no_bar_after_signal_raises(self):
        bars = _bars([(100, 101, 99, 100)])
        with self.assertRaises(ValueError):
            simulate_trade(bars, entry_idx=0, direction="long", stop=95,
                           target_1=110, target_2=115, max_holding_days=20)


if __name__ == "__main__":
    unittest.main()
