"""
Web search abstraction. Default: Anthropic Sonnet + web_search_20250305.
Swap provider after A/B testing by changing settings.web_search_provider.
"""

from utils.anthropic_client import AnthropicClient
from utils.logger import get_logger

log = get_logger("web_search")


class WebSearchClient:
    """
    Abstraction for web search-augmented analysis.
    Currently supports: "anthropic" (Sonnet + web_search tool).
    Future: "perplexity", "gemini" etc.
    """

    def __init__(self, provider: str, anthropic_client: AnthropicClient, settings=None):
        self.provider = provider
        self.anthropic_client = anthropic_client
        self.settings = settings

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
        else:
            raise ValueError(f"Unsupported web search provider: {self.provider}")

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

    def _default_model(self) -> str:
        """Get default model from settings or fallback."""
        if self.settings:
            return self.settings.analyst_model
        return "claude-sonnet-4-6"
