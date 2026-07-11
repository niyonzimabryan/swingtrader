"""Ad-hoc runs must carry per-stage Langfuse tags (audit spec G1 / P2-LF-1).

Before this fix the ad-hoc path (`/test` command) wrapped the whole run in
`["ad_hoc", ticker]` only, so its scoring-shaped Opus generations lacked the
`scoring` tag and were undercounted in the BRY-243 corpus. The scheduled path
(`_process_scan_item`) tags catalyst/scoring/memo and the shared
`_run_post_catalyst_agents` tags fundamental/pattern/web_research; the ad-hoc
path must emit the same set. `_langfuse_context` is replaced with a recorder;
no Langfuse/network.
"""

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator import pipeline as pipeline_mod
from orchestrator.pipeline import TradingPipeline


def _catalyst():
    return SimpleNamespace(
        score=0.8, raw_data={}, reasoning="because", direction="bullish",
    )


def _make_pipeline(recorded_tags):
    pipe = TradingPipeline.__new__(TradingPipeline)
    pipe.settings = SimpleNamespace(
        parallel_agents_enabled=False,       # serial → no stability controller needed
        parallel_timeout_fundamental_s=5,
        parallel_timeout_pattern_s=5,
        parallel_timeout_web_research_s=5,
    )
    pipe.macro_agent = Mock(get_latest_regime=Mock(return_value={}))
    pipe.catalyst_agent = Mock(analyze=Mock(return_value=_catalyst()))
    pipe.fundamental_agent = Mock(analyze=Mock(return_value=SimpleNamespace(reasoning="f")))
    pipe.pattern_agent = Mock(analyze=Mock(return_value=SimpleNamespace(reasoning="p")))
    pipe.web_research_agent = Mock(analyze=Mock(return_value=SimpleNamespace(reasoning="w")))
    pipe.scoring_engine = Mock(score_opportunity=Mock(return_value={"final_score": 0.5}))
    pipe.memo_generator = Mock(generate=Mock(return_value={"memo_id": 1}))
    # Stub out the DB-touching + broker-touching helpers.
    pipe._ensure_ticker = lambda ticker: None
    pipe.get_sector = lambda ticker: "Technology"
    pipe._get_portfolio_context = lambda: "Portfolio: $0"
    pipe._start_pipeline_run = lambda *a, **k: None
    pipe._finish_pipeline_run = lambda *a, **k: None
    return pipe


class AdHocStageTagTests(unittest.TestCase):
    def _run_and_collect_tags(self):
        recorded = []

        def _recorder(session_id=None, tags=None):
            recorded.append(tags)
            return contextlib.nullcontext()

        pipe = _make_pipeline(recorded)
        with patch.object(pipeline_mod, "_langfuse_context", side_effect=_recorder):
            memo = pipe.run_ad_hoc("AAPL", thesis="")
        self.assertEqual(memo, {"memo_id": 1})
        return recorded

    def test_ad_hoc_emits_all_stage_tags(self):
        recorded = self._run_and_collect_tags()

        # Outer session tag + every per-stage tag, each with the ticker.
        self.assertIn(["ad_hoc", "AAPL"], recorded)
        for stage in ("catalyst", "fundamental", "pattern", "web_research", "scoring", "memo"):
            self.assertIn([stage, "AAPL"], recorded, f"missing {stage} tag")

    def test_scoring_tag_present_for_corpus_inclusion(self):
        # The specific regression: ad-hoc scoring calls now carry `scoring`.
        recorded = self._run_and_collect_tags()
        self.assertIn(["scoring", "AAPL"], recorded)

    def test_no_stray_untagged_stage(self):
        # Every recorded context is either the session tag or a [stage, ticker] pair.
        recorded = self._run_and_collect_tags()
        allowed = {"ad_hoc", "catalyst", "fundamental", "pattern", "web_research", "scoring", "memo"}
        for tags in recorded:
            self.assertTrue(tags, "empty tag list recorded")
            self.assertIn(tags[0], allowed, f"unexpected tag group {tags}")
            self.assertEqual(tags[1], "AAPL")


if __name__ == "__main__":
    unittest.main()
