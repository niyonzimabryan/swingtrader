"""Spec Q §13: the owner-only Strategy Lab commands, and what they may print.

Three claims are worth a test, and they are the three that would be expensive to
get wrong:

1. **A non-owner reaches none of them.** The `@authorized` decorator is the
   chat-id allowlist, and an unauthorised chat gets silence rather than a
   refusal — a refusal tells a stranger the bot exists.
2. **No number is produced here.** Every performance figure printed by
   `/strategy` and by the weekly report is copied out of the payload
   `scripts/strategy_lab_scoreboard.py` produced. The renderer is a pure
   function of that dict, so a test can hand it a known payload and assert the
   exact strings come back — and assert that a value the payload does *not*
   contain never appears.
3. **The excluded commands stay excluded.** Promotion and the live-tier controls
   belong to PR 5 and PR 6; `/live_kill` is Phase 6's and untouched. A test that
   the bot registers exactly the intended surface is how that stays true.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from bot import auth
from bot.handlers import strategy_lab as handlers

OWNER = 4242
STRANGER = 9999


class FakeMessage:
    def __init__(self):
        self.replies: list[tuple[str, str | None]] = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append((text, parse_mode))
        return SimpleNamespace(message_id=1)


class FakeUpdate:
    def __init__(self, chat_id: int):
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = FakeMessage()


def context(settings=None, args=None):
    return SimpleNamespace(
        bot_data={"pipeline": SimpleNamespace(settings=settings) if settings else None},
        args=list(args or []),
    )


def _settings(**overrides):
    base = dict(
        strategy_lab_enabled=True,
        strategy_lab_shadow_enabled=True,
        strategy_lab_experiment="shadow_roster_v1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #


class AuthorizationTests(unittest.TestCase):
    """Requirement 7: a non-owner is refused. Silently, like every other command."""

    def setUp(self):
        auth.init_auth(str(OWNER))
        self.calls: list[str] = []

        # Every handler is wrapped by @authorized, so the cheapest proof that
        # authorisation is what stops a stranger is to make the *work* the
        # handler would do observable and assert it never happens.
        self._real = handlers._lab

        def _tripwire():
            self.calls.append("lab")
            raise AssertionError("an unauthorised chat reached the Strategy Lab")

        handlers._lab = _tripwire
        self.addCleanup(lambda: setattr(handlers, "_lab", self._real))

    def test_a_stranger_reaches_no_strategy_lab_handler(self):
        for handler in (
            handlers.experiments_command,
            handlers.strategies_command,
            handlers.strategy_command,
            handlers.pause_experiment_command,
            handlers.resume_experiment_command,
        ):
            update = FakeUpdate(STRANGER)
            run(handler(update, context(_settings(), args=["momentum_v1"])))
            self.assertEqual(
                update.message.replies, [],
                f"{handler.__name__} answered an unauthorised chat",
            )
        self.assertEqual(self.calls, [])

    def test_a_chat_with_no_id_is_ignored(self):
        update = FakeUpdate(OWNER)
        update.effective_chat = None
        run(handlers.experiments_command(update, context(_settings())))
        self.assertEqual(update.message.replies, [])


class DisabledTests(unittest.TestCase):
    """With the master switch off the owner gets a reason, not a table read."""

    def setUp(self):
        auth.init_auth(str(OWNER))

    def test_every_command_says_disabled_and_touches_nothing(self):
        settings = _settings(strategy_lab_enabled=False)
        for handler, args in (
            (handlers.experiments_command, []),
            (handlers.strategies_command, []),
            (handlers.strategy_command, ["momentum_v1"]),
            (handlers.pause_experiment_command, []),
            (handlers.resume_experiment_command, []),
        ):
            update = FakeUpdate(OWNER)
            run(handler(update, context(settings, args=args)))
            self.assertEqual(len(update.message.replies), 1)
            self.assertIn("STRATEGY_LAB_ENABLED", update.message.replies[0][0])

    def test_an_uninitialised_pipeline_says_so(self):
        update = FakeUpdate(OWNER)
        run(handlers.experiments_command(update, context(None)))
        self.assertIn("initializing", update.message.replies[0][0])

    def test_strategy_without_a_slug_prints_usage(self):
        update = FakeUpdate(OWNER)
        run(handlers.strategy_command(update, context(_settings(), args=[])))
        self.assertIn("Usage: /strategy", update.message.replies[0][0])


# --------------------------------------------------------------------------- #
# Rendering: every number comes from the payload
# --------------------------------------------------------------------------- #


PAYLOAD = {
    "schema": "strategy_lab.scoreboard.v1",
    "inputs": {
        "experiment": "shadow_roster_v1",
        "cutoff_utc": "2026-06-30T21:00:00",
        "primary_metric": "mean_net_pct",
        "floors": {"matured": 100, "distinct_dates": 20, "closed": 30, "shadow_days": 60},
        "gate": {"confidence_level": 0.9},
        "costs": {"slippage_bps": 10.0, "half_spread_bps": 5.0, "commission_bps": 0.0},
    },
    "variants": {
        "experiment": "shadow_roster_v1",
        "planned_variants": 4,
        "variants_tried": [["momentum_v1@1.0.0", "shadow"]],
        "n_tried": 5,
        "n_trials": 5,
        "undeclared_variants": 1,
    },
    "sections": {
        "clean_replay": {
            "evidence_class": "clean_replay",
            "primary_metric": "mean_net_pct",
            "winner": None,
            "label": "insufficient_evidence",
            "reasons": ["momentum_v1@1.0.0/shadow has 12 matured trades, floor is 100"],
            "n_trials": 5,
            "variants_declared": 4,
            "gate": {"confidence_level": 0.9},
            "floors": {"matured": 100},
            "arms": [{
                "arm": "momentum_v1@1.0.0/shadow",
                "status": "insufficient_evidence",
                "n_matured": 12,
                "n_open": 3,
                "n_distinct_dates": 7,
                "mean_net_pct": 0.4231,
                "mean_r": 0.1875,
                "win_rate": 0.5833,
                "max_drawdown_pct": 3.125,
                "time_under_water_days": 14,
                "benchmark_relative_pct": 0.0912,
                "warnings": ["below_matured_floor"],
                "uncertainty": {
                    "estimate": 0.4231, "lower": -0.1104, "upper": 0.955,
                    "level": 0.9, "method": "stationary_bootstrap",
                    "block_length": 3, "reps": 1000, "seed": 20260910, "n_eff": 8.25,
                },
                "adjusted_lower": -0.2201,
                "adjusted_level": 0.9791,
                "n_trials": 5,
                "stepm_rejected": False,
                "stepm_note": "family too small",
            }],
            "overlaps": [{
                "left": "momentum_v1@1.0.0/shadow",
                "right": "short_term_reversal_v1@1.0.0/shadow",
                "n_left": 12, "n_right": 9, "n_shared": 4,
                "jaccard": 0.2353, "shared_exposure_days": 11,
                "correlation": 0.6127, "correlation_note": "4 pairs",
            }],
        },
    },
    "refusals": [{"arm": "earnings_drift_v1@1.0.0/shadow", "reason": "no earnings record"}],
    "warnings": ["variants_run_beyond_preregistration"],
    "notes": ["Every number here is computed by code."],
}


class ScoreboardRenderingTests(unittest.TestCase):
    """Requirement 5: samples, maturity, costs, drawdown, benchmark, uncertainty,
    correlation and warnings all appear, and all of them come from the payload."""

    def setUp(self):
        self.text = _plain("\n".join(handlers.scoreboard_lines(PAYLOAD)))

    def test_every_required_display_field_is_present(self):
        for needle in (
            "12",        # samples: n matured
            "3",         # maturity: n open
            "7",         # distinct dates
            "0.4231",    # net %
            "3.1250",    # max drawdown
            "0.0912",    # benchmark-relative
            "0.6127",    # correlation
            "8.2500",    # n_eff
            "20260910",  # the seed, so the card reproduces from the CLI
            "below_matured_floor",
            "variants_run_beyond_preregistration",
        ):
            self.assertIn(needle, self.text, f"{needle!r} missing from the card")

    def test_costs_and_floors_are_printed_beside_the_numbers(self):
        self.assertIn("slip 10.0bps", self.text)
        self.assertIn("half-spread 5.0bps", self.text)
        self.assertIn("matured 100", self.text)

    def test_the_multiple_testing_denominator_is_printed(self):
        self.assertIn("declared `4`", self.text)
        self.assertIn("run `5`", self.text)
        self.assertIn("testing denominator `5`", self.text)

    def test_no_winner_is_named_when_the_payload_names_none(self):
        self.assertIn("insufficient_evidence", self.text)
        self.assertNotIn("winner", self.text)

    def test_a_refused_arm_is_shown_rather_than_dropped(self):
        self.assertIn("earnings", self.text)
        self.assertIn("no earnings record", self.text)

    def test_the_recommendation_is_labelled_informational(self):
        self.assertIn("Recommendations are informational", self.text)
        self.assertIn("owner", self.text)

    def test_the_renderer_invents_nothing(self):
        """Only figures the payload carries may appear.

        Every float in the card is checked against the set of floats the payload
        actually contains, formatted by the one formatter. A renderer that
        derived a number — a ratio, a sum, an annualisation — would produce a
        float that is in the message and not in this set, and that is exactly
        the defect AGENTS.md §1.2 is about.
        """
        import re

        allowed = {handlers._fmt(value) for value in _floats(PAYLOAD)}
        allowed |= {str(value) for value in _floats(PAYLOAD)}
        # Bounded on both sides so a semantic version inside an arm label
        # ("momentum_v1@1.0.0/shadow") is not read as the float 1.0.
        for token in re.findall(r"(?<![\d.])-?\d+\.\d+(?![\d.])", self.text):
            self.assertIn(
                token, allowed,
                f"{token!r} is in the message but not in the scoreboard payload",
            )

    def test_an_absent_payload_says_so_rather_than_printing_zeros(self):
        text = _plain("\n".join(handlers.scoreboard_lines(None)))
        self.assertIn("No scorecard yet", text)
        self.assertNotIn("0.0", text)

    def test_the_exploratory_section_carries_its_warning(self):
        payload = dict(PAYLOAD)
        payload["sections"] = {
            "archival_reconstructed": PAYLOAD["sections"]["clean_replay"],
        }
        text = _plain("\n".join(handlers.scoreboard_lines(payload)))
        self.assertIn("EXPLORATORY", text)
        self.assertIn("never a promotion gate", text)


class MarkdownEscapingTests(unittest.TestCase):
    """MarkdownV2 escapes prose one way and a code span another.

    Outside a code span every special character must be escaped; *inside* one
    only a backtick and a backslash may be, and anything else is literal — so a
    prose-escaped value inside backticks reaches the reader as
    `momentum\\_v1@1\\.0\\.0/shadow`. Almost every value this card prints is a slug
    or a semantic version, so getting it the wrong way round would put a
    backslash on nearly every line.
    """

    def _cards(self):
        yield "\n".join(handlers.scoreboard_lines(PAYLOAD))
        yield handlers.render_experiments({
            "configured": "shadow_roster_v1",
            "experiments": [{
                "name": "shadow_roster_v1", "status": "running",
                "planned_variants": 4, "primary_metric": "mean_net_pct",
                "owner": "bryan", "configured": True,
                "arms": [{
                    "arm_id": 1, "slug": "momentum_v1", "version": "1.0.0",
                    "mode": "shadow", "status": "active", "risk_budget": 0.01,
                    "decisions": 12,
                }],
            }],
        })
        yield handlers.render_strategies({"strategies": [{
            "slug": "momentum_v1", "version": "1.0.0", "scope": "universe",
            "status": "shadow", "policy": "momentum_quarterly_89cal_v1",
            "expected_holding_days": 89, "historically_replayable": True,
            "champion": False,
        }]})

    def test_no_card_carries_a_backslash_inside_a_code_span(self):
        import re

        for card in self._cards():
            for span in re.findall(r"`([^`]*)`", card):
                self.assertNotIn(
                    "\\", span,
                    f"code span {span!r} carries a prose escape; Telegram shows "
                    "the backslash to the reader",
                )

    def test_prose_outside_a_code_span_is_still_escaped(self):
        """The other half: dropping `escape_md` from prose would break parsing."""
        card = handlers.render_experiments({
            "configured": "shadow_roster_v1",
            "experiments": [{
                "name": "shadow_roster_v1", "status": "running",
                "planned_variants": 4, "primary_metric": "mean_net_pct",
                "owner": "bryan", "configured": True, "arms": [],
            }],
        })
        self.assertIn("*shadow\\_roster\\_v1*", card)

    def test_a_backtick_in_a_value_is_escaped_rather_than_closing_the_span(self):
        self.assertEqual(handlers._code("a`b"), "a\\`b")
        self.assertEqual(handlers._code("a\\b"), "a\\\\b")


def _plain(text: str) -> str:
    """The message with MarkdownV2's escapes removed, for readable assertions."""
    return text.replace("\\", "")


def _floats(node) -> set[float]:
    """Every float anywhere in the payload."""
    if isinstance(node, bool):
        return set()
    if isinstance(node, float):
        return {node}
    if isinstance(node, dict):
        out: set[float] = set()
        for value in node.values():
            out |= _floats(value)
        return out
    if isinstance(node, (list, tuple)):
        out = set()
        for value in node:
            out |= _floats(value)
        return out
    return set()


class OverviewRenderingTests(unittest.TestCase):
    def test_experiments_renders_the_tier_distribution(self):
        text = handlers.render_experiments({
            "configured": "shadow_roster_v1",
            "experiments": [{
                "name": "shadow_roster_v1",
                "status": "running",
                "planned_variants": 4,
                "primary_metric": "mean_net_pct",
                "owner": "bryan",
                "configured": True,
                "arms": [
                    {"arm_id": 1, "slug": "momentum_v1", "version": "1.0.0",
                     "mode": "shadow", "status": "active", "risk_budget": 0.01,
                     "decisions": 12},
                    {"arm_id": 2, "slug": "earnings_drift_v1", "version": "1.0.0",
                     "mode": "shadow", "status": "paused", "risk_budget": 0.01,
                     "decisions": 3},
                ],
            }],
        })
        self.assertIn("shadow_roster_v1", _plain(text))
        self.assertIn("2 shadow", _plain(text))
        self.assertIn("`#1`", _plain(text))
        self.assertIn("no broker order", _plain(text))

    def test_no_experiment_yet_explains_how_one_appears(self):
        text = handlers.render_experiments(
            {"configured": "shadow_roster_v1", "experiments": []}
        )
        self.assertIn("No experiment is registered yet", _plain(text))

    def test_an_unknown_slug_lists_the_roster(self):
        text = handlers.render_strategy(
            "nope", {"unknown": "nope", "roster": ["momentum_v1"]}, None
        )
        self.assertIn("Unknown strategy", _plain(text))
        self.assertIn("momentum_v1", _plain(text))

    def test_a_forward_only_strategy_is_labelled(self):
        text = handlers.render_strategy("swingtrader_composite_v1", {
            "slug": "swingtrader_composite_v1",
            "version": "1.0.0",
            "status": "shadow",
            "scope": "ticker",
            "policy": "swingtrader_memo_trade_params_v1",
            "historically_replayable": False,
            "replayability_reason": "an LLM conclusion is not reconstructible at time T",
            "hypothesis": "the incumbent, as a compatibility adapter",
            "arms": [],
            "recent_decisions": [],
        }, None)
        self.assertIn("forward-only", _plain(text))
        self.assertIn("not reconstructible", _plain(text))


# --------------------------------------------------------------------------- #
# The registered command surface
# --------------------------------------------------------------------------- #


class CommandSurfaceTests(unittest.TestCase):
    """What the owner surface is, and the two things it still may not contain.

    PR 4 asserted that the tier controls were *absent*, because the promotion path
    did not exist. PR 6 built that path, so the same two properties are now stated
    about the surface that exists: the tier commands are registered, and neither
    they nor anything else in this module can reach a broker or stand up a second
    kill switch.
    """

    def test_the_module_exposes_the_tier_commands(self):
        exported = dir(handlers)
        for expected in (
            "promote_arm_command", "demote_arm_command", "promotions_command",
            "handle_promotion_callback",
        ):
            self.assertIn(expected, exported)

    def test_no_strategy_lab_handler_names_an_order_call(self):
        import inspect

        source = inspect.getsource(handlers)
        for forbidden in (
            "submit_order", "place_order", "OrderManager", "create_brokers",
            "execution.", "from execution",
        ):
            self.assertNotIn(
                forbidden, source,
                f"{forbidden!r} appears in the Strategy Lab bot handlers",
            )

    def test_the_promotion_card_says_it_is_not_an_entry_approval(self):
        """Spec Q §13: a strategy promotion never approves an individual entry."""
        plan = _a_plan()
        text = _plain(handlers.render_promotion(plan, confirmable=True))
        self.assertIn("not", text)
        self.assertIn("approval of any entry", text)
        self.assertIn("single-use", text)

    def test_a_blocked_plan_renders_every_refusal_and_no_buttons(self):
        plan = _a_plan(
            refusals=("the target arm is active",),
            external_refusals=("STRATEGY_LAB_LIVE_ENABLED is false",),
        )
        text = _plain(handlers.render_promotion(plan, confirmable=False))
        self.assertIn("the target arm is active", text)
        self.assertIn("STRATEGY_LAB_LIVE_ENABLED is false", text)
        self.assertIn("Nothing was changed", text)

    def test_the_bot_registers_the_tier_commands_and_one_kill_switch(self):
        import bot.telegram_bot as module

        source = inspect_source(module)
        for command in (
            "experiments", "strategies", "strategy",
            "pause_experiment", "resume_experiment",
            "promote_arm", "demote_arm", "promotions",
        ):
            self.assertIn(f'CommandHandler("{command}"', source)
        # Phase 6's kill switch is untouched, still registered, and still the
        # only one: a second switch would be a second thing to remember to set.
        self.assertIn('CommandHandler("live_kill", live_kill_command)', source)
        self.assertEqual(source.count('CommandHandler("live_kill"'), 1)
        for forbidden in ("lab_kill", "kill_lab", "strategy_kill"):
            self.assertNotIn(f'CommandHandler("{forbidden}"', source)


def _a_plan(*, refusals=(), external_refusals=()):
    """A `PromotionPlan` built by hand, so the renderer is tested without a database."""
    from datetime import datetime

    from strategy_lab import promotion as pm
    from strategy_lab.domain import ExecutionMode, PromotionKind

    evidence = pm.EvidenceCompleteness(
        metric_snapshot_id=7,
        arm_id=1,
        complete=True,
        missing=(),
        warnings=("small_sample_in_one_regime",),
        warnings_acknowledged=True,
        n_decisions=400,
        n_matured=120,
        n_closed=45,
        floor_name="paper_closed_executions",
        floor_value=30,
        floor_met=True,
        cutoff_utc=datetime(2026, 4, 1, 20, 0),
    )
    request = pm.PromotionRequest(
        source_arm_id=1,
        target_arm_id=2,
        evidence_metric_snapshot_id=7,
        requested_mode=ExecutionMode.LIVE,
        requested_risk_budget=0.0,
        owner="bryan",
        reason="preregistered gate met",
    )
    return pm.PromotionPlan(
        request=request,
        kind=PromotionKind.PROMOTION,
        from_mode=ExecutionMode.PAPER,
        to_mode=ExecutionMode.LIVE,
        source={
            "arm_id": 1, "experiment": "shadow_roster_v1", "slug": "momentum_v1",
            "version": "1.0.0", "strategy_version_id": 3, "mode": "paper",
            "status": "active", "risk_budget": 0.005, "promoted_from_arm_id": None,
        },
        target={
            "arm_id": 2, "experiment": "shadow_roster_v1", "slug": "momentum_v1",
            "version": "1.0.0", "strategy_version_id": 3, "mode": "live",
            "status": "inactive", "risk_budget": 0.0, "promoted_from_arm_id": 1,
        },
        evidence=evidence,
        refusals=tuple(refusals),
        external_refusals=tuple(external_refusals),
        recommendation=pm.READY_FOR_OWNER_REVIEW,
        notes=(
            "Promotion is not entry approval: every proposed live execution "
            "still needs its own signed, expiring, single-use owner callback.",
        ),
    )


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
