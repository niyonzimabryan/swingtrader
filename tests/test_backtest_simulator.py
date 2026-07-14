"""Unit tests for the exit-engine simulator (Spec J1).

Every sequence is hand-computed. Slippage is 0 unless a test targets it, so the
expected fills are exact.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from backtest.simulator import Bar, simulate_trade

D0 = date(2025, 1, 1)


def _bars(rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    """Build a bar list from (open, high, low, close) rows on consecutive days."""
    return [Bar(date=D0 + timedelta(days=i), open=o, high=h, low=l, close=c)
            for i, (o, h, l, c) in enumerate(rows)]


class SimulatorTest(unittest.TestCase):
    def test_entry_is_t1_open_not_signal_close(self):
        # Signal bar is idx0; entry must fill at idx1's OPEN (102), never idx0 close.
        bars = _bars([
            (100, 100, 100, 100),   # signal
            (102, 103, 101, 102),   # T+1 entry bar
            (150, 151, 149, 150),   # far target bar
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.entry_price, 102.0)
        self.assertEqual(r.entry_date, bars[1].date)

    def test_t1_then_t2(self):
        bars = _bars([
            (100, 100, 100, 100),   # signal
            (100, 102, 99, 101),    # entry @100
            (105, 112, 104, 111),   # T1 hit (high 112 >= 110); no T2 (high < 120)
            (112, 121, 111, 120),   # T2 hit (high 121 >= 120)
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, "t1_then_t2")
        self.assertEqual(r.exit_price, 115.0)     # 0.5*110 + 0.5*120
        self.assertEqual(r.pnl_pct, 15.0)
        self.assertEqual(r.exit_date, bars[3].date)
        self.assertEqual(r.holding_days, 2)       # idx3 - fill_idx(1)
        self.assertEqual(r.mfe_pct, 21.0)         # high 121 vs entry 100
        self.assertEqual(r.mae_pct, -1.0)         # low 99 vs entry 100

    def test_gap_through_stop_fills_at_open(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry @100
            (93, 94, 92, 93),       # gaps below stop 95 → fill at open 93, not 95
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, "stop")
        self.assertEqual(r.exit_price, 93.0)      # open, not the 95 stop price
        self.assertEqual(r.pnl_pct, -7.0)
        self.assertEqual(r.holding_days, 1)
        self.assertEqual(r.mae_pct, -8.0)         # low 92

    def test_t1_then_time_exit(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry @100
            (105, 112, 104, 111),   # T1 @110
            (111, 115, 110, 113),   # rides
            (113, 118, 112, 116),   # end bar (fill_idx 1 + max_hold 3) → time exit @ close 116
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=130,
                           max_holding_days=3, slippage_bps=0)
        self.assertEqual(r.rule_fired, "t1_then_time")
        self.assertEqual(r.exit_price, 113.0)     # 0.5*110 + 0.5*116
        self.assertEqual(r.exit_date, bars[4].date)
        self.assertEqual(r.holding_days, 3)

    def test_same_bar_stop_and_target_is_pessimistic_stop(self):
        # High touches T1 (110) and low touches stop (95) on the same bar with no
        # gap at the open → pessimistic: booked as a full stop, no partial T1.
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry @100
            (100, 111, 94, 96),     # both T1 and stop; open 100 (no gap)
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, "stop")
        self.assertEqual(r.exit_price, 95.0)
        self.assertEqual(r.pnl_pct, -5.0)

    def test_same_bar_conflict_after_t1_is_t1_then_stop(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry @100
            (105, 111, 104, 110),   # T1 @110 only (high 111 < T2 112)
            (108, 113, 94, 96),     # T2 (112) and stop (95) same bar → stop wins
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=112,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, "t1_then_stop")
        self.assertEqual(r.exit_price, 102.5)     # 0.5*110 + 0.5*95
        self.assertEqual(r.pnl_pct, 2.5)

    def test_pure_time_exit_no_targets(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry @100
            (101, 103, 100, 102),
            (102, 104, 101, 103),   # end bar → time exit @ close 103
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=130, target_2=140,
                           max_holding_days=2, slippage_bps=0)
        self.assertEqual(r.rule_fired, "time")
        self.assertEqual(r.exit_price, 103.0)
        self.assertEqual(r.pnl_pct, 3.0)
        self.assertEqual(r.holding_days, 2)

    def test_short_direction_t1_then_t2(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 101, 99, 100),    # entry @100 (short)
            (95, 96, 89, 90),       # T1 @90 (low 89 <= 90); stop 105 untouched
            (85, 86, 79, 80),       # T2 @80 (low 79 <= 80)
        ])
        r = simulate_trade(bars, 0, "short", stop=105, target_1=90, target_2=80,
                           max_holding_days=20, slippage_bps=0)
        self.assertEqual(r.rule_fired, "t1_then_t2")
        self.assertEqual(r.exit_price, 85.0)      # 0.5*90 + 0.5*80
        self.assertEqual(r.pnl_pct, 15.0)         # short: (100 - 85)/100

    def test_slippage_worsens_entry_and_stop(self):
        bars = _bars([
            (100, 100, 100, 100),
            (100, 102, 99, 101),    # entry open 100 → 100.1 with 10 bps
            (93, 94, 92, 93),       # gap stop → open 93 → 92.9907 with 10 bps
        ])
        r = simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=10)
        self.assertAlmostEqual(r.entry_price, 100.1, places=4)
        self.assertAlmostEqual(r.exit_price, 93 * (1 - 0.001), places=4)
        # pnl is negative and slightly worse than the frictionless -7%.
        self.assertLess(r.pnl_pct, -7.0)

    def test_no_t1_bar_returns_none(self):
        bars = _bars([(100, 100, 100, 100)])  # only the signal bar, no T+1
        self.assertIsNone(
            simulate_trade(bars, 0, "long", stop=95, target_1=110, target_2=120,
                           max_holding_days=20, slippage_bps=0)
        )


if __name__ == "__main__":
    unittest.main()
