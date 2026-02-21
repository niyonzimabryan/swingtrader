# SwingTrader v2 — Phase A Implementation Spec

> **Purpose:** Self-contained handoff doc for a fresh Claude Code session. Contains exact code patterns, current signatures, and implementation details so the implementing session doesn't need to re-read 15+ files.
>
> **Full plan:** See also `/Users/bryanniyonzima/.claude/plans/splendid-napping-ocean.md` for full 4-phase plan context.
>
> **Project root:** `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/`
> **GitHub:** https://github.com/niyonzimabryan/swingtrader (private, account: niyonzimabryan)

---

## Build Order (Phase A — dependency-ordered)

1. `config/settings.py` — Add new settings
2. `database/models.py` — Add DiscoveredTicker, WatchlistTicker tables
3. `utils/anthropic_client.py` — Add `analyze_with_tools()` for web_search
4. `utils/web_search_client.py` — NEW: WebSearchClient abstraction
5. `agents/discovery_agent.py` — NEW: Discovery Agent
6. `orchestrator/universe.py` — Add watchlist management functions
7. `config/tickers.py` — Expand to S&P 500
8. `scripts/update_sp500.py` — NEW: Fetch S&P 500 constituents
9. `utils/escalation_manager.py` — Update sonnet_analyze() prompt for materiality/direction
10. `agents/catalyst_agent.py` — Materiality/direction split + skip_haiku support
11. `orchestrator/pipeline.py` — Refactor run_full_scan() with source-aware routing
12. `bot/handlers/callbacks.py` — Wire watchlist button

---

## 1. config/settings.py — Add New Settings

**Current file:** Has Settings class with BaseSettings, all existing keys work.

**Add these fields** (after the existing `filter_model` line ~57):

```python
    # --- V2: Web Search & Discovery ---
    web_search_provider: str = "anthropic"  # "anthropic" (default)
    discovery_max_tickers: int = 12
    discovery_model: str = "claude-sonnet-4-6"  # Discovery uses Sonnet, NOT Haiku

    # --- V2: Deep Research (Phase C — empty for now) ---
    gemini_api_key: str = ""
    openai_api_key: str = ""
    deep_research_provider: str = "gemini"  # "gemini" or "openai"
    deep_research_score_threshold: float = 0.75

    # --- V2: Watchlist ---
    watchlist_haiku_threshold: int = 2  # Lower bar for watchlist tickers
    watchlist_max_size: int = 25
    watchlist_expiry_days: int = 30
```

---

## 2. database/models.py — Add Tables

**Current file:** Has 10 tables, uses SQLAlchemy `declarative_base()`. Import pattern: `from sqlalchemy import Column, Integer, String, Float, Boolean, Text, DateTime, Date, ForeignKey...`

**Add after RedditSentiment class (end of file):**

```python
class DiscoveredTicker(Base):
    __tablename__ = "discovered_tickers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False, index=True)
    catalyst_summary = Column(Text, default="")
    catalyst_type = Column(String(50), default="")
    relevance_score = Column(Float, default=0)
    direction_hint = Column(String(20), default="neutral")  # bullish, bearish, neutral
    discovery_context = Column(Text, default="")  # Full context from Discovery Agent
    model_used = Column(String(50), default="")
    run_id = Column(String(50), default="")
    progressed_to_pipeline = Column(Boolean, default=False)
    pipeline_score = Column(Float, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)


class WatchlistTicker(Base):
    __tablename__ = "watchlist_tickers"
    __table_args__ = (
        Index("ix_watchlist_active", "active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False, index=True)
    sector = Column(String(100), default="")
    reason = Column(Text, default="")
    source = Column(String(30), default="")  # "opus_recommendation", "operator", "discovery"
    active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    deactivated_at = Column(DateTime, nullable=True)
```

**Also add relationship to Ticker model** (optional — DiscoveredTicker/WatchlistTicker use ticker string, not FK, since they can reference tickers not yet in the main table).

---

## 3. utils/anthropic_client.py — Add `analyze_with_tools()`

**Current file has:** `analyze()`, `analyze_with_fallback()`, `analyze_json()`, `analyze_json_with_fallback()`. Uses `anthropic` SDK with retry/tenacity.

**Add this method** to the `AnthropicClient` class. This handles Anthropic's web_search tool, which requires a multi-turn loop (model calls tool → we get results → model continues):

```python
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
        Completion with tool use (web_search). Handles multi-turn loop.
        Returns final text response after all tool calls resolved.
        """
        if tools is None:
            tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]

        messages = [{"role": "user", "content": user_prompt}]
        log.info("claude_tool_call_start", model=model, prompt_len=len(user_prompt))

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
                # Collect all text blocks from the final response
                text_parts = []
                for block in response.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                return "\n".join(text_parts)

            # Model wants to use tools — append assistant message and tool results
            messages.append({"role": "assistant", "content": response.content})

            # The Anthropic SDK handles web_search server-side for the web_search tool
            # (it's a built-in tool — results come back automatically in the response)
            # But we still need to check if the model stopped due to tool_use
            # For web_search_20250305, results are embedded in the response content
            # If stop_reason is "tool_use", we need to continue the conversation
            if response.stop_reason == "tool_use":
                # For server-side tools like web_search, the results are already
                # in the response. We just need to continue the conversation.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Continue your analysis with the search results."}
                ]})

        # Exhausted rounds — return whatever we have
        log.warning("tool_rounds_exhausted", model=model, max_rounds=max_tool_rounds)
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts) if text_parts else ""

    def analyze_with_tools_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: list = None,
        max_tokens: int = 8192,
    ) -> dict:
        """Tool-use completion that parses final response as JSON."""
        json_system = system_prompt + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown, no code fences, no explanation."
        text = self.analyze_with_tools(model, json_system, user_prompt, tools, max_tokens, temperature=0.2)
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
```

**NOTE:** The Anthropic web_search_20250305 tool is a **server-side tool** — the API handles fetching search results automatically. The multi-turn loop is needed because the model may call web_search multiple times, and each round the API returns results + the model's next action. Check the [Anthropic web search docs](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search) for the exact response format — the content blocks will include `web_search_tool_result` blocks that the model processes automatically.

---

## 4. utils/web_search_client.py — NEW

Abstraction layer so we can swap web search providers later.

```python
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
        Returns the model's final text response (after any searches).
        """
        if self.provider == "anthropic":
            return self._anthropic_search(system_prompt, user_prompt, model, max_searches, max_tokens)
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
            model = model or (self.settings.analyst_model if self.settings else "claude-sonnet-4-6")
            tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
            return self.anthropic_client.analyze_with_tools_json(
                model, system_prompt, user_prompt, tools, max_tokens
            )
        else:
            raise ValueError(f"Unsupported web search provider: {self.provider}")

    def _anthropic_search(self, system_prompt, user_prompt, model, max_searches, max_tokens) -> str:
        model = model or (self.settings.analyst_model if self.settings else "claude-sonnet-4-6")
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
        return self.anthropic_client.analyze_with_tools(
            model, system_prompt, user_prompt, tools, max_tokens
        )
```

---

## 5. agents/discovery_agent.py — NEW

**Key decisions:**
- Uses Sonnet (NOT Haiku) — quality over cost for idea generation
- Returns 8-12 tickers with pre-validated catalyst context
- Discovered tickers skip Haiku in the pipeline (already validated)
- Does NOT run on `/test` — only scheduled scans
- Uses `WebSearchClient` abstraction

```python
"""
Discovery Agent — dynamic idea sourcing via web search.
Finds actionable catalysts happening NOW, beyond the static S&P 500 universe.
Discovered tickers skip Haiku pre-screen (already validated by Discovery).
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime

from agents.base_agent import BaseAgent, AgentOutput
from utils.web_search_client import WebSearchClient
from database.db import get_session
from database.models import DiscoveredTicker as DiscoveredTickerModel
from utils.logger import get_logger

log = get_logger("discovery_agent")


@dataclass
class DiscoveredTicker:
    ticker: str
    catalyst_summary: str
    catalyst_type: str  # earnings_surprise, analyst_revision, m_and_a, etc.
    relevance_score: float  # 0-1
    direction_hint: str  # bullish, bearish, neutral
    discovery_context: str  # Full paragraph of context for Sonnet catalyst


@dataclass
class DiscoveryOutput:
    tickers: List[DiscoveredTicker] = field(default_factory=list)
    model_used: str = ""
    search_summary: str = ""
    run_id: str = ""


class DiscoveryAgent(BaseAgent):
    agent_type = "discovery"

    def __init__(self, settings, anthropic_client=None, web_search_client: WebSearchClient = None):
        super().__init__(settings, anthropic_client)
        self.web_search = web_search_client

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Not used — Discovery uses discover() instead."""
        return AgentOutput(agent_type=self.agent_type, reasoning="Use discover() method")

    def discover(self, regime: dict = None) -> DiscoveryOutput:
        """
        Web search for actionable catalysts happening NOW.
        Returns 8-12 quality tickers with catalyst summaries.
        """
        if not self.web_search:
            log.warning("no_web_search_client")
            return DiscoveryOutput(run_id=self.run_id)

        log.info("discovery_start", run_id=self.run_id)

        regime_context = ""
        if regime:
            regime_context = (
                f"Current macro regime: {regime.get('regime', 'neutral')} "
                f"(VIX: {regime.get('vix', 'N/A')}, "
                f"S&P distance from 200MA: {regime.get('sp500_distance_200ma', 'N/A')}%)"
            )

        system_prompt = (
            "You are an equity research discovery agent for a systematic swing trading system "
            "(1-20 day holding period). Your job is to search the web for actionable catalysts "
            "happening RIGHT NOW that could create swing trade opportunities.\n\n"
            "REQUIREMENTS:\n"
            "- Focus on US-listed equities with market cap > $500M\n"
            "- Only include tickers with SPECIFIC, ACTIONABLE catalysts (not general market commentary)\n"
            "- Each catalyst must have happened in the last 24-48 hours\n"
            "- Prefer quality over quantity — 8-12 high-quality ideas, not 20 weak ones\n"
            "- Include BOTH well-known and under-followed names\n"
            "- Look across ALL sectors, not just tech\n\n"
            "Search for:\n"
            "1. Earnings surprises (beats/misses with significant magnitude) and guidance changes\n"
            "2. Analyst upgrades/downgrades/price target changes (especially cluster revisions)\n"
            "3. FDA approvals, drug trial results, medical device clearances\n"
            "4. M&A activity (rumors, announcements, deal updates)\n"
            "5. Insider buying (especially cluster buys or large open-market purchases)\n"
            "6. Unusual volume/options activity suggesting informed positioning\n"
            "7. Management changes, activist investor activity\n"
            "8. Sector-specific catalysts (tariff changes, regulatory shifts, commodity moves)"
        )

        user_prompt = (
            f"Today is {datetime.now().strftime('%B %d, %Y')}.\n"
            f"{regime_context}\n\n"
            "Search for the most actionable swing trade catalysts happening right now. "
            "Look at financial news, earnings reports, analyst actions, SEC filings, "
            "and unusual market activity from the last 24-48 hours.\n\n"
            "For EACH ticker you find, provide:\n"
            "- ticker: The stock symbol\n"
            "- catalyst_summary: 1-2 sentence summary of the catalyst\n"
            "- catalyst_type: One of: earnings_surprise, analyst_revision, m_and_a, "
            "product_regulatory, insider_activity, management_change, capital_allocation, "
            "sector_catalyst, unusual_activity, other\n"
            "- relevance_score: 0.0-1.0 (how actionable is this for a 1-20 day swing trade?)\n"
            "- direction_hint: bullish, bearish, or neutral\n"
            "- discovery_context: Full paragraph explaining the catalyst, key numbers, "
            "and why this is actionable (this will be passed to a deeper analysis agent)\n\n"
            f"Return 8-{self.settings.discovery_max_tickers} tickers as JSON:\n"
            '{"tickers": [{"ticker": "AAPL", "catalyst_summary": "...", "catalyst_type": "...", '
            '"relevance_score": 0.85, "direction_hint": "bullish", "discovery_context": "..."}], '
            '"search_summary": "Brief summary of market conditions and what you found"}'
        )

        model = self.settings.discovery_model
        result = self.web_search.search_and_analyze_json(
            system_prompt, user_prompt, model=model, max_searches=10, max_tokens=8192
        )

        if result.get("error"):
            log.error("discovery_failed", error=result["error"])
            return DiscoveryOutput(run_id=self.run_id)

        # Parse results
        output = DiscoveryOutput(
            model_used=model,
            search_summary=result.get("search_summary", ""),
            run_id=self.run_id,
        )

        raw_tickers = result.get("tickers", [])
        for item in raw_tickers:
            ticker_sym = item.get("ticker", "").upper().strip()
            if not ticker_sym or len(ticker_sym) > 10:
                continue
            output.tickers.append(DiscoveredTicker(
                ticker=ticker_sym,
                catalyst_summary=item.get("catalyst_summary", ""),
                catalyst_type=item.get("catalyst_type", "other"),
                relevance_score=min(max(float(item.get("relevance_score", 0.5)), 0.0), 1.0),
                direction_hint=item.get("direction_hint", "neutral"),
                discovery_context=item.get("discovery_context", ""),
            ))

        # Validate tickers exist (basic check — yfinance validation in pipeline)
        output.tickers = self._validate_tickers(output.tickers)

        # Persist to DB
        self._save_discoveries(output)

        log.info("discovery_complete", found=len(output.tickers), run_id=self.run_id)
        return output

    def _validate_tickers(self, tickers: List[DiscoveredTicker]) -> List[DiscoveredTicker]:
        """Basic validation. Full yfinance check happens in pipeline."""
        validated = []
        seen = set()
        for t in tickers:
            # Deduplicate
            if t.ticker in seen:
                continue
            seen.add(t.ticker)
            # Basic format check
            if t.ticker.isalpha() and 1 <= len(t.ticker) <= 5:
                validated.append(t)
            elif '.' in t.ticker or '-' in t.ticker:
                # Allow tickers like BRK.B
                validated.append(t)
        return validated[:self.settings.discovery_max_tickers]

    def _save_discoveries(self, output: DiscoveryOutput):
        """Persist discovered tickers to database."""
        try:
            with get_session() as session:
                for t in output.tickers:
                    session.add(DiscoveredTickerModel(
                        ticker=t.ticker,
                        catalyst_summary=t.catalyst_summary,
                        catalyst_type=t.catalyst_type,
                        relevance_score=t.relevance_score,
                        direction_hint=t.direction_hint,
                        discovery_context=t.discovery_context,
                        model_used=output.model_used,
                        run_id=output.run_id,
                    ))
        except Exception as e:
            log.error("save_discoveries_failed", error=str(e))
```

---

## 6. orchestrator/universe.py — Add Watchlist Management

**Current file:** Has `seed_universe()` and `get_active_universe()`.

**Add these functions** after the existing code:

```python
from datetime import datetime, timedelta
from database.models import WatchlistTicker


def add_to_watchlist(ticker: str, reason: str = "", source: str = "operator", sector: str = "") -> bool:
    """
    Add a ticker to the watchlist. Returns True if added, False if already active.
    Enforces max size — deactivates oldest if at capacity.
    """
    from config.settings import Settings
    settings = Settings()

    with get_session() as session:
        # Check if already active
        existing = session.query(WatchlistTicker).filter_by(
            ticker=ticker, active=True
        ).first()
        if existing:
            log.info("watchlist_already_active", ticker=ticker)
            return False

        # Check capacity
        active_count = session.query(WatchlistTicker).filter_by(active=True).count()
        if active_count >= settings.watchlist_max_size:
            # Deactivate oldest
            oldest = session.query(WatchlistTicker).filter_by(active=True).order_by(
                WatchlistTicker.added_at.asc()
            ).first()
            if oldest:
                oldest.active = False
                oldest.deactivated_at = datetime.utcnow()
                log.info("watchlist_evicted", ticker=oldest.ticker)

        # If no sector provided, look up from UNIVERSE
        if not sector:
            sector = UNIVERSE.get(ticker, "Unknown")

        session.add(WatchlistTicker(
            ticker=ticker, sector=sector, reason=reason, source=source
        ))
        log.info("watchlist_added", ticker=ticker, source=source)
        return True


def get_watchlist() -> list[dict]:
    """Get all active watchlist tickers."""
    with get_session() as session:
        items = session.query(WatchlistTicker).filter_by(active=True).all()
        return [
            {"ticker": w.ticker, "sector": w.sector, "reason": w.reason, "source": w.source}
            for w in items
        ]


def remove_from_watchlist(ticker: str) -> bool:
    """Soft-delete a ticker from watchlist."""
    with get_session() as session:
        item = session.query(WatchlistTicker).filter_by(
            ticker=ticker, active=True
        ).first()
        if item:
            item.active = False
            item.deactivated_at = datetime.utcnow()
            log.info("watchlist_removed", ticker=ticker)
            return True
        return False


def expire_watchlist():
    """Deactivate watchlist items older than expiry threshold."""
    from config.settings import Settings
    settings = Settings()
    cutoff = datetime.utcnow() - timedelta(days=settings.watchlist_expiry_days)

    with get_session() as session:
        expired = session.query(WatchlistTicker).filter(
            WatchlistTicker.active == True,
            WatchlistTicker.added_at < cutoff,
        ).all()
        for item in expired:
            item.active = False
            item.deactivated_at = datetime.utcnow()
        if expired:
            log.info("watchlist_expired", count=len(expired))
```

---

## 7. config/tickers.py — Expand to S&P 500

Replace the current ~92 ticker UNIVERSE with ~503 S&P 500 tickers. Use `scripts/update_sp500.py` to generate this.

**Strategy:** The script fetches S&P 500 constituents from Wikipedia, maps them to GICS sectors, and writes the UNIVERSE dict to `config/tickers.py`. Keep `SECTOR_ETFS` unchanged.

---

## 8. scripts/update_sp500.py — NEW

```python
"""
Fetch current S&P 500 constituents and update config/tickers.py.
Source: Wikipedia S&P 500 page (public, well-maintained table).
Run manually whenever rebalancing occurs (~quarterly).
"""

import pandas as pd
from pathlib import Path

# GICS Sector mapping (Wikipedia uses GICS)
SECTOR_MAP = {
    "Information Technology": "Technology",
    "Health Care": "Healthcare",
    "Financials": "Financials",
    "Consumer Discretionary": "Consumer Discretionary",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Consumer Staples": "Consumer Staples",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Materials": "Materials",
}

OUTPUT_PATH = Path(__file__).parent.parent / "config" / "tickers.py"


def fetch_sp500():
    """Fetch S&P 500 list from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tables = pd.read_html(url)
    df = tables[0]  # First table is current constituents

    universe = {}
    for _, row in df.iterrows():
        symbol = row["Symbol"].replace(".", "-")  # BRK.B → BRK-B for yfinance
        sector = SECTOR_MAP.get(row["GICS Sector"], row["GICS Sector"])
        universe[symbol] = sector

    return universe


def write_tickers_file(universe: dict):
    """Write the tickers.py config file."""
    # Group by sector for readability
    by_sector = {}
    for ticker, sector in sorted(universe.items()):
        by_sector.setdefault(sector, []).append(ticker)

    lines = [
        '"""',
        'Ticker universe — S&P 500 constituents (auto-generated).',
        f'Last updated: {pd.Timestamp.now().strftime("%Y-%m-%d")}',
        'Sector assignments from GICS classification.',
        'Regenerate with: python scripts/update_sp500.py',
        '"""',
        '',
        'UNIVERSE = {',
    ]

    for sector in sorted(by_sector.keys()):
        tickers = sorted(by_sector[sector])
        lines.append(f'    # {sector}')
        # Write ~4 tickers per line for readability
        for i in range(0, len(tickers), 4):
            chunk = tickers[i:i+4]
            pairs = ", ".join(f'"{t}": "{sector}"' for t in chunk)
            lines.append(f'    {pairs},')

    lines.append('}')
    lines.append('')
    lines.append('# Sector ETFs for macro regime analysis')
    lines.append('SECTOR_ETFS = {')
    lines.append('    "XLK": "Technology",')
    lines.append('    "XLF": "Financials",')
    lines.append('    "XLV": "Healthcare",')
    lines.append('    "XLY": "Consumer Discretionary",')
    lines.append('    "XLP": "Consumer Staples",')
    lines.append('    "XLI": "Industrials",')
    lines.append('    "XLE": "Energy",')
    lines.append('    "XLC": "Communication Services",')
    lines.append('    "XLU": "Utilities",')
    lines.append('    "XLRE": "Real Estate",')
    lines.append('    "XLB": "Materials",')
    lines.append('}')
    lines.append('')

    OUTPUT_PATH.write_text('\n'.join(lines))
    print(f"Wrote {len(universe)} tickers to {OUTPUT_PATH}")


if __name__ == "__main__":
    universe = fetch_sp500()
    write_tickers_file(universe)
    print(f"S&P 500 universe updated: {len(universe)} tickers")
```

---

## 9. utils/escalation_manager.py — Update sonnet_analyze() Prompt

**Current `sonnet_analyze()` response format** (line 66-77) requests single `confidence` field.

**Replace the prompt's JSON format** with materiality/direction_confidence split:

```python
        prompt = (
            f"Ticker: {ticker}\n"
            f"Catalyst category: {haiku_result.get('category', 'unknown')}\n"
            f"Initial assessment: {haiku_result.get('summary', '')}\n\n"
            f"Company context:\n{company_context}\n\n"
            f"Full catalyst content:\n{catalyst_text[:5000]}\n\n"
            "Respond with JSON:\n"
            "{\n"
            '  "catalyst_type": "string",\n'
            '  "catalyst_summary": "2-3 sentence summary",\n'
            '  "magnitude": 1-5,\n'
            '  "direction": "bullish|bearish|ambiguous",\n'
            '  "materiality": 0.0-1.0,\n'
            '  "direction_confidence": 0.0-1.0,\n'
            '  "expected_impact_pct": {"low": float, "mid": float, "high": float},\n'
            '  "time_horizon_days": int,\n'
            '  "reasoning": "detailed analysis (3-5 sentences)",\n'
            '  "counter_arguments": "what could go wrong (2-3 sentences)"\n'
            "}\n\n"
            "SCORING GUIDANCE:\n"
            "- materiality: How significant/confirmed is this event? "
            "(0.9+ = major confirmed event like earnings beat, FDA approval; "
            "0.5-0.8 = notable but uncertain; <0.5 = minor/unconfirmed)\n"
            "- direction_confidence: How confident in the price direction? "
            "(0.8+ = clear directional signal; 0.5-0.7 = likely but uncertain; "
            "<0.5 = genuinely ambiguous)"
        )
```

**Also update the log line** to capture the new fields:

```python
        log.info(
            "sonnet_analyze",
            ticker=ticker,
            magnitude=result.get("magnitude"),
            materiality=result.get("materiality"),
            direction_confidence=result.get("direction_confidence"),
        )
```

---

## 10. agents/catalyst_agent.py — Materiality/Direction Split

### Changes to `analyze()` method (line 141-166):

Replace the score formula (currently lines 142-144):

```python
        # OLD:
        # magnitude = sonnet_result.get("magnitude", 1)
        # confidence = sonnet_result.get("confidence", 0.5)
        # catalyst_score = (magnitude / 5.0) * confidence

        # NEW: Materiality/direction confidence split
        materiality = sonnet_result.get("materiality", 0.5)
        direction_confidence = sonnet_result.get("direction_confidence", 0.5)
        catalyst_score = materiality * 0.7 + direction_confidence * 0.3
```

Update `raw_data` to include new fields:

```python
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", ""),
                "catalyst_summary": sonnet_result.get("catalyst_summary", ""),
                "magnitude": sonnet_result.get("magnitude", 1),
                "materiality": materiality,
                "direction_confidence": direction_confidence,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": best_haiku_score,
                "source": best_catalyst.get("source", ""),
            },
```

### Changes to `_analyze_provided_thesis()` method (line 168-202):

Same formula change:

```python
        materiality = sonnet_result.get("materiality", 0.6)
        direction_confidence = sonnet_result.get("direction_confidence", 0.6)
        catalyst_score = materiality * 0.7 + direction_confidence * 0.3
```

### Add `skip_haiku` and `discovery_context` support to `analyze()`:

At the top of `analyze()`, after `provided_thesis`:

```python
        # Discovery-sourced tickers skip Haiku (already validated)
        skip_haiku = kwargs.get("skip_haiku", False)
        discovery_context = kwargs.get("discovery_context", "")
        haiku_threshold_override = kwargs.get("haiku_threshold_override", None)

        # If skipping haiku (discovery source), go straight to Sonnet
        if skip_haiku and discovery_context and self.escalation:
            haiku_result = {
                "score": 5,
                "category": "discovery_validated",
                "summary": discovery_context[:200],
                "direction": kwargs.get("direction_hint", "bullish"),
                "relevant": True,
            }
            sonnet_result = self.escalation.sonnet_analyze(
                ticker, discovery_context, haiku_result, company_context
            )
            materiality = sonnet_result.get("materiality", 0.6)
            direction_confidence = sonnet_result.get("direction_confidence", 0.6)
            catalyst_score = materiality * 0.7 + direction_confidence * 0.3

            return AgentOutput(
                agent_type=self.agent_type,
                ticker=ticker,
                score=catalyst_score,
                confidence=(materiality + direction_confidence) / 2,
                direction=sonnet_result.get("direction", "bullish"),
                reasoning=sonnet_result.get("reasoning", ""),
                raw_data={
                    "catalyst_type": sonnet_result.get("catalyst_type", "discovery"),
                    "catalyst_summary": sonnet_result.get("catalyst_summary", ""),
                    "magnitude": sonnet_result.get("magnitude", 3),
                    "materiality": materiality,
                    "direction_confidence": direction_confidence,
                    "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                    "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                    "counter_arguments": sonnet_result.get("counter_arguments", ""),
                    "haiku_score": 5,
                    "source": "discovery",
                    "discovery_context": discovery_context,
                },
                run_id=self.run_id,
            )

        # Override haiku threshold for watchlist tickers
        effective_threshold = haiku_threshold_override or self.settings.catalyst_escalation_threshold
```

Then update the threshold check (line 125) to use `effective_threshold`:

```python
        if not best_catalyst or best_haiku_score < effective_threshold:
```

---

## 11. orchestrator/pipeline.py — Refactor run_full_scan()

**CRITICAL: `run_ad_hoc()` stays UNTOUCHED** except replacing `self.reddit_agent` with `self.reddit_agent` (Phase B will swap to web_research).

### Add ScanTickerItem dataclass (top of file):

```python
from dataclasses import dataclass

@dataclass
class ScanTickerItem:
    ticker: str
    sector: str
    source: str              # "discovery" | "watchlist" | "universe"
    haiku_threshold: int     # 0 = skip Haiku, 2 = low, 3 = normal
    discovery_context: str = ""  # Pre-validated catalyst context (discovery only)
    direction_hint: str = ""     # From discovery
```

### Add imports:

```python
from agents.discovery_agent import DiscoveryAgent, DiscoveryOutput
from orchestrator.universe import get_watchlist, add_to_watchlist
from utils.web_search_client import WebSearchClient
```

### Update `__init__()`:

After reddit_agent initialization:

```python
        # V2: Discovery Agent + Web Search
        self.web_search_client = None
        self.discovery_agent = None
        if settings.anthropic_api_key:
            self.web_search_client = WebSearchClient(
                settings.web_search_provider, self.anthropic_client, settings
            )
            self.discovery_agent = DiscoveryAgent(
                settings, self.anthropic_client, self.web_search_client
            )
```

### Refactor `run_full_scan()`:

```python
    def run_full_scan(self):
        if self.paused:
            log.info("pipeline_paused, skipping scan")
            return

        log.info("full_scan_start")
        run_start = datetime.now()

        # 1. Update macro regime
        regime_output = self.macro_agent.analyze()
        regime = regime_output.raw_data

        # 2. Discovery Agent — find new ideas via web search
        discovery_output = DiscoveryOutput()
        if self.discovery_agent:
            try:
                discovery_output = self.discovery_agent.discover(regime=regime)
                log.info("discovery_complete", found=len(discovery_output.tickers))
            except Exception as e:
                log.error("discovery_failed", error=str(e))

        # 3. Build merged scan list
        scan_list = self._build_scan_list(discovery_output)
        log.info("scan_list_built", total=len(scan_list),
                 discovery=sum(1 for s in scan_list if s.source == "discovery"),
                 watchlist=sum(1 for s in scan_list if s.source == "watchlist"),
                 universe=sum(1 for s in scan_list if s.source == "universe"))

        # 4. Process each ticker
        memos_generated = 0
        for item in scan_list:
            try:
                memo_data = self._process_scan_item(item, regime)
                if memo_data:
                    memos_generated += 1

                    # If Opus recommends watchlist, add it
                    opus_rec = memo_data.get("scoring", {}).get("opus_evaluation", {}).get("recommendation", "")
                    if opus_rec == "watchlist" and item.source != "watchlist":
                        add_to_watchlist(item.ticker, reason=f"Opus watchlist rec (score: {memo_data.get('scoring', {}).get('final_score', 0):.2f})", source="opus_recommendation")

            except Exception as e:
                log.error("ticker_scan_failed", ticker=item.ticker, error=str(e))
                continue

        duration = (datetime.now() - run_start).total_seconds()
        log.info("full_scan_complete", duration_s=duration, memos=memos_generated)

    def _build_scan_list(self, discovery_output: DiscoveryOutput) -> list:
        """Merge discovery + watchlist + universe, deduplicate."""
        seen = set()
        scan_list = []

        # Priority 1: Discovery (skip Haiku)
        for disc in discovery_output.tickers:
            if disc.ticker not in seen:
                seen.add(disc.ticker)
                sector = UNIVERSE.get(disc.ticker, "Unknown")
                scan_list.append(ScanTickerItem(
                    ticker=disc.ticker,
                    sector=sector,
                    source="discovery",
                    haiku_threshold=0,  # Skip Haiku
                    discovery_context=disc.discovery_context,
                    direction_hint=disc.direction_hint,
                ))

        # Priority 2: Watchlist (lower Haiku threshold)
        for w in get_watchlist():
            if w["ticker"] not in seen:
                seen.add(w["ticker"])
                scan_list.append(ScanTickerItem(
                    ticker=w["ticker"],
                    sector=w.get("sector", "Unknown"),
                    source="watchlist",
                    haiku_threshold=self.settings.watchlist_haiku_threshold,
                ))

        # Priority 3: Universe (normal Haiku threshold)
        for ticker, sector in UNIVERSE.items():
            if ticker not in seen:
                seen.add(ticker)
                scan_list.append(ScanTickerItem(
                    ticker=ticker,
                    sector=sector,
                    source="universe",
                    haiku_threshold=self.settings.catalyst_escalation_threshold,
                ))

        return scan_list

    def _process_scan_item(self, item: ScanTickerItem, regime: dict) -> dict:
        """Process a single ticker through the full pipeline."""
        # Ensure ticker is in DB
        self._ensure_ticker(item.ticker)

        # Route catalyst scan based on source
        catalyst_kwargs = {"sector": item.sector}

        if item.source == "discovery":
            # Skip Haiku — already validated by Discovery Agent
            catalyst_kwargs["skip_haiku"] = True
            catalyst_kwargs["discovery_context"] = item.discovery_context
            catalyst_kwargs["direction_hint"] = item.direction_hint
        elif item.source == "watchlist":
            catalyst_kwargs["haiku_threshold_override"] = item.haiku_threshold

        catalyst = self.catalyst_agent.analyze(ticker=item.ticker, **catalyst_kwargs)

        # Only proceed if catalyst is meaningful
        if catalyst.score < 0.3:
            return None

        # Run remaining agents
        fundamental = self.fundamental_agent.analyze(ticker=item.ticker, sector=item.sector)
        pattern = self.pattern_agent.analyze(
            ticker=item.ticker,
            catalyst_data=catalyst.raw_data,
            catalyst_reasoning=catalyst.reasoning,
        )
        sentiment = self.reddit_agent.analyze(ticker=item.ticker)  # Still stub until Phase B

        # Score
        portfolio_context = self._get_portfolio_context()
        result = self.scoring_engine.score_opportunity(
            item.ticker, catalyst, fundamental, pattern, sentiment,
            regime, portfolio_context,
        )

        # Generate memo if above threshold
        if result.get("meets_memo_threshold"):
            memo_data = self.memo_generator.generate(
                item.ticker, result, catalyst, fundamental, pattern, sentiment, regime,
            )
            if memo_data:
                memo_data["source"] = item.source
                log.info("memo_created", ticker=item.ticker, score=result["final_score"], source=item.source)
                return memo_data

        return None
```

---

## 12. bot/handlers/callbacks.py — Wire Watchlist Button

**Current `handle_watchlist()` (line 113-123):** Only updates memo status, doesn't actually add to watchlist table.

**Replace with:**

```python
async def handle_watchlist(query, context, memo_id: int):
    """Add to watchlist."""
    log.info("memo_watchlisted", memo_id=memo_id)
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo:
            memo.status = "watchlisted"
            memo.responded_at = datetime.utcnow()
            ticker_symbol = memo.ticker.symbol if memo.ticker else None
            sector = memo.ticker.sector if memo.ticker else ""

    # Actually add to watchlist table
    if ticker_symbol:
        from orchestrator.universe import add_to_watchlist
        added = add_to_watchlist(
            ticker_symbol,
            reason=f"Operator watchlisted from memo #{memo_id}",
            source="operator",
            sector=sector,
        )
        status_msg = "Added to watchlist" if added else "Already on watchlist"
    else:
        status_msg = "Could not determine ticker"

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"👀 {status_msg}. Will re-scan with lower threshold on next scan cycle.",
        parse_mode=None,
    )
```

---

## Testing Plan

After all Phase A changes:

1. **Unit: Materiality formula** — `0.92 * 0.7 + 0.58 * 0.3 ≈ 0.818` (NVDA example)
2. **Unit: `_build_scan_list()` dedup** — Discovery > Watchlist > Universe priority
3. **Integration: `/test NVDA`** — Verify ad-hoc flow is UNCHANGED (still works exactly as before)
4. **Integration: Full scan** — Run `pipeline.run_full_scan()` and verify:
   - Discovery Agent finds tickers via web search
   - Discovery tickers skip Haiku
   - Watchlist tickers use threshold 2
   - Universe tickers use threshold 3
5. **Watchlist: Button wiring** — Verify watchlist button adds to DB
6. **S&P 500: `scripts/update_sp500.py`** — Run and verify ~500 tickers generated

---

## Dependencies to Add (requirements.txt)

```
# For scripts/update_sp500.py
lxml>=5.0  # pandas read_html parser
```

No other new deps needed — Anthropic SDK already supports web_search tool.

---

## What This Does NOT Change (Phase B+)

- **Scoring weights** — Still `{"catalyst": 0.40, "fundamental": 0.30, "pattern": 0.22, "sentiment": 0.08}` until Phase B
- **Reddit agent** — Still a stub, replaced in Phase B by WebResearchAgent
- **Memo template** — Unchanged until Phase C
- **Deep Research** — Settings added but not wired until Phase C
- **Pattern enrichment** — Phase D, independent track
