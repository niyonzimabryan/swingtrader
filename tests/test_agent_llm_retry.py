"""Spec B4 — agent LLM calls retry transient failures via the shared client."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anthropic
import httpx

from utils.anthropic_client import AnthropicClient, _is_retryable_anthropic_error
from utils.escalation_manager import EscalationManager
from utils.web_search_client import WebSearchClient, _is_retryable_gemini_error


def _rate_limit_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError("rate limited", response=resp, body=None)


def _server_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(503, request=req)
    return anthropic.InternalServerError("server error", response=resp, body=None)


def _bad_request_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError("bad request", response=resp, body=None)


def _text_response(text):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


class RetryPredicateTest(unittest.TestCase):
    def test_transient_errors_are_retryable(self):
        self.assertTrue(_is_retryable_anthropic_error(_rate_limit_error()))
        self.assertTrue(_is_retryable_anthropic_error(_server_error()))
        self.assertTrue(_is_retryable_anthropic_error(anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))))

    def test_client_errors_are_not_retryable(self):
        self.assertFalse(_is_retryable_anthropic_error(_bad_request_error()))
        self.assertFalse(_is_retryable_anthropic_error(ValueError("nope")))


class AnthropicRetryTest(unittest.TestCase):
    def test_retries_on_rate_limit_then_succeeds(self):
        client = AnthropicClient(api_key="test")
        create = Mock(side_effect=[_rate_limit_error(), _rate_limit_error(), _text_response("ok")])
        client.client.messages.create = create
        with patch("time.sleep"):
            out = client.analyze("m", "sys", "user")
        self.assertEqual(out, "ok")
        self.assertEqual(create.call_count, 3)

    def test_gives_up_after_three_attempts(self):
        client = AnthropicClient(api_key="test")
        create = Mock(side_effect=_rate_limit_error())
        client.client.messages.create = create
        with patch("time.sleep"):
            with self.assertRaises(anthropic.RateLimitError):
                client.analyze("m", "sys", "user")
        self.assertEqual(create.call_count, 3)

    def test_client_error_not_retried(self):
        client = AnthropicClient(api_key="test")
        create = Mock(side_effect=_bad_request_error())
        client.client.messages.create = create
        with patch("time.sleep"):
            with self.assertRaises(anthropic.BadRequestError):
                client.analyze("m", "sys", "user")
        self.assertEqual(create.call_count, 1)  # failed fast, no retry


class CatalystEscalationRetryTest(unittest.TestCase):
    def test_catalyst_prescreen_retries_via_shared_client(self):
        client = AnthropicClient(api_key="test")
        good = _text_response('{"score": 4, "category": "m_and_a", "summary": "x", "direction": "bullish"}')
        create = Mock(side_effect=[_rate_limit_error(), good])
        client.client.messages.create = create
        settings = SimpleNamespace(catalyst_escalation_threshold=3)
        em = EscalationManager(client, settings)
        with patch("utils.escalation_manager.get_model", return_value="claude-haiku"), \
                patch("time.sleep"):
            result = em.haiku_prescreen("some material news", "AAPL", "ctx")
        self.assertEqual(result["score"], 4)
        self.assertEqual(create.call_count, 2)  # one 429 retried, then success


class ServerError(Exception):
    """Mimics google.genai.errors.ServerError (the predicate matches by class name)."""


class GeminiRetryTest(unittest.TestCase):
    def test_gemini_predicate(self):
        self.assertTrue(_is_retryable_gemini_error(ServerError("boom")))
        self.assertTrue(_is_retryable_gemini_error(SimpleNamespace(code=429)))
        self.assertTrue(_is_retryable_gemini_error(TimeoutError()))
        self.assertFalse(_is_retryable_gemini_error(SimpleNamespace(code=400)))

    def test_discovery_gemini_search_retries_then_succeeds(self):
        settings = SimpleNamespace(gemini_api_key="", gemini_search_model="gemini-3.1-pro-preview")
        wsc = WebSearchClient(provider="gemini", anthropic_client=None, settings=settings)

        good = SimpleNamespace(text='{"tickers": []}', candidates=[])
        generate = Mock(side_effect=[ServerError("500"), ServerError("500"), good])
        wsc._gemini_client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))

        with patch("time.sleep"):
            result = wsc.search_and_analyze_json("sys", "user", model="gemini-3.1-pro-preview")
        self.assertEqual(result.get("tickers"), [])
        self.assertEqual(generate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
