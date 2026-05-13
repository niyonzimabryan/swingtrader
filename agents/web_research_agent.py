"""
Web Research Agent — replaces Reddit sentiment stub.
Conducts deep web research on catalyst-flagged tickers using the configured
grounded search provider.
Researches 5 dimensions: catalyst context, competitive dynamics, management signals,
bull/bear debate, and institutional positioning.
"""

import json
from datetime import datetime
from agents.base_agent import BaseAgent, AgentOutput
from utils.web_search_client import WebSearchClient
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("web_research_agent")


SCRAPED_BODY_MAX_CHARS = 4000
SCRAPED_BLOCK_BUDGET_CHARS = 12000


class WebResearchAgent(BaseAgent):
    agent_type = "web_research"

    def __init__(
        self, settings, anthropic_client=None,
        web_search_client: WebSearchClient = None,
        firecrawl_client=None,
    ):
        super().__init__(settings, anthropic_client)
        self.web_search_client = web_search_client
        self.firecrawl_client = firecrawl_client

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """
        Conduct web research on a ticker to enrich catalyst analysis.

        kwargs:
        - sector (str): Ticker sector
        - catalyst_data (dict): Raw catalyst data from CatalystAgent
        - catalyst_reasoning (str): Catalyst reasoning text
        - direction_hint (str): Expected direction from catalyst
        """
        if not ticker:
            return AgentOutput(agent_type=self.agent_type, reasoning="No ticker provided")

        if not self.web_search_client:
            return self._stub_output(ticker)

        log.info("web_research_start", ticker=ticker)

        sector = kwargs.get("sector", "Unknown")
        catalyst_data = kwargs.get("catalyst_data", {})
        catalyst_reasoning = kwargs.get("catalyst_reasoning", "")
        direction_hint = kwargs.get("direction_hint", "neutral")

        scraped_sources = self._scrape_supplemental_sources(ticker, catalyst_data)

        try:
            result = self._run_research(
                ticker, sector, catalyst_data, catalyst_reasoning, direction_hint,
                scraped_sources=scraped_sources,
            )
            return self._build_output(ticker, result, scraped_sources=scraped_sources)
        except Exception as e:
            log.error("web_research_failed", ticker=ticker, error=str(e))
            return self._fallback_output(ticker, str(e))

    def _scrape_supplemental_sources(self, ticker: str, catalyst_data: dict) -> list[dict]:
        """Scrape catalyst-supplied URLs through Firecrawl for the LLM prompt.

        Returns a list of {url, markdown} dicts. Empty list when Firecrawl is
        not configured or no URLs were supplied — the agent then falls back to
        grounded LLM search alone, which is still functional.
        """
        if not self.firecrawl_client or not getattr(self.firecrawl_client, "is_available", False):
            return []

        urls = self._collect_source_urls(catalyst_data)
        if not urls:
            return []

        scraped = []
        budget = SCRAPED_BLOCK_BUDGET_CHARS
        for url in urls:
            if budget <= 0:
                break
            markdown = self.firecrawl_client.scrape(url)
            if not markdown:
                continue
            trimmed = markdown[:SCRAPED_BODY_MAX_CHARS]
            scraped.append({"url": url, "markdown": trimmed})
            budget -= len(trimmed)

        if scraped:
            log.info("web_research_scraped", ticker=ticker, count=len(scraped))
        return scraped

    @staticmethod
    def _collect_source_urls(catalyst_data: dict) -> list[str]:
        if not isinstance(catalyst_data, dict):
            return []
        urls: list[str] = []
        primary = catalyst_data.get("source")
        if isinstance(primary, str) and primary.startswith("http"):
            urls.append(primary)
        extra = catalyst_data.get("source_urls")
        if isinstance(extra, list):
            for u in extra:
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
        return urls[:5]

    def _run_research(
        self, ticker: str, sector: str,
        catalyst_data: dict, catalyst_reasoning: str,
        direction_hint: str,
        scraped_sources: list[dict] | None = None,
    ) -> dict:
        """Run multi-dimensional grounded web research."""
        if getattr(self.settings, "web_search_provider", "anthropic") == "gemini":
            model = getattr(self.settings, "gemini_web_research_model", "gemini-3.1-pro-preview")
        else:
            model = get_model("web_research", self.settings)
        max_searches = max(1, int(getattr(self.settings, "web_research_max_searches", 8)))

        catalyst_summary = catalyst_data.get("catalyst_summary", "")
        catalyst_type = catalyst_data.get("catalyst_type", "")

        system_prompt = (
            "You are a senior equity research analyst conducting deep web research on a stock "
            "that has been flagged with a potential catalyst. Your research will inform a swing trade "
            "decision (1-20 trading day holding period).\n\n"
            "Use grounded web search before answering. Scrutinize the setup like a skeptical PM: verify the catalyst, "
            "look for disconfirming evidence, check whether the market already priced it in, and identify what would "
            "invalidate the trade.\n\n"
            "Search thoroughly across ALL five dimensions listed. Prefer primary sources and recent reputable reporting. "
            "Be specific, include source URLs in JSON fields where useful, and say explicitly when evidence is sparse."
        )

        scraped_block = ""
        if scraped_sources:
            chunks = []
            for src in scraped_sources:
                url = src.get("url", "")
                md = src.get("markdown", "")
                if not md:
                    continue
                chunks.append(f"Source: {url}\n{md}")
            if chunks:
                scraped_block = (
                    "PRE-FETCHED SOURCES (Firecrawl scrape, treat as primary evidence "
                    "but verify recency with your search tools):\n"
                    + "\n\n---\n\n".join(chunks) + "\n\n"
                )

        user_prompt = (
            f"Research {ticker} ({sector}) for a potential swing trade.\n\n"
            f"CATALYST CONTEXT:\n"
            f"Type: {catalyst_type}\n"
            f"Summary: {catalyst_summary}\n"
            f"Direction hint: {direction_hint}\n"
            f"Full reasoning: {catalyst_reasoning[:500]}\n\n"
            f"{scraped_block}"
            "Research the following 5 dimensions and respond with JSON:\n\n"
            "1. CATALYST CONTEXT — What are analysts expecting? Any whisper numbers? "
            "Key metrics to watch? How does this compare to prior quarters?\n\n"
            "2. COMPETITIVE DYNAMICS — What are peers doing? Market share shifts? "
            "Supply chain signals? Any industry-wide tailwinds/headwinds?\n\n"
            "3. MANAGEMENT SIGNALS — Recent conference remarks, tone shifts, insider activity "
            "(buys/sells in last 90 days), executive changes.\n\n"
            "4. BULL/BEAR DEBATE — What are the strongest arguments on each side? "
            "What does consensus miss? Any contrarian signals?\n\n"
            "5. INSTITUTIONAL POSITIONING — Recent 13F changes, notable fund commentary, "
            "short interest trends, options flow if available.\n\n"
            "Respond with this JSON structure:\n"
            "{\n"
            '  "synthesis": "3-5 sentence overall assessment of the information environment",\n'
            '  "catalyst_context": "2-3 sentences on analyst expectations and key metrics",\n'
            '  "competitive_dynamics": "2-3 sentences on peer/industry signals",\n'
            '  "management_signals": "2-3 sentences on insider/management activity",\n'
            '  "bull_bear_debate": "2-3 sentences on key arguments each side",\n'
            '  "institutional_positioning": "2-3 sentences on fund/institutional moves",\n'
            '  "information_score": 0.0-1.0,\n'
            '  "confidence": 0.0-1.0,\n'
            '  "direction": "bullish|bearish|neutral",\n'
            '  "key_finding": "single most important finding in one sentence",\n'
            '  "sources_summary": "brief description of sources found, including source URLs when available",\n'
            '  "source_urls": ["https://source-1.example", "https://source-2.example"]\n'
            "}\n\n"
            "SCORING GUIDANCE:\n"
            "- information_score: How favorable is the information environment for a trade?\n"
            "  0.8+ = strongly supportive (consensus, positioning, mgmt all align)\n"
            "  0.5-0.7 = mixed but leans one way\n"
            "  <0.5 = unfavorable or contradictory signals\n"
            "- confidence: How much data were you able to find?\n"
            "  0.8+ = rich data across most dimensions\n"
            "  0.5-0.7 = decent coverage but some gaps\n"
            "  <0.5 = sparse data, low conviction in assessment"
        )

        result = self.web_search_client.search_and_analyze_json(
            system_prompt, user_prompt, model=model, max_searches=max_searches, max_tokens=6144
        )

        log.info(
            "web_research_complete",
            ticker=ticker,
            information_score=result.get("information_score"),
            confidence=result.get("confidence"),
            direction=result.get("direction"),
        )

        return result

    def _build_output(
        self, ticker: str, result: dict, scraped_sources: list[dict] | None = None,
    ) -> AgentOutput:
        """Convert web research result to AgentOutput."""
        info_score = result.get("information_score", 0.5)
        confidence = result.get("confidence", 0.5)
        direction = result.get("direction", "neutral")

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=info_score,
            confidence=confidence,
            direction=direction,
            reasoning=result.get("synthesis", ""),
            raw_data={
                "catalyst_context": result.get("catalyst_context", ""),
                "competitive_dynamics": result.get("competitive_dynamics", ""),
                "management_signals": result.get("management_signals", ""),
                "bull_bear_debate": result.get("bull_bear_debate", ""),
                "institutional_positioning": result.get("institutional_positioning", ""),
                "key_finding": result.get("key_finding", ""),
                "sources_summary": result.get("sources_summary", ""),
                "source_urls": result.get("source_urls", []),
                "grounding": result.get("_grounding", {}),
                "scraped_source_urls": [s.get("url", "") for s in (scraped_sources or [])],
                "status": "active",
            },
            run_id=self.run_id,
        )

    def _stub_output(self, ticker: str) -> AgentOutput:
        """Return stub output when web search is not configured."""
        log.info("web_research_stub", ticker=ticker)
        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=0.5,
            confidence=0.2,
            direction="neutral",
            reasoning="Web research not available — web search client not configured.",
            raw_data={
                "catalyst_context": "",
                "competitive_dynamics": "",
                "management_signals": "",
                "bull_bear_debate": "",
                "institutional_positioning": "",
                "key_finding": "",
                "sources_summary": "",
                "status": "stub",
            },
            run_id=self.run_id,
        )

    def _fallback_output(self, ticker: str, error: str) -> AgentOutput:
        """Return fallback output on error."""
        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=0.5,
            confidence=0.1,
            direction="neutral",
            reasoning=f"Web research failed: {error[:200]}",
            raw_data={
                "status": "error",
                "error": error[:500],
            },
            run_id=self.run_id,
        )
