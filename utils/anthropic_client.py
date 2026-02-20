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
