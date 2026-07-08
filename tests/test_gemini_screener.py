"""Tests for the tier-2 Gemini screener (spec C: audit-2026-07-04).

The Gemini client is mocked — no network. We drive `_screen_single` /
`screen_batch` with fake responses that reproduce the production failure
modes (truncation, empty/safety-blocked candidates, SDK enum drift) and
assert the parse-health counters, truncation-vs-parse-failure distinction,
and the >10% degraded warning.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from screening import gemini_screener as screener_mod
from screening.gemini_screener import (
    GeminiScreener,
    GeminiScreenResult,
    PARSE_FAIL_WARN_RATIO,
)


class _FakeResponse:
    """Stand-in for a google-genai GenerateContentResponse."""

    def __init__(self, text, finish_reason="STOP", text_raises=False, candidates=None):
        self._text = text
        self._text_raises = text_raises
        if candidates is None:
            candidates = [SimpleNamespace(finish_reason=_FinishReason(finish_reason))]
        self.candidates = candidates

    @property
    def text(self):
        if self._text_raises:
            raise ValueError("Response has no text parts (safety block)")
        return self._text


class _FinishReason:
    """Mimics the SDK enum: has a `.name`. A plain string is used for drift tests."""

    def __init__(self, name):
        self.name = name


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, model, contents, config):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def _make_screener(responses, threshold=0.5, max_tokens=4096):
    scr = GeminiScreener.__new__(GeminiScreener)
    scr.settings = None
    scr._model = "gemini-2.5-flash"
    scr._threshold = threshold
    scr._max_output_tokens = max_tokens
    scr._client = _FakeClient(responses)
    return scr


def _batch(symbols):
    return [{"symbol": s, "catalyst_context": "test"} for s in symbols]


VALID_JSON = (
    '{"score": 0.72, "direction": "bullish", '
    '"summary": "Clean breakout.", "catalysts": ["earnings"], "risks": ["macro"]}'
)
TRUNCATED_JSON = '{\n "score": 0.6,\n "direction": "bull'  # from the audit log


class GeminiScreenerParseTests(unittest.TestCase):
    def test_valid_json_parses_and_escalates(self):
        scr = _make_screener([_FakeResponse(VALID_JSON, "STOP")])
        batch = scr.screen_batch(_batch(["AAPL"]))

        self.assertEqual(batch.attempted, 1)
        self.assertEqual(batch.parsed, 1)
        self.assertEqual(batch.parse_failed, 0)
        self.assertEqual(batch.truncated, 0)
        self.assertEqual(batch.empty, 0)
        self.assertFalse(batch.degraded)
        r = batch.results[0]
        self.assertAlmostEqual(r.score, 0.72)
        self.assertEqual(r.direction, "bullish")
        self.assertTrue(r.escalate)
        self.assertEqual(batch.escalated, ["AAPL"])

    def test_prose_wrapped_json_extracts_first_balanced_block(self):
        # Preamble before + trailing prose that itself contains braces; a naive
        # first-{ / last-} slice would break, the balanced scanner must not.
        text = (
            "Here is the analysis for the ticker:\n\n"
            + VALID_JSON
            + "\n\nLet me know if you want more {deep dive} details."
        )
        scr = _make_screener([_FakeResponse(text, "STOP")])
        batch = scr.screen_batch(_batch(["AAPL"]))
        self.assertEqual(batch.parsed, 1)
        self.assertAlmostEqual(batch.results[0].score, 0.72)

    def test_code_fenced_json_parses(self):
        text = "```json\n" + VALID_JSON + "\n```"
        scr = _make_screener([_FakeResponse(text, "STOP")])
        batch = scr.screen_batch(_batch(["AAPL"]))
        self.assertEqual(batch.parsed, 1)

    def test_none_text_falls_back_without_exception(self):
        scr = _make_screener([_FakeResponse(None, "STOP")])
        batch = scr.screen_batch(_batch(["EA"]))
        self.assertEqual(batch.empty, 1)
        self.assertEqual(batch.parsed, 0)
        self.assertEqual(batch.errors, [])  # guarded, not an exception
        r = batch.results[0]
        self.assertEqual(r.score, 0.0)
        self.assertFalse(r.escalate)
        self.assertIn("Empty response", r.summary)

    def test_safety_blocked_text_raises_is_guarded(self):
        scr = _make_screener([_FakeResponse(None, "SAFETY", text_raises=True)])
        batch = scr.screen_batch(_batch(["CPB"]))
        self.assertEqual(batch.empty, 1)
        self.assertEqual(batch.errors, [])
        self.assertIn("Empty response", batch.results[0].summary)

    def test_max_tokens_counts_as_truncated_not_parse_failed(self):
        scr = _make_screener([_FakeResponse(TRUNCATED_JSON, "MAX_TOKENS")])
        batch = scr.screen_batch(_batch(["NVDA"]))
        self.assertEqual(batch.truncated, 1)
        self.assertEqual(batch.parse_failed, 0)
        self.assertEqual(batch.parsed, 0)
        self.assertIn("truncated", batch.results[0].summary.lower())

    def test_unparseable_non_truncated_is_parse_failed(self):
        scr = _make_screener([_FakeResponse("no json at all here", "STOP")])
        batch = scr.screen_batch(_batch(["XYZ"]))
        self.assertEqual(batch.parse_failed, 1)
        self.assertEqual(batch.truncated, 0)
        self.assertEqual(batch.parsed, 0)

    def test_unknown_finish_reason_string_does_not_raise(self):
        # SDK enum drift: finish_reason arrives as a bare string, not an enum.
        resp = _FakeResponse(VALID_JSON)
        resp.candidates = [SimpleNamespace(finish_reason="TOO_MANY_TOOL_CALLS")]
        scr = _make_screener([resp])
        batch = scr.screen_batch(_batch(["AAPL"]))
        self.assertEqual(batch.parsed, 1)  # still parses; no exception


class GeminiScreenerSummaryTests(unittest.TestCase):
    def test_summary_counters_correct_across_mixed_batch(self):
        responses = [
            _FakeResponse(VALID_JSON, "STOP"),          # parsed
            _FakeResponse(TRUNCATED_JSON, "MAX_TOKENS"),  # truncated
            _FakeResponse(None, "STOP"),                # empty
            _FakeResponse("garbage", "STOP"),           # parse_failed
            _FakeResponse(VALID_JSON, "STOP"),          # parsed
        ]
        scr = _make_screener(responses)
        batch = scr.screen_batch(_batch(["A", "B", "C", "D", "E"]))

        self.assertEqual(batch.attempted, 5)
        self.assertEqual(batch.parsed, 2)
        self.assertEqual(batch.truncated, 1)
        self.assertEqual(batch.empty, 1)
        self.assertEqual(batch.parse_failed, 1)
        # attempted == sum of all buckets + call errors
        self.assertEqual(
            batch.parsed + batch.truncated + batch.empty + batch.parse_failed + len(batch.errors),
            batch.attempted,
        )
        self.assertAlmostEqual(batch.parse_fail_rate, 0.2)

    def test_summary_event_logged_once(self):
        scr = _make_screener([_FakeResponse(VALID_JSON, "STOP")])
        with patch.object(screener_mod, "log") as mock_log:
            scr.screen_batch(_batch(["AAPL"]))
        summary_events = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "gemini_screen_summary"]
        self.assertEqual(len(summary_events), 1)

    def test_degraded_warning_emitted_above_threshold(self):
        # 2/10 parse failures = 0.20 > 0.10 → degraded.
        responses = [_FakeResponse("garbage", "STOP")] * 2 + [_FakeResponse(VALID_JSON, "STOP")] * 8
        scr = _make_screener(responses)
        with patch.object(screener_mod, "log") as mock_log:
            batch = scr.screen_batch(_batch([f"T{i}" for i in range(10)]))

        self.assertTrue(batch.degraded)
        self.assertAlmostEqual(batch.parse_fail_rate, 0.2)
        degraded_events = [c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "gemini_screen_degraded"]
        self.assertEqual(len(degraded_events), 1)

    def test_degraded_not_emitted_at_or_below_threshold(self):
        # 1/10 = 0.10 is NOT strictly greater than 0.10 → not degraded.
        responses = [_FakeResponse("garbage", "STOP")] + [_FakeResponse(VALID_JSON, "STOP")] * 9
        scr = _make_screener(responses)
        with patch.object(screener_mod, "log") as mock_log:
            batch = scr.screen_batch(_batch([f"T{i}" for i in range(10)]))

        self.assertFalse(batch.degraded)
        self.assertAlmostEqual(batch.parse_fail_rate, 0.1)
        degraded_events = [c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "gemini_screen_degraded"]
        self.assertEqual(degraded_events, [])

    def test_truncation_alone_does_not_trigger_degraded(self):
        # Spec C3 gates the degraded warning on parse_failed only; truncation has its
        # own per-ticker gemini_screen_truncated signal + summary counter.
        responses = [_FakeResponse(TRUNCATED_JSON, "MAX_TOKENS")] * 5 + [_FakeResponse(VALID_JSON, "STOP")] * 5
        scr = _make_screener(responses)
        batch = scr.screen_batch(_batch([f"T{i}" for i in range(10)]))
        self.assertEqual(batch.truncated, 5)
        self.assertEqual(batch.parse_failed, 0)
        self.assertFalse(batch.degraded)


class GeminiScreenerUnitTests(unittest.TestCase):
    def test_extract_json_balanced_scanner(self):
        scr = GeminiScreener.__new__(GeminiScreener)
        self.assertEqual(scr._extract_json('prefix {"a": 1} suffix'), {"a": 1})
        # Nested + trailing brace-y prose
        self.assertEqual(scr._extract_json('{"a": {"b": 2}} then {junk}'), {"a": {"b": 2}})
        # Brace inside a string value must not confuse depth tracking
        self.assertEqual(scr._extract_json('{"a": "has } brace"}'), {"a": "has } brace"})
        # Truncated → no balanced close → None
        self.assertIsNone(scr._extract_json('{"a": 1'))
        self.assertIsNone(scr._extract_json("no braces"))

    def test_finish_reason_coercion(self):
        scr = GeminiScreener.__new__(GeminiScreener)
        self.assertEqual(scr._finish_reason(SimpleNamespace(candidates=[SimpleNamespace(finish_reason=_FinishReason("MAX_TOKENS"))])), "MAX_TOKENS")
        self.assertEqual(scr._finish_reason(SimpleNamespace(candidates=[SimpleNamespace(finish_reason="DRIFT_VALUE")])), "DRIFT_VALUE")
        self.assertEqual(scr._finish_reason(SimpleNamespace(candidates=[])), "UNKNOWN")
        self.assertEqual(scr._finish_reason(SimpleNamespace(candidates=[SimpleNamespace(finish_reason=None)])), "UNKNOWN")

    def test_response_text_none_and_raises(self):
        scr = GeminiScreener.__new__(GeminiScreener)
        self.assertEqual(scr._response_text(_FakeResponse(None)), "")
        self.assertEqual(scr._response_text(_FakeResponse("  hi  ")), "hi")
        self.assertEqual(scr._response_text(_FakeResponse(None, text_raises=True)), "")

    def test_warn_ratio_constant(self):
        self.assertEqual(PARSE_FAIL_WARN_RATIO, 0.10)


if __name__ == "__main__":
    unittest.main()
