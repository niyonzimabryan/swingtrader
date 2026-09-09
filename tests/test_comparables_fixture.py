"""The Spec N §5.0 fixture, reproduced to the cent.

Every expected number in this file is copied from
`docs/investment-workspace/comparables-fixture.md`, which was written first and
derived by hand. If a number here changes, the doc changes with it or the change
is a bug.
"""

from __future__ import annotations

import unittest
from datetime import date

from backtest.simulator import simulate_trade

from comparables import fixtures as fx
from comparables.outcomes import (
    CostModel,
    benchmark_return,
    calendar_time_alpha,
    calendar_time_series,
    car,
    delisting_rate,
    event_return,
    horizon_outcomes,
    maturity,
    policy_aggregate,
    policy_bars,
    policy_outcome,
    provenance_mix,
)

PCT = 100.0
#: "to the cent" is 0.01 percentage points; the arithmetic actually agrees far
#: closer than that, so the tests assert the tighter bound.
TIGHT = 1e-9


class FixtureArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.calendar = fx.CALENDAR
        self.benchmark = fx.benchmark_series()
        self.events = fx.cohort()
        self.by_id = {e.event_id: e for e in self.events}

    # -- §6.2 of the doc ---------------------------------------------------- #

    def test_headline_fixture_to_the_cent(self):
        expected_raw = {
            ("AAA", 1): 1.0, ("AAA", 5): 4.0, ("AAA", 10): 7.0,
            ("BBB", 1): -1.0, ("BBB", 5): -3.0, ("BBB", 10): -5.0,
            ("CCC", 1): 2.0, ("CCC", 5): 5.0, ("CCC", 10): 10.0,
            ("DDD", 1): 2.0, ("DDD", 5): 2.0, ("DDD", 10): 8.0,
            ("EEE", 1): -1.0, ("EEE", 5): -5.0, ("EEE", 10): -58.6,
            ("FFF", 1): 1.0, ("FFF", 5): 4.0, ("FFF", 10): 8.0,
        }
        expected_car = {
            ("AAA", 1): 0.702085, ("AAA", 5): 3.848028, ("AAA", 10): 6.321705,
            ("BBB", 1): -1.297915, ("BBB", 5): -3.102941, ("BBB", 10): -5.538262,
            ("CCC", 1): 1.702085, ("CCC", 5): 4.831260, ("CCC", 10): 9.169247,
            ("DDD", 1): 1.901283, ("DDD", 5): 1.750966, ("DDD", 10): 7.379494,
            ("EEE", 1): -1.098717, ("EEE", 5): -5.329215, ("EEE", 10): -63.706938,
            ("FFF", 1): 0.901283, ("FFF", 5): 3.663764, ("FFF", 10): 7.276543,
        }
        for (ticker, h), want in expected_raw.items():
            got = event_return(self.by_id[ticker], self.calendar, h) * PCT
            self.assertAlmostEqual(got, want, places=9, msg=f"R_i({h}) for {ticker}")
        for (ticker, h), want in expected_car.items():
            got = car(self.by_id[ticker], self.benchmark, self.calendar, h) * PCT
            self.assertAlmostEqual(got, want, places=6, msg=f"CAR({h}) for {ticker}")

        # Benchmark returns over the identical sessions (doc §6.2).
        expected_bench = {
            (11, 1): 0.297915, (11, 5): 0.099305, (11, 10): 0.496524,
            (19, 1): 0.098717, (19, 5): 0.296150, (19, 10): 0.493583,
        }
        for (zero, h), want in expected_bench.items():
            got = benchmark_return(self.benchmark, self.calendar, zero, h) * PCT
            self.assertAlmostEqual(got, want, places=6, msg=f"R_m({h}) at t={zero}")

        # Cross-sectional means (doc §6.3).
        expected_means = {
            1: (0.666667, 0.198316, 0.468351),
            5: (1.166667, 0.197727, 0.943644),
            10: (-5.100000, 0.495054, -6.516368),
        }
        for h, (raw, bench, mean_car) in expected_means.items():
            out = horizon_outcomes(self.events, self.benchmark, self.calendar, h)
            self.assertAlmostEqual(out.mean_raw * PCT, raw, places=6)
            self.assertAlmostEqual(out.mean_benchmark * PCT, bench, places=6)
            self.assertAlmostEqual(out.mean_car * PCT, mean_car, places=6)
            self.assertEqual(out.n_matured, 6)
            self.assertEqual(out.n_censored, 0)

        # Calendar-time portfolio and the alpha x h headline (doc §7.3).
        expected_ct = {
            1: (2, 1.0, False, 0.468351, 0.468351),
            5: (10, -0.257249404, True, 0.238669, 1.193345),
            10: (18, -5.840135951, True, -0.369174, -3.691740),
        }
        for h, (n, beta, estimated, alpha_d, alpha_h) in expected_ct.items():
            series = calendar_time_series(self.events, self.benchmark, self.calendar, h)
            result = calendar_time_alpha(series, h)
            self.assertEqual(result.n_sessions, n)
            self.assertAlmostEqual(result.beta, beta, places=9)
            self.assertIs(result.beta_estimated, estimated)
            self.assertAlmostEqual(result.alpha_daily * PCT, alpha_d, places=6)
            self.assertAlmostEqual(result.alpha_times_h * PCT, alpha_h, places=6)

        # The h=10 portfolio holds six names on the two overlapping sessions.
        series = calendar_time_series(self.events, self.benchmark, self.calendar, 10)
        self.assertEqual(series.holdings,
                         (3, 3, 3, 3, 3, 3, 3, 3, 6, 6, 3, 3, 3, 3, 3, 3, 3, 3))
        self.assertAlmostEqual(series.abnormal[15] * PCT, -18.616805, places=6)

        # Composition (doc §9).
        self.assertAlmostEqual(delisting_rate(self.events), 1.0 / 6.0, places=12)
        self.assertEqual(provenance_mix(self.events),
                         {"observed_live": 2, "vendor_pit": 4,
                          "archival_reconstructed": 0})

    # -- §2 of the doc ------------------------------------------------------ #

    def test_horizons_are_sessions(self):
        """A 5- and a 10-session horizon step over the weekend and MLK Day."""
        zero = self.calendar.session_zero(self.by_id["AAA"].known_at_utc)
        self.assertEqual(self.calendar.sessions[zero], date(2024, 1, 4))

        self.assertEqual(
            self.calendar.window(zero, 5),
            (date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8),
             date(2024, 1, 9), date(2024, 1, 10)),
        )
        ten = self.calendar.window(zero, 10)
        self.assertEqual(
            ten,
            (date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9),
             date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12),
             date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18)),
        )
        for holiday in fx.HOLIDAYS:
            self.assertNotIn(holiday, ten)
        # Ten sessions, fifteen calendar days: a calendar-day horizon would have
        # ended on Saturday 2024-01-13, two sessions early.
        self.assertEqual((ten[-1] - ten[0]).days, 14)
        self.assertEqual(len(ten), 10)

    # -- §10 of the doc ----------------------------------------------------- #

    def test_delisting_contributes_loss(self):
        with_terminal = horizon_outcomes(self.events, self.benchmark, self.calendar, 10)
        without = horizon_outcomes(fx.cohort_without_terminal(), self.benchmark,
                                   self.calendar, 10)
        self.assertAlmostEqual(with_terminal.mean_car * PCT, -6.516368, places=6)
        self.assertAlmostEqual(without.mean_car * PCT, 2.650298, places=6)
        self.assertGreater(abs(with_terminal.mean_car - without.mean_car), 0.09)

        # The delisting is matured, not censored, in both readings.
        self.assertEqual(with_terminal.n_censored, 0)
        self.assertEqual(without.n_censored, 0)
        self.assertEqual(with_terminal.n_matured, 6)

        # Session 7 is outside the five-session window, so h=5 is unmoved.
        h5_with = horizon_outcomes(self.events, self.benchmark, self.calendar, 5)
        h5_without = horizon_outcomes(fx.cohort_without_terminal(), self.benchmark,
                                      self.calendar, 5)
        self.assertAlmostEqual(h5_with.mean_car, h5_without.mean_car, places=12)
        self.assertAlmostEqual(h5_with.mean_car * PCT, 0.943644, places=6)

    def test_delisted_names_retained(self):
        """The to-zero name stays in; removing it changes the mean."""
        retained = horizon_outcomes(self.events, self.benchmark, self.calendar, 10)
        self.assertIn("EEE", retained.event_ids)

        survivors = [e for e in self.events if e.event_id != "EEE"]
        pruned = horizon_outcomes(survivors, self.benchmark, self.calendar, 10)
        self.assertNotIn("EEE", pruned.event_ids)
        self.assertAlmostEqual(pruned.mean_car * PCT, 4.921745, places=6)
        self.assertAlmostEqual(retained.mean_car * PCT, -6.516368, places=6)
        self.assertGreater(abs(pruned.mean_car - retained.mean_car), 0.11)

        # And the survivors-only cohort has a delisting rate of zero, which is
        # the composition smell §4.2 exists to name.
        self.assertAlmostEqual(delisting_rate(survivors), 0.0, places=12)
        self.assertAlmostEqual(delisting_rate(self.events), 1 / 6, places=12)

    def test_delisting_return_applied(self):
        """The configured terminal return, not the last print."""
        from comparables import config
        from comparables.outcomes import event_daily_returns

        eee = self.by_id["EEE"]
        self.assertEqual(eee.terminal.reason, "performance_nasdaq")
        self.assertEqual(eee.terminal.terminal_return,
                         config.DELISTING_TERMINAL_RETURN["performance_nasdaq"])
        self.assertEqual(eee.terminal.terminal_return, -0.55)
        self.assertTrue(eee.terminal.resolved)

        daily = event_daily_returns(eee, self.calendar, 10)
        self.assertAlmostEqual(daily[7], -0.55, places=12)
        self.assertEqual(daily[8], 0.0)         # carried flat
        self.assertEqual(daily[9], 0.0)
        self.assertAlmostEqual(event_return(eee, self.calendar, 10), -0.586, places=12)

        # The last print was 9.20; 9.20 x 0.45 = 4.14, and 9.20/10 - 1 = -8%,
        # which is emphatically not what the cohort records.
        last_print = eee.series.total_return[eee.series.index_of(date(2024, 1, 25))]
        self.assertEqual(last_print.close, 9.20)
        self.assertNotAlmostEqual(event_return(eee, self.calendar, 10), -0.08, places=4)

    def test_three_price_series_stored(self):
        """Raw, split-adjusted and total-return, plus factors with ex-dates."""
        for event in self.events:
            series = event.series
            self.assertEqual(len(series.raw), len(series.split_adjusted))
            self.assertEqual(len(series.raw), len(series.total_return))
            self.assertTrue(series.raw and series.split_adjusted and series.total_return)

        ccc = self.by_id["CCC"].series
        self.assertEqual(len(ccc.actions), 1)
        action = ccc.actions[0]
        self.assertEqual(action.kind, "split")
        self.assertEqual(action.factor, 2.0)
        self.assertEqual(action.ex_date, date(2024, 1, 10))

        # Either series reconstructs from the other through the stored factor.
        for i, bar in enumerate(ccc.split_adjusted):
            factor = action.factor if bar.date < action.ex_date else 1.0
            self.assertAlmostEqual(ccc.raw[i].close, bar.close * factor, places=9)
            self.assertAlmostEqual(ccc.raw[i].open, bar.open * factor, places=9)

        # A name with no corporate action carries three identical series.
        aaa = self.by_id["AAA"].series
        self.assertEqual(aaa.actions, ())
        self.assertEqual(aaa.raw, aaa.split_adjusted)
        self.assertEqual(aaa.split_adjusted, aaa.total_return)

    def test_unknown_end_is_censored(self):
        events = fx.cohort_with_unknown_end()
        out = horizon_outcomes(events, self.benchmark, self.calendar, 10)
        self.assertEqual(out.n_matured, 5)
        self.assertEqual(out.n_censored, 1)
        self.assertEqual(out.censored_reasons, (("EEE", "unknown_end"),))
        self.assertNotIn("EEE", out.event_ids)
        self.assertAlmostEqual(out.mean_car * PCT, 4.921745, places=6)

        halted = next(e for e in events if e.event_id == "EEE")
        self.assertFalse(maturity(halted, self.calendar, 10).matured)
        # A resolved delisting is the other case, and it *is* matured.
        self.assertTrue(maturity(self.by_id["EEE"], self.calendar, 10).matured)

    def test_split_invariance(self):
        """A mid-hold 2-for-1 split yields an identical `TradeResult`."""
        split = self.by_id["CCC"]
        twin = fx.split_free_twin()
        self.assertTrue(split.series.actions)
        self.assertFalse(twin.series.actions)

        costs = CostModel.default()
        a = policy_outcome(split, self.calendar, fx.POLICY, costs)
        b = policy_outcome(twin, self.calendar, fx.POLICY, costs)
        for field in ("gross_pct", "net_pct", "rule_fired", "exit_date",
                      "entry_date", "net_by_slippage_bps"):
            self.assertEqual(getattr(a, field), getattr(b, field), field)

        # And the reason the rule exists: on the raw series the ex-date is a
        # -50% overnight gap that would have fired the stop.
        raw = split.series.raw
        ex = next(i for i, bar in enumerate(raw) if bar.date == fx.SPLIT_EX_DATE)
        self.assertLess(raw[ex].open, raw[ex - 1].close * 0.55)
        self.assertLess(raw[ex].low, fx.ENTRY_OPEN["CCC"] * fx.POLICY.stop_frac * 2)

    # -- §8 of the doc ------------------------------------------------------ #

    def test_policy_matches_simulator(self):
        """The policy leg is `simulate_trade`, bit for bit, with nothing added."""
        costs = CostModel.default()
        for event in self.events:
            bars = policy_bars(event, self.calendar)
            zero_day = self.calendar.sessions[
                self.calendar.session_zero(event.known_at_utc)]
            pos = next(i for i, b in enumerate(bars) if b.date == zero_day)
            reference = bars[pos].open
            hs = costs.half_spread_bps(event.liquidity_decile)

            expected_gross = simulate_trade(
                bars, pos - 1, "long",
                reference * fx.POLICY.stop_frac,
                reference * fx.POLICY.target1_frac,
                reference * fx.POLICY.target2_frac,
                fx.POLICY.max_holding_days, slippage_bps=0.0)
            expected_net = simulate_trade(
                bars, pos - 1, "long",
                reference * fx.POLICY.stop_frac,
                reference * fx.POLICY.target1_frac,
                reference * fx.POLICY.target2_frac,
                fx.POLICY.max_holding_days,
                slippage_bps=costs.baseline_slippage_bps + hs)

            got = policy_outcome(event, self.calendar, fx.POLICY, costs)
            self.assertEqual(got.gross_pct, expected_gross.pnl_pct, event.event_id)
            self.assertEqual(got.net_pct, expected_net.pnl_pct, event.event_id)
            self.assertEqual(got.rule_fired, expected_net.rule_fired, event.event_id)
            self.assertEqual(got.exit_date, expected_net.exit_date, event.event_id)
            self.assertEqual(got.entry_date, expected_net.entry_date, event.event_id)

        expected = {
            "AAA": (5.5, 5.16, "t1_then_time", date(2024, 1, 18)),
            "BBB": (-5.0, -5.45, "time", date(2024, 1, 18)),
            "CCC": (6.0, 5.70, "t1_then_t2", date(2024, 1, 16)),
            "DDD": (6.0, 5.62, "t1_then_t2", date(2024, 1, 30)),
            "EEE": (-6.0, -6.64, "stop", date(2024, 1, 24)),
            "FFF": (6.0, 5.56, "t1_then_t2", date(2024, 1, 30)),
        }
        agg = policy_aggregate(self.events, self.calendar, fx.POLICY, costs)
        for outcome in agg.outcomes:
            gross, net, rule, exit_date = expected[outcome.event_id]
            self.assertEqual(outcome.gross_pct, gross, outcome.event_id)
            self.assertEqual(outcome.net_pct, net, outcome.event_id)
            self.assertEqual(outcome.rule_fired, rule, outcome.event_id)
            self.assertEqual(outcome.exit_date, exit_date, outcome.event_id)

    def test_policy_return_is_net(self):
        costs = CostModel.default()
        agg = policy_aggregate(self.events, self.calendar, fx.POLICY, costs)
        self.assertAlmostEqual(agg.mean_gross_pct, 2.083333, places=6)
        self.assertAlmostEqual(agg.mean_net_pct, 1.658333, places=6)
        self.assertEqual(
            [round(v, 6) for _, v in agg.mean_net_by_slippage_bps],
            [1.658333, 1.353333, 0.846667],
        )
        self.assertEqual([bps for bps, _ in agg.mean_net_by_slippage_bps],
                         [10.0, 25.0, 50.0])

        # Net is the headline and it is strictly worse than gross; gross is a
        # separately labelled field, not a substitute.
        self.assertLess(agg.mean_net_pct, agg.mean_gross_pct)
        self.assertNotEqual(agg.mean_net_pct, agg.mean_gross_pct)

        # Both costs are in the net number: dropping either changes it.
        aaa = next(o for o in agg.outcomes if o.event_id == "AAA")
        self.assertEqual(aaa.half_spread_bps, 6.0)
        bars = policy_bars(self.by_id["AAA"], self.calendar)
        pos = next(i for i, b in enumerate(bars) if b.date == date(2024, 1, 4))
        ref = bars[pos].open

        def run(bps):
            return simulate_trade(bars, pos - 1, "long", ref * 0.94, ref * 1.04,
                                  ref * 1.08, 14, slippage_bps=bps).pnl_pct

        self.assertEqual(aaa.net_pct, run(10.0 + 6.0))
        self.assertNotEqual(aaa.net_pct, run(10.0))     # half-spread is included
        self.assertNotEqual(aaa.net_pct, run(6.0))      # slippage is included
        self.assertEqual(aaa.gross_pct, run(0.0))


class BenchmarkSubtractionTests(unittest.TestCase):
    def test_benchmark_subtracted(self):
        """Every name returns exactly the index, so every CAR is zero."""
        calendar, benchmark, events = fx.synthetic_cohort(
            n_dates=6, per_date=2, sessions=120, seed=5,
            effect_daily=0.0, idio_sd=0.0, beta=1.0,
            market_drift=0.006, market_sd=0.002,
        )
        for h in (1, 5, 10):
            out = horizon_outcomes(events, benchmark, calendar, h)
            # The market moved materially over the window ...
            self.assertGreater(abs(out.mean_raw), 0.003)
            self.assertAlmostEqual(out.mean_raw, out.mean_benchmark, places=9)
            # ... and none of it survives the adjustment.
            for value in out.car:
                self.assertAlmostEqual(value, 0.0, places=10)
            self.assertAlmostEqual(out.mean_car, 0.0, places=10)


if __name__ == "__main__":
    unittest.main()
