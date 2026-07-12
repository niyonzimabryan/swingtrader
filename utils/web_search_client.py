"""
Web search abstraction.

Default provider: Gemini Pro + Google Search grounding.
Fallback/alternate provider: Anthropic Sonnet + web_search_20250305.
"""

import json

from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception

from utils import gemini_ledger
from utils.anthropic_client import AnthropicClient
from utils.logger import get_logger

log = get_logger("web_search")

RAW_PARSE_ERROR_LIMIT = 20_000


def is_gemini_billing_error(exc: BaseException) -> bool:
    """
    True for Gemini quota/billing exhaustion — Google reports depleted prepay
    credits as 429 RESOURCE_EXHAUSTED ("prepayment credits are depleted"), which
    looks like a transient rate limit but is persistent until a top-up. Retrying
    it just burns ~60s of backoff per call across every Gemini stage (seen in
    prod 2026-07-08). Must fail fast and be loud instead.
    """
    message = str(exc).lower()
    return "prepayment credits" in message or "credits are depleted" in message


def _is_retryable_gemini_error(exc: BaseException) -> bool:
    """
    Transient Gemini failures worth retrying: 5xx server errors, 429 rate limits,
    and transport errors. Duck-typed (via the error's `code`/class name) so this
    module doesn't hard-depend on google-genai being importable.
    """
    if is_gemini_billing_error(exc):
        # Loud, greppable marker — mirrors anthropic_client's anthropic_credit_exhausted.
        log.critical("gemini_credit_exhausted", error=str(exc)[:300])
        return False
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    return type(exc).__name__ == "ServerError"


# Same policy as anthropic_client._llm_retry: 3 attempts, exponential backoff,
# reraise the original error on final failure so callers/degradation paths see it.
_gemini_retry = retry(
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    retry=retry_if_exception(_is_retryable_gemini_error),
    reraise=True,
)


class WebSearchClient:
    """
    Abstraction for web search-augmented analysis.
    Supports:
    - "gemini": Gemini Pro + Google Search grounding
    - "anthropic": Sonnet + web_search tool
    """

    def __init__(self, provider: str, anthropic_client: AnthropicClient, settings=None):
        self.provider = provider
        self.anthropic_client = anthropic_client
        self.settings = settings
        self._gemini_client = None
        if self.provider == "gemini":
            self._gemini_client = self._init_gemini_client()

    def search_and_analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = None,
        max_searches: int = 10,
        max_tokens: int = 8192,
    ) -> str:
        """
        Run a web-search-augmented analysis.
        Returns the model's final text response (after any web searches).
        """
        if self.provider == "anthropic":
            return self._anthropic_search(
                system_prompt, user_prompt, model, max_searches, max_tokens
            )
        elif self.provider == "gemini":
            text, _ = self._gemini_search(
                system_prompt, user_prompt, model, max_searches, max_tokens
            )
            return text
        else:
            raise ValueError(f"Unsupported web search provider: {self.provider}")

    def search_and_analyze_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = None,
        max_searches: int = 10,
        max_tokens: int = 8192,
    ) -> dict:
        """Web-search-augmented analysis that returns parsed JSON."""
        if self.provider == "anthropic":
            model = model or self._default_model()
            tools = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
            ]
            return self.anthropic_client.analyze_with_tools_json(
                model, system_prompt, user_prompt, tools, max_tokens
            )
        elif self.provider == "gemini":
            text, grounding = self._gemini_search(
                system_prompt, user_prompt, model, max_searches, max_tokens
            )
            result = self._parse_json(text)
            if grounding:
                result["_grounding"] = grounding
            return result
        else:
            raise ValueError(f"Unsupported web search provider: {self.provider}")

    def search_and_analyze_json_with_grounding(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = None,
        max_searches: int = 10,
        max_tokens: int = 8192,
    ) -> dict:
        """Return parsed JSON plus grounding metadata when the provider exposes it."""
        return self.search_and_analyze_json(system_prompt, user_prompt, model, max_searches, max_tokens)

    def search_and_analyze_json_with_thinking(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = None,
        max_searches: int = 10,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
    ) -> dict:
        """
        Web-search-augmented analysis with extended thinking.
        The model thinks deeply before and after web searches.
        Returns parsed JSON.
        """
        if self.provider == "anthropic":
            model = model or self._default_model()
            tools = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
            ]
            return self.anthropic_client.analyze_with_tools_json_thinking(
                model, system_prompt, user_prompt, tools, budget_tokens, max_tokens
            )
        elif self.provider == "gemini":
            # Gemini Pro is the reasoning/search provider here. The API handles
            # reasoning internally, so budget_tokens is intentionally advisory.
            text, grounding = self._gemini_search(
                system_prompt, user_prompt, model, max_searches, max_tokens
            )
            result = self._parse_json(text)
            if grounding:
                result["_grounding"] = grounding
            return result
        else:
            raise ValueError(f"Unsupported web search provider: {self.provider}")

    def _anthropic_search(
        self, system_prompt, user_prompt, model, max_searches, max_tokens
    ) -> str:
        """Anthropic Sonnet + web_search_20250305 tool."""
        model = model or self._default_model()
        tools = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
        ]
        return self.anthropic_client.analyze_with_tools(
            model, system_prompt, user_prompt, tools, max_tokens
        )

    def _init_gemini_client(self):
        api_key = getattr(self.settings, "gemini_api_key", "") if self.settings else ""
        if not api_key:
            log.warning("gemini_search_missing_api_key")
            return None
        try:
            from google import genai

            return genai.Client(api_key=api_key)
        except ImportError:
            log.warning("google_genai_not_installed")
            return None
        except Exception as exc:
            log.error("gemini_search_init_failed", error=str(exc))
            return None

    def _gemini_search(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_searches: int,
        max_tokens: int,
    ) -> tuple[str, dict]:
        if not self._gemini_client:
            raise ValueError("Gemini search client is not initialized")

        from google.genai import types

        model = self._gemini_model(model)
        grounded_user_prompt = self._with_search_directive(user_prompt, max_searches)
        log.info(
            "gemini_grounded_search_call",
            model=model,
            provider=self.provider,
            max_searches=max_searches,
            max_tokens=max_tokens,
        )
        response = self._gemini_generate(
            model=model,
            contents=grounded_user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.25,
                max_output_tokens=max_tokens,
            ),
        )
        grounding = self._extract_grounding(response)
        log.info(
            "gemini_grounded_search_response",
            model=model,
            grounded=grounding.get("grounded", False),
            queries=len(grounding.get("queries", [])),
            sources=len(grounding.get("sources", [])),
        )
        return (response.text or "").strip(), grounding

    @_gemini_retry
    def _gemini_generate(self, model, contents, config):
        """Single Gemini generate_content call, retried on transient errors."""
        return gemini_ledger.generate_content(
            self._gemini_client,
            model=model,
            stage="web_search",
            contents=contents,
            config=config,
        )

    def _gemini_model(self, requested_model: str | None) -> str:
        if requested_model and not requested_model.startswith("claude-"):
            return requested_model
        if self.settings:
            return getattr(self.settings, "gemini_search_model", "gemini-3.1-pro-preview")
        return "gemini-3.1-pro-preview"

    def _with_search_directive(self, user_prompt: str, max_searches: int) -> str:
        return (
            f"{user_prompt}\n\n"
            "SEARCH REQUIREMENT:\n"
            f"- Use Google Search grounding before answering. Run up to {max_searches} distinct search-query clusters if needed.\n"
            "- Prefer primary/current sources: company IR, SEC filings, exchange releases, FDA/regulator pages, earnings transcripts, reputable financial news, and analyst-action reporting.\n"
            "- Cross-check each important claim against at least two independent sources when available.\n"
            "- Do not rely on model memory for current market facts.\n"
            "- Include enough evidence in the JSON fields for downstream trade scrutiny."
        )

    def _parse_json(self, text: str) -> dict:
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("gemini_json_parse_failed", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:RAW_PARSE_ERROR_LIMIT]}

    def _extract_grounding(self, response) -> dict:
        metadata = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            metadata = getattr(candidates[0], "grounding_metadata", None)
        if not metadata:
            return {"grounded": False, "queries": [], "sources": []}

        data = metadata.model_dump(exclude_none=True) if hasattr(metadata, "model_dump") else {}
        queries = (
            data.get("web_search_queries")
            or data.get("webSearchQueries")
            or data.get("retrieval_queries")
            or data.get("retrievalQueries")
            or []
        )
        sources = []
        chunks = data.get("grounding_chunks") or data.get("groundingChunks") or []
        for chunk in chunks:
            web = chunk.get("web", {}) if isinstance(chunk, dict) else {}
            uri = web.get("uri")
            title = web.get("title", "")
            if uri:
                sources.append({"title": title, "uri": uri})

        return {
            "grounded": bool(queries or sources),
            "queries": queries[:12],
            "sources": sources[:12],
        }

    def _default_model(self) -> str:
        """Get default model from settings or fallback."""
        if self.settings:
            return self.settings.analyst_model
        return "claude-sonnet-5"
