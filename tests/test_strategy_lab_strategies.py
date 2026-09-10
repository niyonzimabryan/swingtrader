"""Spec Q §7: the V1 strategy roster, rule by rule.

Every threshold in the normative reference configuration gets a test on each
side of it, every abstention path gets a test, and the golden vectors are hand
computed — the arithmetic is written into the test beside the assertion, so a
number that changes has to be argued for rather than re-recorded.

The fixtures are shaped so the arithmetic stays checkable on paper. A flat $100
series with a fixed $1 half-range has a true range of exactly
``max(2, 1, 1) = 2`` on every bar, so Wilder ATR(14) is exactly 2.0; at a
million shares a session its median dollar volume is exactly $100m. The
momentum universe is built so each constituent's formation return is exactly
the number the test names.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from strategy_lab import snapshots, universe, validation
from strategy_lab.domain import DecisionAction, ExecutionMode, SnapshotScope
from strategy_lab.execution_policy import (
    EVENT_SWING_14CAL_V1,
    MOMENTUM_QUARTERLY_89CAL_V1,
    REVERSAL_5CAL_V1,
)
from strategy_lab.indicators import wilder_atr
from strategy_lab.strategies import ROSTER, build_versions, get_strategy
from strategy_lab.strategies import earnings_drift_v1 as earnings
from strategy_lab.strategies import momentum_v1 as momentum
from strategy_lab.strategies import short_term_reversal_v1 as reversal
from strategy_lab.strategies import swingtrader_composite_v1 as composite
from tests.strategylabfixture import (
    Q1_2026_CLOSE,
    Q1_2026_SESSION,
    bars_from_closes,
    composite_result,
    earnings_record,
    flat_bars,
    replayable_inputs,
    ticker_snapshot,
    universe_snapshot,
)


def only(decisions):
    assert len(decisions) == 1, f"expected one decision, got {len(decisions)}"
    return decisions[0]


def by_ticker(decisions):
    return {d.ticker: d for d in decisions}


# --------------------------------------------------------------------------- #
# Shared contract
# --------------------------------------------------------------------------- #


class RosterContractTests(unittest.TestCase):
    def test_the_roster_is_the_four_v1_strategies(self):
        self.assertEqual(sorted(ROSTER), [
            "earnings_drift_v1",
            "momentum_v1",
            "short_term_reversal_v1",
            "swingtrader_composite_v1",
        ])

    def test_every_version_is_long_only_and_names_a_registered_policy(self):
        from strategy_lab.execution_policy import POLICIES

        for slug, strategy in ROSTER.items():
            with self.subTest(slug):
                self.assertEqual(strategy.metadata.direction.value, "long")
                self.assertIn(strategy.metadata.execution_policy_version, POLICIES)
                self.assertEqual(strategy.metadata.slug, slug)

    def test_an_unknown_slug_names_the_roster(self):
        with self.assertRaises(KeyError) as caught:
            get_strategy("regime_router_v1")
        self.assertIn("momentum_v1", str(caught.exception))

    def test_every_decision_carries_reason_codes_and_a_versioned_plan(self):
        """Spec Q acceptance: reason codes, not only prose; a versioned plan."""
        cases = [
            (composite.STRATEGY, ticker_snapshot(replayable_inputs(
                "AAPL", flat_bars(), composite=composite_result()))),
            (earnings.STRATEGY, ticker_snapshot(replayable_inputs(
                "AAPL", flat_bars(),
                earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0)))),
            (momentum.STRATEGY, _momentum_universe()),
            (reversal.STRATEGY, _reversal_universe()),
        ]
        for strategy, snapshot in cases:
            with self.subTest(strategy.metadata.slug):
                decisions = strategy.evaluate(snapshot)
                self.assertTrue(decisions)
                longs = [d for d in decisions if d.action is DecisionAction.LONG]
                self.assertTrue(longs, "each fixture is built to produce a long")
                for decision in decisions:
                    self.assertTrue(decision.reason_codes)
                for decision in longs:
                    self.assertIsNotNone(decision.risk_plan)
                    self.assertEqual(
                        decision.risk_plan.execution_policy_version,
                        strategy.metadata.execution_policy_version,
                    )

    def test_the_same_version_over_the_same_snapshot_hashes_identically(self):
        snapshot = _momentum_universe()
        first = momentum.STRATEGY.evaluate(snapshot)
        second = momentum.MomentumV1().evaluate(snapshot)
        self.assertEqual(
            [d.decision_hash for d in first], [d.decision_hash for d in second]
        )
        self.assertEqual(
            validation.decision_set_hash(snapshot, momentum.STRATEGY.metadata, first),
            validation.decision_set_hash(snapshot, momentum.STRATEGY.metadata, second),
        )

    def test_a_ticker_strategy_refuses_a_universe_snapshot_and_the_reverse(self):
        with self.assertRaises(ValueError):
            earnings.STRATEGY.evaluate(_momentum_universe())
        with self.assertRaises(ValueError):
            momentum.STRATEGY.evaluate(
                ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
            )


# --------------------------------------------------------------------------- #
# earnings_drift_v1
# --------------------------------------------------------------------------- #


def _earnings_snapshot(**record_kwargs):
    defaults = dict(reported_eps=1.25, consensus_eps=1.0)
    defaults.update(record_kwargs)
    record = earnings_record("AAPL", **defaults)
    return ticker_snapshot(replayable_inputs("AAPL", flat_bars(), earnings=record))


class EarningsDriftTests(unittest.TestCase):
    def test_the_golden_positive_surprise(self):
        # surprise = (1.25 - 1.00) / max(|1.00|, 0.01) x 100 = 25.0%
        # signal strength = min(25.0 / 50.0, 1.0) = 0.5
        # ATR(14) over a flat $100 series with a $1 half-range = 2.0, so the
        # stop resolves at 100 - 2 x 2 = 96 against a $100 entry reference.
        self.assertEqual(earnings.surprise_pct(1.25, 1.00), 25.0)
        decision = only(earnings.STRATEGY.evaluate(_earnings_snapshot()))
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertEqual(decision.signal_strength, 0.5)
        self.assertIn(earnings.REASON_SURPRISE_MET, decision.reason_codes)
        self.assertEqual(decision.risk_plan.max_hold_calendar_days, 14)
        self.assertEqual(decision.risk_plan.position_risk_pct, 1.0)
        self.assertIsNone(
            decision.risk_plan.stop_price,
            "the 2-ATR stop anchors to the fill, which does not exist yet",
        )

    def test_the_atr_the_stop_will_use_is_exactly_two(self):
        bars = flat_bars()
        self.assertEqual(
            wilder_atr(
                [b.split_adjusted_high for b in bars],
                [b.split_adjusted_low for b in bars],
                [b.split_adjusted_close for b in bars],
                14,
            ),
            2.0,
        )
        self.assertEqual(EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0).stop_price, 96.0)

    def test_the_five_percent_threshold_from_both_sides(self):
        # (1.05 - 1.00) / 1.00 x 100 = 5.0 (binary floating point lands a
        # hair above, which is the qualifying side of ">= 5.0").
        self.assertGreaterEqual(earnings.surprise_pct(1.05, 1.00), 5.0)
        at_threshold = only(earnings.STRATEGY.evaluate(
            _earnings_snapshot(reported_eps=1.05, consensus_eps=1.00)
        ))
        self.assertEqual(at_threshold.action, DecisionAction.LONG)

        # (1.04 - 1.00) / 1.00 x 100 = 4.0
        below = only(earnings.STRATEGY.evaluate(
            _earnings_snapshot(reported_eps=1.04, consensus_eps=1.00)
        ))
        self.assertEqual(below.action, DecisionAction.FLAT)
        self.assertIn(earnings.REASON_SURPRISE_BELOW, below.reason_codes)

    def test_the_consensus_floor_stops_a_near_zero_denominator_exploding(self):
        # (0.001 - 0.0) / max(0.0, 0.01) x 100 = 10.0, not infinity.
        self.assertEqual(earnings.surprise_pct(0.001, 0.0), 10.0)
        decision = only(earnings.STRATEGY.evaluate(
            _earnings_snapshot(reported_eps=0.001, consensus_eps=0.0)
        ))
        self.assertEqual(decision.action, DecisionAction.LONG)

    def test_a_smaller_loss_than_expected_is_a_positive_surprise(self):
        # (-0.40 - -0.50) / max(0.50, 0.01) x 100 = 20.0
        self.assertAlmostEqual(earnings.surprise_pct(-0.40, -0.50), 20.0, places=9)
        decision = only(earnings.STRATEGY.evaluate(
            _earnings_snapshot(reported_eps=-0.40, consensus_eps=-0.50)
        ))
        self.assertEqual(decision.action, DecisionAction.LONG)

    def test_a_miss_is_flat_not_short(self):
        decision = only(earnings.STRATEGY.evaluate(
            _earnings_snapshot(reported_eps=0.80, consensus_eps=1.00)
        ))
        self.assertEqual(decision.action, DecisionAction.FLAT)

    def test_no_earnings_record_is_flat_and_says_so(self):
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(earnings.REASON_NO_EARNINGS_RECORD, decision.reason_codes)

    def test_a_record_that_was_already_actionable_yesterday_does_not_refire(self):
        stale_record = _earnings_snapshot(
            known_at_utc=datetime(2026, 3, 27, 12, 0),  # before the prior close
            event_date=date(2026, 3, 27),
        )
        decision = only(earnings.STRATEGY.evaluate(stale_record))
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(earnings.REASON_RECORD_NOT_NEW, decision.reason_codes)

    def test_a_record_landing_after_the_event_date_still_fires_and_says_so(self):
        snapshot = _earnings_snapshot(
            event_date=date(2026, 3, 27),
            known_at_utc=datetime(2026, 3, 31, 20, 30),
        )
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertIn(earnings.REASON_DELAYED_ENTRY, decision.reason_codes)

    def test_a_record_without_trustworthy_provenance_carries_the_warning(self):
        snapshot = _earnings_snapshot(replay_eligible=False)
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertIn(universe.REASON_DATA_QUALITY_WARNING, decision.reason_codes)

    def test_a_missing_price_series_abstains_rather_than_guessing(self):
        record = earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0)
        snapshot = ticker_snapshot(replayable_inputs("AAPL", (), earnings=record))
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.ABSTAIN)
        self.assertEqual(decision.blocked_reasons, ("missing_dependency",))

    def test_a_stale_price_series_abstains_with_a_different_reason(self):
        record = earnings_record(
            "AAPL", reported_eps=1.25, consensus_eps=1.0,
            known_at_utc=Q1_2026_CLOSE + timedelta(days=4),
        )
        snapshot = ticker_snapshot(
            replayable_inputs("AAPL", flat_bars(), earnings=record),
            cutoff=Q1_2026_CLOSE + timedelta(days=4),
        )
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.ABSTAIN)
        self.assertEqual(decision.blocked_reasons, ("stale_data",))

    def test_an_illiquid_name_is_flat_with_the_failing_screen_named(self):
        # $4.00 x 1m shares = $4m median dollar volume, and $4.00 is under the
        # $5.00 minimum. Both screens fail and both are reported.
        snapshot = ticker_snapshot(replayable_inputs(
            "AAPL", flat_bars(close=4.0),
            earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0),
        ))
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(universe.REASON_PRICE_BELOW_MINIMUM, decision.reason_codes)
        self.assertIn(universe.REASON_ILLIQUID, decision.reason_codes)

    def test_a_short_history_is_flat_under_the_252_session_rule(self):
        snapshot = ticker_snapshot(replayable_inputs(
            "AAPL", flat_bars(251),
            earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0),
        ))
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(universe.REASON_INSUFFICIENT_HISTORY, decision.reason_codes)

    def test_exactly_252_sessions_passes_the_history_rule(self):
        snapshot = ticker_snapshot(replayable_inputs(
            "AAPL", flat_bars(252),
            earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0),
        ))
        decision = only(earnings.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.LONG)

    def test_post_announcement_price_action_cannot_qualify_an_entry(self):
        """Spec Q §7: no post-announcement price or volume in the qualifier.

        Two series with wildly different post-event moves and the same
        record must reach the same action and the same surprise-driven
        strength; only the ATR-dependent abstention could differ, and neither
        series triggers it.
        """
        record = earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0)
        calm = ticker_snapshot(replayable_inputs("AAPL", flat_bars(), earnings=record))
        spiked = ticker_snapshot(replayable_inputs(
            "AAPL", bars_from_closes([100.0] * 259 + [160.0]), earnings=record
        ))
        calm_decision = only(earnings.STRATEGY.evaluate(calm))
        spiked_decision = only(earnings.STRATEGY.evaluate(spiked))
        self.assertEqual(calm_decision.action, spiked_decision.action)
        self.assertEqual(calm_decision.signal_strength, spiked_decision.signal_strength)


# --------------------------------------------------------------------------- #
# momentum_v1
# --------------------------------------------------------------------------- #


def _momentum_closes(formation_return: float) -> list[float]:
    """253 closes whose T-252 -> T-21 return is exactly ``formation_return``.

    The formation window is ``bars[0:232]``, so anchoring at 100.0 on the first
    bar and holding ``100 x (1 + r)`` from the second onwards makes
    ``close[231] / close[0] - 1`` exactly ``r``.
    """
    return [100.0] + [100.0 * (1.0 + formation_return)] * 252


def _momentum_universe(
    count: int = 60,
    overrides: dict | None = None,
    last_session: date = Q1_2026_SESSION,
    **kw,
):
    """``count`` constituents whose formation returns are 0.00, 0.01, ... ."""
    overrides = overrides or {}
    inputs = []
    for index in range(count):
        ticker = f"T{index:03d}"
        value = overrides.get(ticker, index / 100.0)
        inputs.append(replayable_inputs(
            ticker,
            bars_from_closes(_momentum_closes(value), last_session=last_session),
        ))
    return universe_snapshot(inputs, **kw)


class MomentumTests(unittest.TestCase):
    def test_the_formation_window_is_t_minus_252_to_t_minus_21(self):
        bars = bars_from_closes(_momentum_closes(0.37))
        self.assertEqual(len(bars), 253)
        self.assertAlmostEqual(momentum.formation_return(bars), 0.37, places=12)

    def test_a_252_bar_series_cannot_be_scored(self):
        """252 bars pass the universe screen and still lack a T-252 anchor."""
        self.assertIsNone(momentum.formation_return(flat_bars(252)))

    def test_one_decision_per_constituent_ordered_by_ticker(self):
        snapshot = _momentum_universe()
        decisions = momentum.STRATEGY.evaluate(snapshot)
        self.assertEqual(len(decisions), len(snapshot.constituents))
        self.assertEqual([d.ticker for d in decisions], list(snapshot.constituents))
        self.assertEqual(
            len({d.ticker for d in decisions}), len(decisions), "no repeats"
        )
        for decision in decisions:
            self.assertEqual(decision.snapshot_hash, snapshot.content_hash)

    def test_the_top_decile_of_sixty_is_six_equally_weighted_names(self):
        # ceil(60 / 10) = 6, capped at 20; equal weight => 1/6 each.
        decisions = by_ticker(momentum.STRATEGY.evaluate(_momentum_universe()))
        longs = sorted(t for t, d in decisions.items() if d.action is DecisionAction.LONG)
        self.assertEqual(longs, ["T054", "T055", "T056", "T057", "T058", "T059"])
        for ticker in longs:
            self.assertAlmostEqual(
                decisions[ticker].risk_plan.position_risk_pct, 1 / 6, places=12
            )
            self.assertEqual(decisions[ticker].risk_plan.max_hold_calendar_days, 89)
        self.assertEqual(decisions["T053"].action, DecisionAction.FLAT)
        self.assertIn(momentum.REASON_NOT_SELECTED, decisions["T053"].reason_codes)

    def test_a_tie_at_the_selection_boundary_breaks_on_ticker_ascending(self):
        # T053 and T054 both score 0.54; the sixth slot goes to T053.
        snapshot = _momentum_universe(overrides={"T053": 0.54})
        decisions = by_ticker(momentum.STRATEGY.evaluate(snapshot))
        self.assertEqual(decisions["T053"].action, DecisionAction.LONG)
        self.assertEqual(decisions["T054"].action, DecisionAction.FLAT)

    def test_the_cap_of_twenty_binds_before_the_decile_does(self):
        # ceil(250 / 10) = 25, capped at 20.
        self.assertEqual(momentum.select_count(250), 20)
        self.assertEqual(momentum.select_count(200), 20)
        self.assertEqual(momentum.select_count(199), 20)
        self.assertEqual(momentum.select_count(50), 5)

    def test_fewer_than_fifty_eligible_constituents_abstains_the_whole_arm(self):
        decisions = momentum.STRATEGY.evaluate(_momentum_universe(count=49))
        self.assertEqual(len(decisions), 49)
        self.assertTrue(all(d.action is DecisionAction.ABSTAIN for d in decisions))
        self.assertIn(momentum.REASON_TOO_FEW_ELIGIBLE, decisions[0].reason_codes)

    def test_off_a_rebalance_session_every_constituent_is_flat(self):
        snapshot = _momentum_universe(
            last_session=date(2026, 3, 30), cutoff=datetime(2026, 3, 30, 21, 0)
        )
        self.assertFalse(snapshots.calendar_of(snapshot)["is_quarter_end_session"])
        decisions = momentum.STRATEGY.evaluate(snapshot)
        self.assertTrue(all(d.action is DecisionAction.FLAT for d in decisions))
        self.assertIn(momentum.REASON_NOT_REBALANCE_SESSION, decisions[0].reason_codes)

    def test_a_constituent_with_no_scoreable_window_abstains_alone(self):
        inputs = [
            replayable_inputs(f"T{i:03d}", bars_from_closes(_momentum_closes(i / 100.0)))
            for i in range(60)
        ]
        inputs.append(replayable_inputs("ZSHORT", flat_bars(252)))
        snapshot = universe_snapshot(inputs)
        decisions = by_ticker(momentum.STRATEGY.evaluate(snapshot))
        self.assertEqual(decisions["ZSHORT"].action, DecisionAction.ABSTAIN)
        self.assertIn(
            momentum.REASON_FORMATION_WINDOW_SHORT, decisions["ZSHORT"].reason_codes
        )
        # ceil(61 / 10) = 7 selected; the short name is eligible but unscoreable.
        longs = [t for t, d in decisions.items() if d.action is DecisionAction.LONG]
        self.assertEqual(len(longs), 7)

    def test_an_illiquid_constituent_is_flat_and_leaves_the_rank_alone(self):
        inputs = [
            replayable_inputs(f"T{i:03d}", bars_from_closes(_momentum_closes(i / 100.0)))
            for i in range(60)
        ]
        inputs.append(replayable_inputs(
            "ZPENNY", bars_from_closes([1.0] * 253, volume=1000.0)
        ))
        decisions = by_ticker(momentum.STRATEGY.evaluate(universe_snapshot(inputs)))
        self.assertEqual(decisions["ZPENNY"].action, DecisionAction.FLAT)
        self.assertIn(universe.REASON_ILLIQUID, decisions["ZPENNY"].reason_codes)
        longs = [t for t, d in decisions.items() if d.action is DecisionAction.LONG]
        self.assertEqual(len(longs), 6, "an ineligible name is not in the population")

    def test_a_reconstructed_universe_snapshot_warns_on_every_decision(self):
        inputs = [
            replayable_inputs(
                f"T{i:03d}", bars_from_closes(_momentum_closes(i / 100.0)),
                price_provenance_class=snapshots.PROVENANCE_ARCHIVAL,
                price_replay_eligible=False,
                delisting_known=False,
            )
            for i in range(60)
        ]
        snapshot = universe_snapshot(inputs)
        self.assertFalse(snapshot.replay_eligible)
        decisions = momentum.STRATEGY.evaluate(snapshot)
        for decision in decisions:
            self.assertIn(universe.REASON_DATA_QUALITY_WARNING, decision.reason_codes)
            self.assertIn(momentum.REASON_DELISTING_UNKNOWN, decision.reason_codes)

    def test_the_selected_basket_uses_the_quarterly_policy(self):
        decisions = momentum.STRATEGY.evaluate(_momentum_universe())
        for decision in decisions:
            if decision.risk_plan is not None:
                self.assertEqual(
                    decision.risk_plan.execution_policy_version,
                    MOMENTUM_QUARTERLY_89CAL_V1.version,
                )


# --------------------------------------------------------------------------- #
# short_term_reversal_v1
# --------------------------------------------------------------------------- #


def _uptrend_then_drop(final_closes) -> list[float]:
    """249 sessions ramping 60 -> 120, then the closes given."""
    return [60.0 + i * (60.0 / 248) for i in range(249)] + list(final_closes)


def _reversal_universe(qualifying: int = 3, extra=()):
    inputs = []
    for index in range(qualifying):
        # 110 / 120 - 1 = -8.333%, RSI(2) ~ 1.1, close 110 > SMA(200) ~ 96.5
        inputs.append(replayable_inputs(
            f"R{index:03d}", bars_from_closes(_uptrend_then_drop([116.0, 113.0, 110.0]))
        ))
    inputs.extend(extra)
    return universe_snapshot(inputs)


class ShortTermReversalTests(unittest.TestCase):
    def test_the_golden_qualifying_signal(self):
        # three-session return = 110 / 120 - 1 = -8.333%  (<= -8%)
        # RSI(2), Wilder                     ~ 1.09        (<= 10)
        # close 110 > SMA(200) ~ 96.54
        bars = bars_from_closes(_uptrend_then_drop([116.0, 113.0, 110.0]))
        self.assertAlmostEqual(reversal.three_session_return(bars), -1 / 12, places=12)
        decisions = by_ticker(reversal.STRATEGY.evaluate(_reversal_universe(1)))
        decision = decisions["R000"]
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertIn(reversal.REASON_SELECTED, decision.reason_codes)
        self.assertEqual(decision.risk_plan.execution_policy_version, REVERSAL_5CAL_V1.version)
        self.assertEqual(decision.risk_plan.max_hold_calendar_days, 5)
        self.assertEqual(decision.risk_plan.position_risk_pct, 1.0)
        # min(0.08333 / 0.20, 1.0)
        self.assertAlmostEqual(decision.signal_strength, (1 / 12) / 0.20, places=12)

    def test_the_eight_percent_threshold_from_both_sides(self):
        """The rule is "at or below -8%", bracketed a basis point either side.

        Exactly -8% is not reachable: neither -0.08 nor 0.92 is representable
        in binary floating point, and no pair of prices makes
        `last / first - 1.0` compare equal to the stored threshold. The
        honest test is therefore a tight bracket around it, with the
        arithmetic shown — 91.99 / 100 - 1 = -8.01%, 92.01 / 100 - 1 = -7.99%,
        and the $100 anchor is set rather than computed so the ramp's rounding
        cannot move it.
        """
        def series(last):
            return (
                [50.0 + i * (50.0 / 248) for i in range(248)]
                + [100.0, 97.0, 95.0, last]
            )

        self.assertLess(
            reversal.three_session_return(bars_from_closes(series(91.99))), -0.08
        )
        self.assertGreater(
            reversal.three_session_return(bars_from_closes(series(92.01))), -0.08
        )
        at = replayable_inputs("AAAA", bars_from_closes(series(91.99)))
        above = replayable_inputs("BBBB", bars_from_closes(series(92.01)))
        decisions = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot([at, above])))
        self.assertEqual(decisions["AAAA"].action, DecisionAction.LONG)
        self.assertEqual(decisions["BBBB"].action, DecisionAction.FLAT)
        self.assertIn(
            reversal.REASON_DECLINE_TOO_SHALLOW, decisions["BBBB"].reason_codes
        )

    def test_an_up_close_keeps_rsi_above_the_threshold(self):
        # 110 / 120 - 1 = -8.33%, so the decline qualifies — but the last
        # session is an up day, so RSI(2) is nowhere near 10 and the name is
        # not a reversal signal.
        inputs = replayable_inputs("CCCC", bars_from_closes(
            _uptrend_then_drop([104.0, 103.0, 110.0])))
        decision = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot([inputs])))["CCCC"]
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(reversal.REASON_RSI_TOO_HIGH, decision.reason_codes)

    def test_a_name_below_its_200_session_mean_is_filtered_out(self):
        # A downtrend: 140 -> 100 over 249 sessions, then a sharp drop to 88.
        closes = [140.0 - i * (40.0 / 248) for i in range(249)] + [95.0, 92.0, 88.0]
        inputs = replayable_inputs("DDDD", bars_from_closes(closes))
        decision = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot([inputs])))["DDDD"]
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(reversal.REASON_BELOW_TREND_FILTER, decision.reason_codes)

    def test_the_cap_keeps_the_ten_largest_declines_ties_by_ticker(self):
        inputs = []
        # Twelve names, all qualifying; the two shallowest declines are capped.
        for index in range(12):
            # 110.00 down to 109.45: every one is a decline of at least 8.33%,
            # and a higher index is a deeper one.
            last = 110.0 - index * 0.05
            inputs.append(replayable_inputs(
                f"R{index:03d}", bars_from_closes(_uptrend_then_drop([116.0, 113.0, last]))
            ))
        decisions = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot(inputs)))
        longs = sorted(t for t, d in decisions.items() if d.action is DecisionAction.LONG)
        self.assertEqual(len(longs), 10)
        self.assertEqual(longs, [f"R{i:03d}" for i in range(2, 12)])
        for ticker in ("R000", "R001"):
            self.assertEqual(decisions[ticker].action, DecisionAction.FLAT)
            self.assertIn(reversal.REASON_CAPPED, decisions[ticker].reason_codes)
        for ticker in longs:
            self.assertAlmostEqual(
                decisions[ticker].risk_plan.position_risk_pct, 0.1, places=12
            )

    def test_a_tie_on_the_decline_breaks_on_ticker_ascending(self):
        inputs = [
            replayable_inputs(
                f"R{index:03d}", bars_from_closes(_uptrend_then_drop([116.0, 113.0, 110.0]))
            )
            for index in range(11)
        ]
        decisions = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot(inputs)))
        longs = sorted(t for t, d in decisions.items() if d.action is DecisionAction.LONG)
        self.assertEqual(longs, [f"R{i:03d}" for i in range(10)])
        self.assertEqual(decisions["R010"].action, DecisionAction.FLAT)

    def test_a_series_too_short_for_the_indicators_never_reaches_them(self):
        """The 252-session screen is stricter than every indicator window.

        A 180-session series fails the shared liquidity rules first and is
        `flat` with the failing screen named, so `REASON_HISTORY_SHORT` is a
        defensive guard rather than a reachable path today. It stays because a
        future universe with a shorter history rule must fail closed rather
        than hand `None` to a comparison.
        """
        ok = replayable_inputs("EEEE", flat_bars(252, close=100.0))
        short = replayable_inputs("FFFF", flat_bars(180, close=100.0))
        decisions = by_ticker(reversal.STRATEGY.evaluate(universe_snapshot([ok, short])))
        self.assertEqual(decisions["FFFF"].action, DecisionAction.FLAT)
        self.assertIn(universe.REASON_INSUFFICIENT_HISTORY, decisions["FFFF"].reason_codes)
        self.assertEqual(decisions["EEEE"].action, DecisionAction.FLAT)

    def test_it_is_structurally_shadow_only(self):
        version = reversal.STRATEGY.metadata
        self.assertTrue(validation.is_shadow_only(version))
        self.assertEqual(
            validation.require_mode_allowed(version, ExecutionMode.SHADOW),
            ExecutionMode.SHADOW,
        )
        for mode in (ExecutionMode.PAPER, ExecutionMode.LIVE):
            with self.subTest(mode), self.assertRaises(validation.ModeRefused):
                validation.require_mode_allowed(version, mode)

    def test_the_other_arms_are_not_shadow_only(self):
        for slug in ("earnings_drift_v1", "momentum_v1", "swingtrader_composite_v1"):
            with self.subTest(slug):
                self.assertFalse(validation.is_shadow_only(ROSTER[slug].metadata))


# --------------------------------------------------------------------------- #
# swingtrader_composite_v1
# --------------------------------------------------------------------------- #


def _composite_snapshot(**kwargs):
    return ticker_snapshot(
        replayable_inputs("AAPL", flat_bars(), composite=composite_result(**kwargs))
    )


class CompositeAdapterTests(unittest.TestCase):
    def test_the_golden_memo_maps_straight_through(self):
        decision = only(composite.STRATEGY.evaluate(_composite_snapshot()))
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertEqual(decision.signal_strength, 0.82, "the pipeline's own score")
        self.assertEqual(decision.risk_plan.stop_price, 94.00)
        self.assertEqual(decision.risk_plan.target_prices, (112.20, 118.30))
        self.assertEqual(decision.risk_plan.max_hold_calendar_days, 20)
        self.assertIn(composite.REASON_MEMO_COHORT, decision.reason_codes)
        self.assertIn("pipeline_classification_high_conviction", decision.reason_codes)

    def test_a_non_memo_cohort_is_flat(self):
        for cohort in ("exploration", "below", ""):
            with self.subTest(cohort):
                decision = only(composite.STRATEGY.evaluate(
                    _composite_snapshot(cohort=cohort)
                ))
                self.assertEqual(decision.action, DecisionAction.FLAT)
                self.assertIn(
                    composite.REASON_NOT_ACTIONABLE_COHORT, decision.reason_codes
                )

    def test_a_short_direction_is_flat_because_v1_is_long_only(self):
        decision = only(composite.STRATEGY.evaluate(_composite_snapshot(direction="short")))
        self.assertEqual(decision.action, DecisionAction.FLAT)
        self.assertIn(composite.REASON_DIRECTION_NOT_LONG, decision.reason_codes)

    def test_a_memo_cohort_with_no_memo_still_fires_and_records_the_gap(self):
        decision = only(composite.STRATEGY.evaluate(_composite_snapshot(memo_generated=False)))
        self.assertEqual(decision.action, DecisionAction.LONG)
        self.assertIn(composite.REASON_MEMO_NOT_GENERATED, decision.reason_codes)

    def test_no_frozen_result_abstains(self):
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
        decision = only(composite.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.ABSTAIN)
        self.assertEqual(decision.blocked_reasons, ("missing_dependency",))
        self.assertIn(composite.REASON_NO_COMPOSITE_RESULT, decision.reason_codes)

    def test_a_result_from_last_week_is_stale_not_actionable(self):
        snapshot = ticker_snapshot(
            replayable_inputs(
                "AAPL", flat_bars(),
                composite=composite_result(scored_at=datetime(2026, 3, 24, 20, 0)),
            )
        )
        decision = only(composite.STRATEGY.evaluate(snapshot))
        self.assertEqual(decision.action, DecisionAction.ABSTAIN)
        self.assertEqual(decision.blocked_reasons, ("stale_data",))

    def test_broken_trade_parameters_abstain_rather_than_defaulting(self):
        cases = {
            "no stop": {"entry_price": 100.0, "target_1": 110.0, "max_hold_days": 20},
            "stop above entry": {
                "entry_price": 100.0, "stop_loss": 105.0,
                "target_1": 110.0, "max_hold_days": 20,
            },
            "target below entry": {
                "entry_price": 100.0, "stop_loss": 94.0,
                "target_1": 99.0, "max_hold_days": 20,
            },
            "inverted targets": {
                "entry_price": 100.0, "stop_loss": 94.0, "target_1": 110.0,
                "target_2": 105.0, "max_hold_days": 20,
            },
            "no horizon": {"entry_price": 100.0, "stop_loss": 94.0, "target_1": 110.0},
        }
        for label, params in cases.items():
            with self.subTest(label):
                decision = only(composite.STRATEGY.evaluate(
                    _composite_snapshot(trade_params=params)
                ))
                self.assertEqual(decision.action, DecisionAction.ABSTAIN)
                self.assertIn(
                    composite.REASON_TRADE_PARAMS_INCOMPLETE, decision.reason_codes
                )

    def test_a_single_target_memo_produces_a_single_target_plan(self):
        decision = only(composite.STRATEGY.evaluate(_composite_snapshot(trade_params={
            "entry_price": 100.0, "stop_loss": 94.0, "target_1": 110.0,
            "max_hold_days": 15,
        })))
        self.assertEqual(decision.risk_plan.target_prices, (110.0,))
        self.assertEqual(decision.risk_plan.max_hold_calendar_days, 15)

    def test_it_is_never_historically_replayable(self):
        version = composite.STRATEGY.metadata
        self.assertFalse(version.historically_replayable)
        snapshot = _composite_snapshot()
        with self.assertRaises(validation.ReplayRefused) as caught:
            validation.guard_historical_replay(version, snapshot)
        self.assertIn("historically_replayable=False", str(caught.exception))

    def test_the_adapter_names_no_model_client(self):
        """Spec Q §7A: the adapter maps stored values and makes no LLM call."""
        import inspect

        source = inspect.getsource(composite)
        for needle in ("anthropic", "openai", "genai", "ScoringEngine("):
            self.assertNotIn(needle, source)


# --------------------------------------------------------------------------- #
# Replay guard across the roster
# --------------------------------------------------------------------------- #


class ReplayGuardTests(unittest.TestCase):
    def test_a_replayable_arm_over_a_clean_snapshot_is_allowed(self):
        validation.guard_historical_replay(
            earnings.STRATEGY.metadata, _earnings_snapshot()
        )

    def test_a_replayable_arm_over_a_reconstructed_snapshot_is_refused(self):
        snapshot = ticker_snapshot(replayable_inputs(
            "AAPL", flat_bars(),
            price_provenance_class=snapshots.PROVENANCE_ARCHIVAL,
            price_replay_eligible=False,
            earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0),
        ))
        with self.assertRaises(validation.ReplayRefused) as caught:
            validation.guard_historical_replay(earnings.STRATEGY.metadata, snapshot)
        self.assertIn("archival_reconstructed", str(caught.exception))

    def test_every_version_states_why_it_is_or_is_not_replayable(self):
        for slug, version in build_versions().items():
            with self.subTest(slug):
                self.assertTrue(version.replayability_reason.strip())


if __name__ == "__main__":
    unittest.main()
