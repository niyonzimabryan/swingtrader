import json
import anthropic
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from utils.logger import get_logger

log = get_logger("anthropic_client")

# Sonnet fallback model for when Opus times out
SONNET_FALLBACK = "claude-sonnet-4-6"


class AnthropicClient:
    def __init__(self, api_key: str, timeout: int = 120):
        self.client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
        )
        self.api_key = api_key
        self.default_timeout = timeout

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    )
    def analyze(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Single completion call with retry on rate limits."""
        log.info("claude_api_call", model=model, prompt_len=len(user_prompt))
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = response.content[0].text
        log.info(
            "claude_api_response",
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return text

    def analyze_with_fallback(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        fallback_model: str = None,
    ) -> str:
        """
        Try model first; on timeout/rate-limit, fall back to Sonnet.
        Use this for Opus calls to prevent 30+ minute stalls.
        """
        fallback = fallback_model or SONNET_FALLBACK
        try:
            return self.analyze(model, system_prompt, user_prompt, max_tokens, temperature)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            log.warning(
                "opus_timeout_fallback",
                original_model=model,
                fallback_model=fallback,
                error=str(e),
            )
            return self.analyze(fallback, system_prompt, user_prompt, max_tokens, temperature)
        except anthropic.RateLimitError as e:
            log.warning(
                "opus_ratelimit_fallback",
                original_model=model,
                fallback_model=fallback,
                error=str(e),
            )
            return self.analyze(fallback, system_prompt, user_prompt, max_tokens, temperature)

    def analyze_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> dict:
        """Completion call that parses response as JSON."""
        json_system = system_prompt + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown, no code fences, no explanation."
        text = self.analyze(model, json_system, user_prompt, max_tokens, temperature=0.2)
        # Strip common wrapper artifacts
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("json_parse_failed", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:1000]}

    def analyze_json_with_fallback(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        fallback_model: str = None,
    ) -> dict:
        """JSON completion with Sonnet fallback on timeout/rate-limit."""
        json_system = system_prompt + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown, no code fences, no explanation."
        text = self.analyze_with_fallback(
            model, json_system, user_prompt, max_tokens, temperature=0.2, fallback_model=fallback_model
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("json_parse_failed", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:1000]}

    # --- V2: Extended Thinking ---

    def analyze_with_thinking(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
    ) -> str:
        """
        Completion with extended thinking enabled.
        The model reasons in a thinking block before producing output.
        Returns only the final text (thinking is logged but not returned).

        Note: Extended thinking requires temperature=1 (API-enforced).
        max_tokens must be >= budget_tokens.
        """
        effective_max = max(max_tokens, budget_tokens + 4096)
        log.info("claude_thinking_call", model=model, budget=budget_tokens,
                 prompt_len=len(user_prompt))

        response = self.client.messages.create(
            model=model,
            max_tokens=effective_max,
            temperature=1,  # Required for extended thinking
            thinking={
                "type": "enabled",
                "budget_tokens": budget_tokens,
            },
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract text blocks (skip thinking blocks)
        text_parts = []
        thinking_tokens = 0
        for block in response.content:
            if hasattr(block, "thinking"):
                thinking_tokens = len(block.thinking) // 4  # Rough token estimate
            elif hasattr(block, "text"):
                text_parts.append(block.text)

        log.info(
            "claude_thinking_response",
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            thinking_tokens_approx=thinking_tokens,
        )
        return "\n".join(text_parts)

    def analyze_json_with_thinking(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
    ) -> dict:
        """Extended thinking completion that parses response as JSON."""
        json_system = (
            system_prompt
            + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown, no code fences, no explanation."
        )
        text = self.analyze_with_thinking(
            model, json_system, user_prompt, budget_tokens, max_tokens
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        # Try to find JSON in the response (thinking models sometimes add preamble)
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("json_parse_failed_thinking", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:1000]}

    def analyze_json_with_thinking_and_fallback(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
        fallback_model: str = None,
    ) -> dict:
        """
        Extended thinking JSON completion with fallback.
        On timeout/rate-limit, falls back to non-thinking call on fallback model.
        """
        fallback = fallback_model or SONNET_FALLBACK
        try:
            return self.analyze_json_with_thinking(
                model, system_prompt, user_prompt, budget_tokens, max_tokens
            )
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            log.warning(
                "thinking_timeout_fallback",
                original_model=model,
                fallback_model=fallback,
                error=str(e),
            )
            return self.analyze_json(fallback, system_prompt, user_prompt, max_tokens=4096)
        except anthropic.RateLimitError as e:
            log.warning(
                "thinking_ratelimit_fallback",
                original_model=model,
                fallback_model=fallback,
                error=str(e),
            )
            return self.analyze_json(fallback, system_prompt, user_prompt, max_tokens=4096)

    def analyze_with_tools_and_thinking(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: list = None,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
        max_tool_rounds: int = 15,
    ) -> str:
        """
        Tool use + extended thinking combined.
        The model thinks before each tool call and before the final response.
        Used for discovery scan: web search + deep reasoning about which tickers to pursue.
        """
        if tools is None:
            tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]

        effective_max = max(max_tokens, budget_tokens + 8192)
        messages = [{"role": "user", "content": user_prompt}]
        log.info("claude_thinking_tool_start", model=model, budget=budget_tokens,
                 prompt_len=len(user_prompt))

        response = None
        for round_num in range(max_tool_rounds):
            response = self.client.messages.create(
                model=model,
                max_tokens=effective_max,
                temperature=1,  # Required for extended thinking
                thinking={
                    "type": "enabled",
                    "budget_tokens": budget_tokens,
                },
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            log.info(
                "claude_thinking_tool_round",
                model=model,
                round=round_num + 1,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

            if response.stop_reason == "end_turn":
                text_parts = []
                for block in response.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                return "\n".join(text_parts)

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Continue your analysis with the search results."}
                ]})

        log.warning("thinking_tool_rounds_exhausted", model=model, max_rounds=max_tool_rounds)
        if response:
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            return "\n".join(text_parts) if text_parts else ""
        return ""

    def analyze_with_tools_json_thinking(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: list = None,
        budget_tokens: int = 10000,
        max_tokens: int = 16000,
    ) -> dict:
        """Tool-use + extended thinking that returns parsed JSON."""
        json_system = (
            system_prompt
            + "\n\nIMPORTANT: After completing your research, respond ONLY with valid JSON. "
            "No markdown, no code fences, no explanation outside the JSON."
        )
        text = self.analyze_with_tools_and_thinking(
            model, json_system, user_prompt, tools, budget_tokens, max_tokens
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("json_parse_failed_thinking_tools", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:1000]}

    # --- V2: Tool Use (web_search) ---

    def analyze_with_tools(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: list = None,
        max_tokens: int = 8192,
        temperature: float = 0.3,
        max_tool_rounds: int = 15,
    ) -> str:
        """
        Completion with tool use (web_search_20250305).
        Handles multi-turn loop where model calls tools, gets results, continues.
        The web_search tool is server-side — Anthropic fetches results automatically.
        Returns final text response after all tool calls resolved.
        """
        if tools is None:
            tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]

        messages = [{"role": "user", "content": user_prompt}]
        log.info("claude_tool_call_start", model=model, prompt_len=len(user_prompt))

        response = None
        for round_num in range(max_tool_rounds):
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            log.info(
                "claude_tool_round",
                model=model,
                round=round_num + 1,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

            # If model is done (no more tool calls), extract final text
            if response.stop_reason == "end_turn":
                text_parts = []
                for block in response.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                return "\n".join(text_parts)

            # Model wants to use tools — append assistant response
            # For web_search_20250305, the server handles search execution
            # and includes results in the response content blocks.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                # Server-side tools (web_search) have results embedded.
                # Continue the conversation so the model can process results.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Continue your analysis with the search results."}
                ]})

        # Exhausted rounds — return whatever we have
        log.warning("tool_rounds_exhausted", model=model, max_rounds=max_tool_rounds)
        if response:
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            return "\n".join(text_parts) if text_parts else ""
        return ""

    def analyze_with_tools_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: list = None,
        max_tokens: int = 8192,
    ) -> dict:
        """Tool-use completion that parses final response as JSON."""
        json_system = (
            system_prompt
            + "\n\nIMPORTANT: After completing your research, respond ONLY with valid JSON. "
            "No markdown, no code fences, no explanation outside the JSON."
        )
        text = self.analyze_with_tools(
            model, json_system, user_prompt, tools, max_tokens, temperature=0.2
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        # Try to find JSON in the response (sometimes model adds preamble before JSON)
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("json_parse_failed_tools", raw_text=text[:500])
            return {"error": "Failed to parse JSON response", "raw": text[:1000]}
