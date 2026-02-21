"""
Deep Research Agent — triggers extended async research for high-conviction ideas.
Uses Gemini Deep Research (Interactions API) via DeepResearchClient.

Flow:
1. Memo generated → sent to Telegram immediately
2. If Opus score >= threshold → this agent fires async
3. Sends "🔬 Deep research generating..." notification
4. Gemini runs 5-20 min of autonomous research
5. Report returns → Opus re-evaluates with new data
6. If recommendation changed → notify operator with updated verdict
7. PDF generated and sent via Telegram

Does NOT run on /test (avoids wait time). Operator can trigger manually via Telegram button.
"""

import json
from datetime import datetime
from database.db import get_session
from database.models import DeepResearchRequest
from utils.deep_research_client import DeepResearchClient
from utils.escalation_manager import EscalationManager
from utils.logger import get_logger

log = get_logger("deep_research_agent")


class DeepResearchAgent:
    """Orchestrates deep research + Opus re-evaluation for high-conviction trades."""

    def __init__(
        self,
        settings,
        deep_research_client: DeepResearchClient,
        escalation_manager: EscalationManager,
    ):
        self.settings = settings
        self.dr_client = deep_research_client
        self.escalation = escalation_manager
        self.score_threshold = getattr(settings, "deep_research_score_threshold", 0.75)

    def should_trigger(self, scoring_result: dict, is_ad_hoc: bool = False) -> bool:
        """
        Determine if deep research should trigger for this opportunity.
        Only triggers on scheduled scans, not /test (unless explicitly requested).
        """
        if is_ad_hoc:
            return False  # Never auto-trigger on /test

        if not self.dr_client or not self.dr_client.is_available:
            return False

        final_score = scoring_result.get("final_score", 0)
        return final_score >= self.score_threshold

    async def run(
        self,
        ticker: str,
        memo_id: int,
        scoring_result: dict,
        catalyst_reasoning: str,
        web_research_reasoning: str,
        notification_callback=None,
    ) -> dict:
        """
        Run async deep research pipeline:
        1. Submit research task
        2. Save request to DB
        3. Poll for completion
        4. Opus re-evaluates
        5. Notify operator if recommendation changed
        6. Generate PDF

        Returns dict with deep research results.
        """
        log.info("deep_research_start", ticker=ticker, memo_id=memo_id)

        original_score = scoring_result.get("final_score", 0)
        original_eval = scoring_result.get("opus_evaluation", {})

        # 1. Build research prompt
        research_prompt = self._build_research_prompt(
            ticker, scoring_result, catalyst_reasoning, web_research_reasoning
        )

        # 2. Notify operator
        if notification_callback:
            await notification_callback(
                f"🔬 Deep research generating for {ticker}... "
                f"(score: {original_score:.2f}, est. 5-20 min)"
            )

        # 3. Submit to Gemini
        submit_result = await self.dr_client.research(research_prompt)

        if submit_result["status"] != "submitted":
            log.error("deep_research_submit_failed", ticker=ticker, error=submit_result.get("error"))
            return {"status": "failed", "error": submit_result.get("error")}

        task_id = submit_result["task_id"]

        # 4. Save to DB
        dr_request_id = self._save_request(
            memo_id=memo_id,
            ticker=ticker,
            task_id=task_id,
            provider=submit_result.get("provider", "gemini"),
            original_score=original_score,
        )

        # 5. Poll for completion
        poll_result = await self.dr_client.poll_result(task_id)

        if poll_result["status"] != "completed":
            self._update_request(dr_request_id, status=poll_result["status"],
                                  error=poll_result.get("error", ""),
                                  duration_s=poll_result.get("duration_s"))
            if notification_callback:
                await notification_callback(
                    f"⚠️ Deep research for {ticker} {poll_result['status']}: "
                    f"{poll_result.get('error', 'Unknown error')[:200]}"
                )
            return poll_result

        research_report = poll_result.get("report", "")
        duration_s = poll_result.get("duration_s", 0)

        log.info("deep_research_report_received", ticker=ticker, report_len=len(research_report), duration_s=duration_s)

        # 6. Opus re-evaluates with deep research
        reeval_result = {}
        recommendation_changed = False

        if self.escalation:
            reeval_result = self.escalation.opus_reevaluate(
                ticker, original_eval, research_report, original_score
            )
            recommendation_changed = reeval_result.get("recommendation_changed", False)

        # 7. Update DB
        self._update_request(
            dr_request_id,
            status="completed",
            research_report=research_report,
            reevaluation_result=json.dumps(reeval_result),
            updated_score=reeval_result.get("final_score"),
            updated_recommendation=reeval_result.get("recommendation"),
            duration_s=duration_s,
        )

        # 8. Notify operator
        if notification_callback:
            new_score = reeval_result.get("final_score", original_score)
            new_rec = reeval_result.get("recommendation", "?")
            old_rec = original_eval.get("recommendation", "?")
            key_insight = reeval_result.get("key_insight_from_research", "")

            if recommendation_changed:
                msg = (
                    f"🔬 *Deep Research Update: {ticker}*\n\n"
                    f"⚠️ Recommendation changed: {old_rec.upper()} → {new_rec.upper()}\n"
                    f"Score: {original_score:.2f} → {new_score:.2f}\n"
                    f"Key insight: {key_insight[:200]}\n"
                    f"Research time: {duration_s:.0f}s"
                )
            else:
                msg = (
                    f"🔬 *Deep Research Complete: {ticker}*\n\n"
                    f"✅ Recommendation confirmed: {new_rec.upper()}\n"
                    f"Score: {original_score:.2f} → {new_score:.2f}\n"
                    f"Key insight: {key_insight[:200]}\n"
                    f"Research time: {duration_s:.0f}s"
                )
            await notification_callback(msg)

        return {
            "status": "completed",
            "ticker": ticker,
            "task_id": task_id,
            "research_report": research_report,
            "reevaluation": reeval_result,
            "recommendation_changed": recommendation_changed,
            "duration_s": duration_s,
            "dr_request_id": dr_request_id,
        }

    def _build_research_prompt(
        self, ticker: str, scoring_result: dict,
        catalyst_reasoning: str, web_research_reasoning: str,
    ) -> str:
        """Build the prompt for deep research."""
        direction = scoring_result.get("direction", "bullish")
        score = scoring_result.get("final_score", 0)
        opus_eval = scoring_result.get("opus_evaluation", {})

        return (
            f"Conduct comprehensive deep research on {ticker} for a swing trade evaluation "
            f"(1-20 trading day holding period).\n\n"
            f"CURRENT ASSESSMENT:\n"
            f"Direction: {direction}\n"
            f"Composite score: {score:.2f}\n"
            f"Catalyst: {catalyst_reasoning[:500]}\n"
            f"Web research synthesis: {web_research_reasoning[:500]}\n"
            f"Key risk identified: {opus_eval.get('key_risk', 'N/A')}\n\n"
            "RESEARCH OBJECTIVES:\n"
            "1. VALIDATE OR CHALLENGE the catalyst — is it as significant as initial analysis suggests?\n"
            "2. COMPETITIVE LANDSCAPE — detailed peer analysis, market share trends, competitive moats\n"
            "3. INSTITUTIONAL SENTIMENT — recent 13F filings, hedge fund commentary, analyst consensus shifts\n"
            "4. MANAGEMENT CREDIBILITY — track record on guidance, recent insider transactions, executive changes\n"
            "5. VALUATION CONTEXT — current multiples vs. history, vs. peers, implied expectations\n"
            "6. RISK DEEP DIVE — what are the under-appreciated risks? Regulatory? Macro sensitivity?\n"
            "7. TECHNICAL/FLOW — unusual options activity, short interest changes, dark pool activity\n"
            "8. TIMELINE — are there upcoming catalysts (earnings, FDA dates, conferences) that affect timing?\n\n"
            "Provide a structured research report with clear sections, specific data points, and actionable conclusions. "
            "Explicitly state what strengthens or weakens the trade thesis."
        )

    def _save_request(self, memo_id: int, ticker: str, task_id: str,
                       provider: str, original_score: float) -> int:
        """Persist deep research request to DB."""
        try:
            with get_session() as session:
                req = DeepResearchRequest(
                    memo_id=memo_id,
                    ticker=ticker,
                    task_id=task_id,
                    provider=provider,
                    status="submitted",
                    original_score=original_score,
                )
                session.add(req)
                session.flush()
                return req.id
        except Exception as e:
            log.error("save_dr_request_failed", error=str(e))
            return 0

    def _update_request(self, dr_request_id: int, **kwargs):
        """Update deep research request in DB."""
        try:
            with get_session() as session:
                req = session.query(DeepResearchRequest).filter_by(id=dr_request_id).first()
                if req:
                    for k, v in kwargs.items():
                        if hasattr(req, k):
                            setattr(req, k, v)
                    if kwargs.get("status") in ("completed", "failed", "timeout"):
                        req.completed_at = datetime.utcnow()
        except Exception as e:
            log.error("update_dr_request_failed", error=str(e))
