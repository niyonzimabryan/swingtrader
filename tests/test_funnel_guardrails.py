"""Spec I0 — funnel guardrails: tier-2 escalation cap + catalyst hard cap.

Both caps protect the first healthy scan from a cost/duration bomb without ever
evicting a higher-priority source for a lower one.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.pipeline import TradingPipeline
from screening.gemini_screener import GeminiBatchResult, GeminiScreenResult


class Tier2EscalationCapTests(unittest.TestCase):
    def _result(self, pairs, escalated):
        return GeminiBatchResult(
            results=[GeminiScreenResult(ticker=t, score=s, summary="", escalate=True) for t, s in pairs],
            escalated=list(escalated),
            total_screened=len(pairs),
        )

    def test_keeps_top_n_by_score_ties_by_symbol(self):
        gr = self._result(
            [("AAA", 0.60), ("BBB", 0.90), ("CCC", 0.70), ("DDD", 0.70), ("EEE", 0.50)],
            ["AAA", "BBB", "CCC", "DDD", "EEE"],
        )
        kept, dropped = TradingPipeline._cap_tier2_escalations(gr, cap=3)
        # Top-3 by score desc; CCC/DDD tie at 0.70 → symbol asc keeps CCC before DDD.
        self.assertEqual(kept, ["BBB", "CCC", "DDD"])
        self.assertEqual(dropped, 2)

    def test_no_cap_when_under_limit(self):
        gr = self._result([("AAA", 0.6), ("BBB", 0.9)], ["AAA", "BBB"])
        kept, dropped = TradingPipeline._cap_tier2_escalations(gr, cap=25)
        self.assertEqual(dropped, 0)
        self.assertEqual(set(kept), {"AAA", "BBB"})

    def test_cap_zero_is_noop(self):
        gr = self._result([("AAA", 0.6)], ["AAA"])
        kept, dropped = TradingPipeline._cap_tier2_escalations(gr, cap=0)
        self.assertEqual((kept, dropped), (["AAA"], 0))


class CatalystHardCapTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = TradingPipeline.__new__(TradingPipeline)
        self.pipeline.settings = SimpleNamespace(
            watchlist_haiku_threshold=2,
            catalyst_escalation_threshold=3,
            scan_max_catalyst_tickers=3,
        )

    def test_cap_evicts_lowest_priority_only(self):
        # Two tier-2 escalations + two tier-1 flags → 4 candidates, cap 3.
        structured_result = SimpleNamespace(
            flagged=[
                SimpleNamespace(symbol="CCC", catalysts=["x"], change_pct=None, volume_ratio=None, earnings_date=None, sector="Tech"),
                SimpleNamespace(symbol="DDD", catalysts=["y"], change_pct=None, volume_ratio=None, earnings_date=None, sector="Tech"),
            ]
        )
        gemini_result = GeminiBatchResult(
            results=[
                GeminiScreenResult(ticker="AAA", score=0.9, summary="", escalate=True),
                GeminiScreenResult(ticker="BBB", score=0.8, summary="", escalate=True),
            ],
            escalated=["AAA", "BBB"],
            total_screened=2,
        )
        with patch("orchestrator.pipeline.get_watchlist", return_value=[]):
            scan_list = TradingPipeline._build_scan_list(
                self.pipeline, SimpleNamespace(tickers=[]), structured_result, gemini_result,
            )
        tickers = [s.ticker for s in scan_list]
        # tier2 (AAA, BBB) always survive; one tier-1 kept, the lowest-priority dropped.
        self.assertEqual(len(tickers), 3)
        self.assertEqual(tickers[:2], ["AAA", "BBB"])
        self.assertIn("CCC", tickers)
        self.assertNotIn("DDD", tickers)


if __name__ == "__main__":
    unittest.main()
