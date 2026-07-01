"""providers — thin model-call wrappers for LIVE candidate replay.

Kept out of the core path: the harness (run.evaluate) never imports this unless an
adapter chooses live replay. SDKs (anthropic, google-generativeai) import lazily so
the core stays zero-dependency and offline/CI use needs no API keys.

An adapter builds a Model, then wraps it in a `candidate_output_for(record)` that
reconstructs the app's own prompt and parses the app's own output shape.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Response:
    text: str
    usage: dict = field(default_factory=dict)   # {"in": tok, "out": tok}


class MockModel:
    """Deterministic stand-in for tests/demos. `fn(system, user) -> str | (str, usage)`."""
    def __init__(self, fn, name="mock"):
        self.fn = fn
        self.name = name

    def complete(self, system: str, user: str, **kw) -> Response:
        out = self.fn(system, user)
        if isinstance(out, tuple):
            text, usage = out
            return Response(text=text, usage=usage)
        return Response(text=out, usage={})


class AnthropicModel:
    def __init__(self, model_id: str, max_tokens: int = 2048):
        self.name = model_id
        self.max_tokens = max_tokens
        self._client = None

    def _c(self):
        if self._client is None:
            import anthropic  # lazy
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def complete(self, system: str, user: str, **kw) -> Response:
        msg = self._c().messages.create(
            model=self.name, max_tokens=kw.get("max_tokens", self.max_tokens),
            system=system, messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return Response(text=text, usage={"in": msg.usage.input_tokens, "out": msg.usage.output_tokens})


class GeminiModel:
    def __init__(self, model_id: str):
        self.name = model_id
        self._model = None

    def _m(self):
        if self._model is None:
            import google.generativeai as genai  # lazy
            genai.configure(api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
            self._model = genai.GenerativeModel(self.name)
        return self._model

    def complete(self, system: str, user: str, **kw) -> Response:
        resp = self._m().generate_content(f"{system}\n\n{user}" if system else user)
        um = getattr(resp, "usage_metadata", None)
        usage = {"in": getattr(um, "prompt_token_count", 0),
                 "out": getattr(um, "candidates_token_count", 0)} if um else {}
        return Response(text=resp.text, usage=usage)


def build(model_id: str, catalog, **kw):
    """Construct a live Model for `model_id`, routing by the registry's provider field."""
    provider = catalog.entry(model_id).get("provider")
    if provider == "anthropic":
        return AnthropicModel(model_id, **kw)
    if provider == "google":
        return GeminiModel(model_id)
    raise ValueError(f"no live provider wired for {model_id!r} (provider={provider})")
