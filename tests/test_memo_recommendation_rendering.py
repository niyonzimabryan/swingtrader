"""Verify memo rendering and keyboard buttons differ correctly across Opus
recommendations: BUY/APPROVE (proceed) vs WATCHLIST vs PASS.

Goal of t_2b189c02: WATCHLIST/PASS memos must NOT present Sonnet draft params
as if Opus endorsed them, and the action buttons must not let the user approve
a non-endorsed trade.
"""

import unittest
from types import SimpleNamespace

from bot.keyboards import memo_approval_keyboard
from memo.generator import MemoGenerator
from memo.templates.ic_memo import format_memo_plain, format_memo_telegram


def _base_memo(opus_recommendation: str) -> dict:
    return {
        "ticker": "ACME",
        "direction": "long",
        "composite_score": 0.78,
        "classification": "high_conviction",
        "generated_at": "2026-05-10T12:00",
        "thesis": "Test thesis",
        "catalyst": {
            "catalyst_type": "earnings",
            "catalyst_summary": "Q1 beat",
            "materiality": 0.7,
            "direction_confidence": 0.7,
        },
        "fundamental": {
            "quality_score": 0.8, "valuation_score": 0.6,
            "growth_score": 0.7, "balance_sheet_score": 0.9,
        },
        "pattern": {"status": "stub"},
        "web_research": {"status": "stub"},
        "trade_params": {
            "entry_price": 100.0, "stop_loss": 95.0, "stop_pct": 5.0,
            "target_1": 110.0, "target_1_pct": 10.0,
            "target_2": 120.0, "target_2_pct": 20.0,
            "position_pct": 5.0, "dollar_amount": 5000.0, "shares": 50,
            "risk_reward": 2.0, "max_hold_days": 20,
        },
        "opus_evaluation": {
            "recommendation": opus_recommendation,
            "conviction": "medium",
            "key_risk": "Macro headwinds",
            "stress_test": "ok",
            "reasoning": "Reasoning text for stop test.",
        },
    }


def _memo_settings() -> SimpleNamespace:
    return SimpleNamespace(
        portfolio_value=100_000.0,
        base_position_pct=0.05,
        max_position_pct=0.10,
        min_position_pct=0.02,
        default_stop_loss_pct=0.05,
        max_stop_loss_pct=0.08,
        max_holding_days=20,
    )


class MemoRenderingByRecommendationTests(unittest.TestCase):
    # --- BUY / APPROVE (proceed) ---
    def test_proceed_renders_executable_final_trade_params(self):
        for renderer in (format_memo_plain, format_memo_telegram):
            with self.subTest(renderer=renderer.__name__):
                out = renderer(_base_memo("proceed"))
                self.assertIn("FINAL TRADE PARAMETERS", out)
                self.assertNotIn("REFERENCE PARAMS", out)
                self.assertNotIn("NOT EXECUTABLE", out)
                # Executable price values should be present
                self.assertIn("100", out)  # entry
                self.assertIn("95", out)   # stop

    # --- WATCHLIST ---
    def test_watchlist_marks_params_non_executable(self):
        for renderer in (format_memo_plain, format_memo_telegram):
            with self.subTest(renderer=renderer.__name__):
                out = renderer(_base_memo("watchlist"))
                self.assertIn("WATCHLIST", out)
                self.assertIn("NOT EXECUTABLE", out)
                self.assertIn("Sonnet", out)
                # Must NOT label these as final/executable
                self.assertNotIn("FINAL TRADE PARAMETERS", out)

    # --- PASS ---
    def test_pass_hides_trade_params_entirely(self):
        for renderer in (format_memo_plain, format_memo_telegram):
            with self.subTest(renderer=renderer.__name__):
                out = renderer(_base_memo("pass"))
                self.assertIn("PASS", out)
                self.assertIn("No trade parameters generated", out)
                self.assertNotIn("FINAL TRADE PARAMETERS", out)
                self.assertNotIn("REFERENCE PARAMS", out)


class MemoTradeParameterDirectionTests(unittest.TestCase):
    def setUp(self):
        self.generator = MemoGenerator(_memo_settings(), anthropic_client=None)

    def test_long_trade_params_put_stop_below_entry_and_targets_above(self):
        params = self.generator._compute_trade_params(
            price=100.0,
            atr=1.0,
            regime={"position_size_multiplier": 1.0},
            score=0.8,
            classification="moderate",
            direction="long",
        )

        self.assertEqual(params["direction"], "long")
        self.assertLess(params["stop_loss"], params["entry_price"])
        self.assertGreater(params["target_1"], params["entry_price"])
        self.assertGreater(params["target_2"], params["target_1"])
        self.assertGreater(params["target_1_pct"], 0)
        self.assertGreater(params["target_2_pct"], params["target_1_pct"])

    def test_short_trade_params_put_stop_above_entry_and_targets_below(self):
        params = self.generator._compute_trade_params(
            price=100.0,
            atr=1.0,
            regime={"position_size_multiplier": 1.0},
            score=0.8,
            classification="moderate",
            direction="short",
        )

        self.assertEqual(params["direction"], "short")
        self.assertGreater(params["stop_loss"], params["entry_price"])
        self.assertLess(params["target_1"], params["entry_price"])
        self.assertLess(params["target_2"], params["target_1"])
        self.assertGreater(params["target_1_pct"], 0)
        self.assertGreater(params["target_2_pct"], params["target_1_pct"])

    def test_short_memo_renders_inverted_params_matching_direction(self):
        memo = _base_memo("proceed")
        memo["direction"] = "short"
        memo["direction_raw"] = "bearish"
        memo["trade_params"] = self.generator._compute_trade_params(
            price=100.0,
            atr=1.0,
            regime={"position_size_multiplier": 1.0},
            score=0.8,
            classification="moderate",
            direction="short",
        )

        params = memo["trade_params"]
        self.assertGreater(params["stop_loss"], params["entry_price"])
        self.assertLess(params["target_1"], params["entry_price"])
        self.assertLess(params["target_2"], params["target_1"])

        for renderer in (format_memo_plain, format_memo_telegram):
            with self.subTest(renderer=renderer.__name__):
                out = renderer(memo)
                self.assertIn("SHORT", out)
                self.assertIn(f"{params['entry_price']:,.2f}", out)
                self.assertIn(f"{params['stop_loss']:,.2f}", out)
                self.assertIn(f"{params['target_1']:,.2f}", out)
                self.assertIn(f"{params['target_2']:,.2f}", out)


def _button_labels(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


class KeyboardButtonsByRecommendationTests(unittest.TestCase):
    def test_proceed_keyboard_has_approve_button(self):
        labels = _button_labels(
            memo_approval_keyboard(memo_id=1, show_deep_research=False, opus_recommendation="proceed")
        )
        self.assertTrue(any("Approve" in l for l in labels))

    def test_watchlist_keyboard_has_no_approve_button(self):
        labels = _button_labels(
            memo_approval_keyboard(memo_id=1, show_deep_research=False, opus_recommendation="watchlist")
        )
        self.assertFalse(any(l.strip().startswith("✅") and "Approve" in l for l in labels),
                         f"WATCHLIST keyboard must not offer plain Approve; got {labels}")
        # Must offer watchlist + dismiss + override paths instead
        self.assertTrue(any("Watchlist" in l for l in labels))
        self.assertTrue(any("Override" in l for l in labels))
        self.assertTrue(any("Dismiss" in l for l in labels))

    def test_pass_keyboard_has_no_approve_button(self):
        labels = _button_labels(
            memo_approval_keyboard(memo_id=1, show_deep_research=False, opus_recommendation="pass")
        )
        self.assertFalse(any("Approve" in l for l in labels),
                         f"PASS keyboard must not offer Approve; got {labels}")
        self.assertTrue(any("Dismiss" in l for l in labels))
        self.assertTrue(any("Override" in l for l in labels))


if __name__ == "__main__":
    unittest.main()
