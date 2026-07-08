"""
Gemini Flash Tier 2 Screener — web research + preliminary scoring.
Takes Tier 1 flagged tickers, uses Gemini 2.0 Flash with Google Search
grounding to produce a research brief + score for each.

Only tickers scoring above the escalation threshold proceed to Sonnet (Tier 3).
Cost: ~$0.0005/ticker (effectively free on Gemini free tier).

Output handling: Google Search grounding is mutually exclusive with the
structured-output path (response_mime_type=application/json / response_schema)
— the API returns 400 "Tool use with a response mime type ... is unsupported".
Since grounding IS the point of tier-2, we keep grounding and instead give the
model a large token budget (grounded replies prepend a prose preamble before
the JSON) plus robust extraction of the first balanced {...} block, and detect
truncation via the MAX_TOKENS finish reason rather than a structured schema.
"""

import json
import time
import warnings
from dataclasses import dataclass, field
from utils.logger import get_logger

log = get_logger("gemini_screener")

# SDK enum drift: newer server-side FinishReason values (e.g. TOO_MANY_TOOL_CALLS)
# aren't in the installed google-genai enum, which warns once per response during
# deserialization. Silence that specific noise so it doesn't fire per ticker.
warnings.filterwarnings("ignore", message=r".*is not a valid FinishReason.*")

# Emit gemini_screen_degraded when parse_failed / attempted exceeds this (spec C3).
PARSE_FAIL_WARN_RATIO = 0.10


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
    # Per-scan parse-health counters (spec C3).
    # attempted == parsed + parse_failed + truncated + empty + len(errors)
    attempted: int = 0
    parsed: int = 0
    parse_failed: int = 0
    truncated: int = 0
    empty: int = 0
    parse_fail_rate: float = 0.0   # parse_failed / attempted
    degraded: bool = False         # parse_fail_rate > PARSE_FAIL_WARN_RATIO


SCREENING_PROMPT = """You are a swing trading screener. For the given stock ticker, research it using web search and produce a brief analysis.

TICKER: {ticker}
KNOWN CATALYSTS: {catalyst_context}

Output ONLY a single JSON object and nothing else — no preamble, no markdown, no code fences, no text before or after it. Use exactly this shape:
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
        self._max_output_tokens = getattr(settings, "gemini_flash_max_output_tokens", 4096)

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
        counts = {"parsed": 0, "parse_failed": 0, "truncated": 0, "empty": 0}

        for item in flagged_tickers:
            ticker = item["symbol"]
            catalyst_context = item.get("catalyst_context", "None provided")

            try:
                result, outcome = self._screen_single(ticker, catalyst_context)
                results.append(result)
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as e:
                log.error("gemini_screen_failed", ticker=ticker, error=str(e))
                errors.append(f"{ticker}: {str(e)[:100]}")
                # Don't escalate on error — just skip
                results.append(GeminiScreenResult(ticker=ticker, score=0, summary=f"Screening failed: {str(e)[:100]}"))

        escalated = [r.ticker for r in results if r.escalate]
        duration = time.time() - start

        attempted = len(flagged_tickers)
        parse_fail_rate = counts["parse_failed"] / attempted if attempted else 0.0
        degraded = attempted > 0 and parse_fail_rate > PARSE_FAIL_WARN_RATIO

        log.info(
            "gemini_batch_complete",
            total=attempted,
            escalated=len(escalated),
            duration_s=round(duration, 1),
            errors=len(errors),
        )
        # Single per-scan summary event (spec C3).
        log.info(
            "gemini_screen_summary",
            attempted=attempted,
            parsed=counts["parsed"],
            parse_failed=counts["parse_failed"],
            truncated=counts["truncated"],
            empty=counts["empty"],
            call_errors=len(errors),
            parse_fail_rate=round(parse_fail_rate, 3),
        )
        if degraded:
            # WARNING so it surfaces in ops dashboards. Telegram alerting can ride
            # Spec B's notifier once merged; log-only until then (spec C3).
            log.warning(
                "gemini_screen_degraded",
                attempted=attempted,
                parsed=counts["parsed"],
                parse_failed=counts["parse_failed"],
                truncated=counts["truncated"],
                empty=counts["empty"],
                call_errors=len(errors),
                parse_fail_rate=round(parse_fail_rate, 3),
                threshold=PARSE_FAIL_WARN_RATIO,
            )

        return GeminiBatchResult(
            results=results,
            escalated=escalated,
            total_screened=attempted,
            duration_s=round(duration, 1),
            errors=errors,
            attempted=attempted,
            parsed=counts["parsed"],
            parse_failed=counts["parse_failed"],
            truncated=counts["truncated"],
            empty=counts["empty"],
            parse_fail_rate=round(parse_fail_rate, 3),
            degraded=degraded,
        )

    def _screen_single(self, ticker: str, catalyst_context: str) -> tuple:
        """
        Screen a single ticker with Gemini Flash + Google Search grounding.

        Returns (GeminiScreenResult, outcome) where outcome is one of
        "parsed" | "parse_failed" | "truncated" | "empty".
        """
        from google.genai import types

        prompt = SCREENING_PROMPT.format(
            ticker=ticker,
            catalyst_context=catalyst_context,
        )

        # Use Google Search as grounding tool. Grounding cannot be combined with a
        # JSON response schema (API 400), so budget generously + extract robustly.
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.3,
                max_output_tokens=self._max_output_tokens,
            ),
        )

        finish_reason = self._finish_reason(response)
        text = self._response_text(response)

        # Guard empty / safety-blocked / no-candidate responses (was: NoneType.strip()).
        if not text:
            log.warning("gemini_screen_failed", ticker=ticker, reason="empty_response", finish_reason=finish_reason)
            return (
                GeminiScreenResult(ticker=ticker, score=0.0, summary=f"Empty response (finish={finish_reason})"),
                "empty",
            )

        data = self._extract_json(text)
        if data is None:
            # Distinguish truncation (budget too small) from a genuine parse failure.
            if finish_reason == "MAX_TOKENS":
                log.warning("gemini_screen_truncated", ticker=ticker, finish_reason=finish_reason, chars=len(text))
                return (
                    GeminiScreenResult(ticker=ticker, score=0.0, summary="Response truncated (raise max_output_tokens)"),
                    "truncated",
                )
            log.warning("gemini_json_parse_failed", ticker=ticker, finish_reason=finish_reason, raw=text[:200])
            return (
                GeminiScreenResult(ticker=ticker, score=0.0, summary=f"Parse failed: {text[:100]}"),
                "parse_failed",
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

        return result, "parsed"

    def _response_text(self, response) -> str:
        """Safely pull text from a response that may be None / empty / safety-blocked."""
        try:
            text = response.text
        except Exception:
            # google-genai raises when there are no text parts (safety block, tool-only turn).
            text = None
        return text.strip() if text else ""

    def _finish_reason(self, response) -> str:
        """Read the finish reason exactly once and coerce it to a plain string.

        Tolerates missing candidates and unknown SDK enum values so a single
        ticker can't raise or spam warnings.
        """
        try:
            fr = response.candidates[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            return "UNKNOWN"
        if fr is None:
            return "UNKNOWN"
        return getattr(fr, "name", None) or str(fr)

    def _extract_json(self, text: str) -> dict | None:
        """Extract and parse the first balanced {...} block from possibly prose-wrapped text.

        Grounded responses often prepend a prose preamble (and occasionally trailing
        commentary), so scan for the first complete top-level object rather than
        json.loads-ing the whole string. Returns None on no/broken/truncated JSON.
        """
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        return None
