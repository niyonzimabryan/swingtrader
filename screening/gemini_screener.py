"""
Gemini Flash Tier 2 Screener — web research + preliminary scoring.
Takes Tier 1 flagged tickers, uses Gemini 2.0 Flash with Google Search
grounding to produce a research brief + score for each.

Only tickers scoring above the escalation threshold proceed to Sonnet (Tier 3).
Cost: ~$0.0005/ticker (effectively free on Gemini free tier).
"""

import json
import time
from dataclasses import dataclass, field
from utils.logger import get_logger

log = get_logger("gemini_screener")


@dataclass
class GeminiScreenResult:
    """Result for a single ticker from Gemini Flash screening."""
    ticker: str
    score: float = 0.0          # 0.0 - 1.0 preliminary score
    direction: str = "neutral"   # bullish / bearish / neutral
    summary: str = ""            # 2-3 sentence research brief
    catalysts: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    escalate: bool = False       # True if score >= threshold


@dataclass
class GeminiBatchResult:
    """Result from screening a batch of tickers."""
    results: list = field(default_factory=list)  # list[GeminiScreenResult]
    escalated: list = field(default_factory=list)  # tickers that passed threshold
    total_screened: int = 0
    duration_s: float = 0.0
    errors: list = field(default_factory=list)


SCREENING_PROMPT = """You are a swing trading screener. For the given stock ticker, research it using web search and produce a brief analysis.

TICKER: {ticker}
KNOWN CATALYSTS: {catalyst_context}

Respond in this exact JSON format (no markdown, no code fences):
{{
  "score": <float 0.0 to 1.0>,
  "direction": "<bullish|bearish|neutral>",
  "summary": "<2-3 sentence research brief on current setup>",
  "catalysts": ["<catalyst 1>", "<catalyst 2>"],
  "risks": ["<risk 1>", "<risk 2>"]
}}

Scoring guide:
- 0.0-0.3: No actionable setup, noise
- 0.3-0.5: Interesting but not compelling
- 0.5-0.7: Solid setup with clear catalyst
- 0.7-1.0: Strong conviction, multiple confirming signals

Focus on: recent earnings, analyst actions, insider activity, sector momentum, technical breakouts/breakdowns, upcoming catalysts. Be honest — most tickers should score 0.2-0.4."""


class GeminiScreener:
    """Gemini 2.0 Flash screening client with Google Search grounding."""

    def __init__(self, settings):
        self.settings = settings
        self._client = None
        self._model = settings.gemini_flash_model
        self._threshold = settings.gemini_flash_escalation_threshold

        if settings.gemini_api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=settings.gemini_api_key)
                log.info("gemini_screener_initialized", model=self._model)
            except ImportError:
                log.warning("google-genai not installed. Run: pip install google-genai")
            except Exception as e:
                log.error("gemini_screener_init_failed", error=str(e))

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def screen_batch(self, flagged_tickers: list) -> GeminiBatchResult:
        """
        Screen a batch of flagged tickers through Gemini Flash.
        Each ticker gets a web-grounded research call.

        Args:
            flagged_tickers: list of dicts with keys: symbol, catalyst_context
        Returns:
            GeminiBatchResult with per-ticker scores and escalation decisions.
        """
        if not self.is_available:
            log.warning("gemini_screener_not_available")
            return GeminiBatchResult()

        start = time.time()
        results = []
        errors = []

        for item in flagged_tickers:
            ticker = item["symbol"]
            catalyst_context = item.get("catalyst_context", "None provided")

            try:
                result = self._screen_single(ticker, catalyst_context)
                results.append(result)
            except Exception as e:
                log.error("gemini_screen_failed", ticker=ticker, error=str(e))
                errors.append(f"{ticker}: {str(e)[:100]}")
                # Don't escalate on error — just skip
                results.append(GeminiScreenResult(ticker=ticker, score=0, summary=f"Screening failed: {str(e)[:100]}"))

        escalated = [r.ticker for r in results if r.escalate]
        duration = time.time() - start

        log.info(
            "gemini_batch_complete",
            total=len(flagged_tickers),
            escalated=len(escalated),
            duration_s=round(duration, 1),
            errors=len(errors),
        )

        return GeminiBatchResult(
            results=results,
            escalated=escalated,
            total_screened=len(flagged_tickers),
            duration_s=round(duration, 1),
            errors=errors,
        )

    def _screen_single(self, ticker: str, catalyst_context: str) -> GeminiScreenResult:
        """Screen a single ticker with Gemini Flash + Google Search grounding."""
        from google.genai import types

        prompt = SCREENING_PROMPT.format(
            ticker=ticker,
            catalyst_context=catalyst_context,
        )

        # Use Google Search as grounding tool
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.3,
                max_output_tokens=512,
            ),
        )

        # Parse response
        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("gemini_json_parse_failed", ticker=ticker, raw=text[:200])
            # Attempt to extract JSON from mixed content
            data = self._extract_json(text)
            if not data:
                return GeminiScreenResult(
                    ticker=ticker,
                    score=0.0,
                    summary=f"Parse failed: {text[:100]}",
                )

        score = float(data.get("score", 0))
        score = max(0.0, min(1.0, score))  # Clamp

        result = GeminiScreenResult(
            ticker=ticker,
            score=score,
            direction=data.get("direction", "neutral"),
            summary=data.get("summary", ""),
            catalysts=data.get("catalysts", []),
            risks=data.get("risks", []),
            escalate=score >= self._threshold,
        )

        log.info(
            "gemini_screen_result",
            ticker=ticker,
            score=score,
            direction=result.direction,
            escalate=result.escalate,
        )

        return result

    def _extract_json(self, text: str) -> dict | None:
        """Try to extract JSON from text that may contain non-JSON content."""
        # Find first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None
