"""Anthropic client helpers for Claude 5-family request shape."""
from types import SimpleNamespace

from utils.anthropic_client import rejects_sampling, sampling_kwargs, text_from_message


def test_rejects_sampling_on_current_family():
    assert rejects_sampling("claude-sonnet-5") is True
    assert rejects_sampling("claude-opus-5") is True
    assert rejects_sampling("claude-opus-4-8") is True
    assert rejects_sampling("claude-fable-5-1") is True
    assert rejects_sampling("claude-opus-4-6") is False
    assert rejects_sampling("claude-haiku-4-5-20251001") is False


def test_sampling_kwargs_omitted_for_sonnet_5_and_thinking():
    assert sampling_kwargs("claude-sonnet-5", 0.3) == {}
    assert sampling_kwargs("claude-opus-5", 0.2) == {}
    assert sampling_kwargs("claude-opus-4-6", 0.3) == {"temperature": 0.3}
    assert sampling_kwargs("claude-opus-4-6", 1, thinking=True) == {}


def test_text_from_message_skips_thinking_blocks():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="", text=None),
            SimpleNamespace(type="text", text='{"score": 0.8}'),
        ]
    )
    assert text_from_message(response) == '{"score": 0.8}'
