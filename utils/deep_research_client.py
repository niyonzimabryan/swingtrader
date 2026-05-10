"""
Deep Research Client — abstraction over Gemini Deep Research (Interactions API).
Uses the most advanced mode: deep-research-pro-preview-12-2025.
Async background execution: submits research task, polls for completion.

Provider: Gemini (via google-genai SDK)
Alternative: OpenAI o4-mini-deep-research (not implemented yet)
"""

import asyncio
import time
from datetime import datetime
from utils.logger import get_logger

log = get_logger("deep_research_client")

# Most advanced deep research agent available
GEMINI_DEEP_RESEARCH_AGENT = "deep-research-pro-preview-12-2025"
DEFAULT_POLL_INTERVAL = 15  # seconds between polls
DEFAULT_TIMEOUT = 1800  # 30 minutes max
MAX_CONSECUTIVE_POLL_ERRORS = 5  # Fail-fast threshold for persistent polling exceptions


class DeepResearchClient:
    """
    Abstraction for async deep research APIs.
    Currently supports: "gemini" (Interactions API with deep-research-pro).
    Future: "openai" (o4-mini-deep-research via Responses API).
    """

    def __init__(self, provider: str = "gemini", api_key: str = "", settings=None):
        self.provider = provider
        self.api_key = api_key
        self.settings = settings
        self._client = None

        if provider == "gemini" and api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                log.info("gemini_deep_research_initialized")
            except ImportError:
                log.warning("google-genai package not installed. Run: pip install google-genai")
            except Exception as e:
                log.error("gemini_init_failed", error=str(e))

    @property
    def is_available(self) -> bool:
        """Check if deep research is configured and available."""
        return self._client is not None

    async def research(self, prompt: str, context: str = "") -> dict:
        """
        Submit a deep research task. Returns task metadata immediately.
        The actual research runs in background for 5-20+ minutes.

        Returns:
            {
                "task_id": str,
                "status": "submitted" | "error",
                "submitted_at": str (ISO),
                "provider": str,
                "error": str (if failed),
            }
        """
        if self.provider == "gemini":
            return await self._gemini_submit(prompt, context)
        else:
            return {"task_id": "", "status": "error", "error": f"Unsupported provider: {self.provider}"}

    async def poll_result(
        self,
        task_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        """
        Poll for deep research completion. Blocks until done or timeout.

        Returns:
            {
                "task_id": str,
                "status": "completed" | "failed" | "timeout",
                "report": str (research report text, if completed),
                "duration_s": float,
                "error": str (if failed/timeout),
            }
        """
        if self.provider == "gemini":
            return await self._gemini_poll(task_id, poll_interval, timeout)
        else:
            return {"task_id": task_id, "status": "failed", "error": f"Unsupported provider: {self.provider}"}

    async def research_and_wait(
        self,
        prompt: str,
        context: str = "",
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        """
        Convenience: submit + poll in one call. Returns full result.
        """
        submit_result = await self.research(prompt, context)
        if submit_result["status"] != "submitted":
            return submit_result

        task_id = submit_result["task_id"]
        return await self.poll_result(task_id, poll_interval, timeout)

    # --- Gemini Implementation ---

    async def _gemini_submit(self, prompt: str, context: str = "") -> dict:
        """Submit research task via Gemini Interactions API."""
        if not self._client:
            return {"task_id": "", "status": "error", "error": "Gemini client not initialized (missing API key?)"}

        try:
            full_prompt = prompt
            if context:
                full_prompt = f"{context}\n\n{prompt}"

            # Run sync API call in executor to not block event loop
            loop = asyncio.get_event_loop()
            interaction = await loop.run_in_executor(
                None,
                lambda: self._client.interactions.create(
                    input=full_prompt,
                    agent=GEMINI_DEEP_RESEARCH_AGENT,
                    background=True,
                ),
            )

            task_id = interaction.id
            log.info("deep_research_submitted", task_id=task_id, provider="gemini")

            return {
                "task_id": task_id,
                "status": "submitted",
                "submitted_at": datetime.utcnow().isoformat(),
                "provider": "gemini",
            }

        except Exception as e:
            log.error("deep_research_submit_failed", error=str(e))
            return {"task_id": "", "status": "error", "error": str(e)[:500]}

    async def _gemini_poll(self, task_id: str, poll_interval: int, timeout: int) -> dict:
        """Poll Gemini Interactions API for research completion."""
        if not self._client:
            return {"task_id": task_id, "status": "failed", "error": "Gemini client not initialized"}

        start = time.time()
        loop = asyncio.get_event_loop()
        consecutive_errors = 0
        last_error: str = ""

        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                log.warning("deep_research_timeout", task_id=task_id, elapsed=elapsed)
                return {
                    "task_id": task_id,
                    "status": "timeout",
                    "duration_s": elapsed,
                    "error": f"Research timed out after {timeout}s",
                }

            try:
                interaction = await loop.run_in_executor(
                    None,
                    lambda: self._client.interactions.get(task_id),
                )
                consecutive_errors = 0

                if interaction.status == "completed":
                    report = ""
                    if interaction.outputs:
                        report = interaction.outputs[-1].text

                    duration = time.time() - start
                    log.info(
                        "deep_research_completed",
                        task_id=task_id,
                        duration_s=round(duration, 1),
                        report_len=len(report),
                    )
                    return {
                        "task_id": task_id,
                        "status": "completed",
                        "report": report,
                        "duration_s": round(duration, 1),
                    }

                elif interaction.status == "failed":
                    error = str(getattr(interaction, "error", "Unknown error"))
                    log.error("deep_research_failed", task_id=task_id, error=error)
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "duration_s": time.time() - start,
                        "error": error,
                    }

                else:
                    # Still in progress
                    log.debug("deep_research_polling", task_id=task_id, elapsed=round(elapsed, 0))

            except Exception as e:
                consecutive_errors += 1
                last_error = str(e)
                log.error(
                    "deep_research_poll_error",
                    task_id=task_id,
                    error=last_error,
                    consecutive_errors=consecutive_errors,
                )
                if consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    duration = time.time() - start
                    log.error(
                        "deep_research_poll_fast_fail",
                        task_id=task_id,
                        consecutive_errors=consecutive_errors,
                        duration_s=round(duration, 1),
                    )
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "duration_s": round(duration, 1),
                        "error": (
                            f"{MAX_CONSECUTIVE_POLL_ERRORS} consecutive poll errors; "
                            f"last error: {last_error[:300]}"
                        ),
                    }

            await asyncio.sleep(poll_interval)
