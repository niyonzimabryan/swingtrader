"""Tests for the shared Gemini call ledger (audit spec G2 / P2-LF-3).

Every Google GenAI `generate_content` call must emit exactly one structlog
`llm_call` event carrying provider/model/stage/ticker/duration_ms/token counts/
finish_reason/ok — the greppable Railway-log source for Gemini telemetry. The
SDK client is faked; no network.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import gemini_ledger


def _response(finish="STOP", prompt_tokens=123, out_tokens=45):
    return SimpleNamespace(
        text='{"score": 0.5}',
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens, candidates_token_count=out_tokens
        ),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
    )


class _Client:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _llm_events(mock_log):
    return [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "llm_call"]


class GeminiLedgerTests(unittest.TestCase):
    def test_success_emits_one_llm_call_with_required_fields(self):
        client = _Client(_response())
        with patch.object(gemini_ledger, "log") as mock_log:
            resp = gemini_ledger.generate_content(
                client, model="gemini-2.5-flash", stage="screening", ticker="AAPL",
                contents="prompt", config={"x": 1},
            )
        self.assertIs(resp, client._response)
        events = _llm_events(mock_log)
        self.assertEqual(len(events), 1)
        kw = events[0].kwargs
        self.assertEqual(kw["provider"], "gemini")
        self.assertEqual(kw["model"], "gemini-2.5-flash")
        self.assertEqual(kw["stage"], "screening")
        self.assertEqual(kw["ticker"], "AAPL")
        self.assertEqual(kw["input_tokens"], 123)
        self.assertEqual(kw["output_tokens"], 45)
        self.assertEqual(kw["finish_reason"], "STOP")
        self.assertTrue(kw["ok"])
        self.assertIsInstance(kw["duration_ms"], float)
        self.assertGreaterEqual(kw["duration_ms"], 0.0)

    def test_kwargs_forwarded_verbatim(self):
        client = _Client(_response())
        with patch.object(gemini_ledger, "log"):
            gemini_ledger.generate_content(
                client, model="m", stage="web_search", contents="c", config="cfg",
            )
        self.assertEqual(client.calls, [{"model": "m", "contents": "c", "config": "cfg"}])

    def test_failure_logs_ok_false_and_reraises(self):
        client = _Client(error=RuntimeError("boom"))
        with patch.object(gemini_ledger, "log") as mock_log:
            with self.assertRaises(RuntimeError):
                gemini_ledger.generate_content(client, model="m", stage="screening")
        events = _llm_events(mock_log)
        self.assertEqual(len(events), 1)
        kw = events[0].kwargs
        self.assertFalse(kw["ok"])
        self.assertIsNone(kw["input_tokens"])
        self.assertIsNone(kw["output_tokens"])
        self.assertIsNone(kw["finish_reason"])
        self.assertIsNone(kw["ticker"])  # no ticker passed

    def test_missing_usage_and_candidates_is_tolerated(self):
        # google-genai responses may lack usage_metadata / candidates entirely.
        client = _Client(SimpleNamespace(text="{}"))
        with patch.object(gemini_ledger, "log") as mock_log:
            gemini_ledger.generate_content(client, model="m", stage="web_search")
        kw = _llm_events(mock_log)[0].kwargs
        self.assertIsNone(kw["input_tokens"])
        self.assertIsNone(kw["output_tokens"])
        self.assertIsNone(kw["finish_reason"])
        self.assertTrue(kw["ok"])

    def test_finish_reason_bare_string_drift(self):
        # SDK enum drift: finish_reason may arrive as a plain string (no .name).
        resp = SimpleNamespace(
            text="{}", usage_metadata=None,
            candidates=[SimpleNamespace(finish_reason="TOO_MANY_TOOL_CALLS")],
        )
        client = _Client(resp)
        with patch.object(gemini_ledger, "log") as mock_log:
            gemini_ledger.generate_content(client, model="m", stage="screening")
        self.assertEqual(_llm_events(mock_log)[0].kwargs["finish_reason"], "TOO_MANY_TOOL_CALLS")


class GeminiScreenerLedgerIntegrationTests(unittest.TestCase):
    """The screener call site routes through the shared ledger."""

    def test_screen_single_emits_llm_call(self):
        from screening.gemini_screener import GeminiScreener

        scr = GeminiScreener.__new__(GeminiScreener)
        scr.settings = None
        scr._model = "gemini-2.5-flash"
        scr._threshold = 0.5
        scr._max_output_tokens = 4096
        scr._client = _Client(_response())

        with patch.object(gemini_ledger, "log") as mock_log:
            scr.screen_batch([{"symbol": "NVDA", "catalyst_context": "test"}])

        events = _llm_events(mock_log)
        self.assertEqual(len(events), 1)
        kw = events[0].kwargs
        self.assertEqual(kw["provider"], "gemini")
        self.assertEqual(kw["stage"], "screening")
        self.assertEqual(kw["ticker"], "NVDA")


if __name__ == "__main__":
    unittest.main()
