"""
Reddit Sentiment Agent — STUB for Phase 1.
Returns neutral score. Real PRAW integration ready in data/reddit_data.py.
"""

from agents.base_agent import BaseAgent, AgentOutput
from utils.logger import get_logger

log = get_logger("reddit_agent")


class RedditSentimentAgent(BaseAgent):
    agent_type = "sentiment"

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Stub: returns neutral sentiment."""
        log.info("reddit_stub", ticker=ticker)
        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=0.5,
            confidence=0.2,
            direction="neutral",
            reasoning="Reddit sentiment scanning initializing. Data will be available after first daily post-market scan.",
            raw_data={
                "mention_volume": "unknown",
                "sentiment": "neutral",
                "sentiment_shift": "stable",
                "contrarian_flag": False,
                "status": "stub",
            },
            run_id=self.run_id,
        )
