"""
Trading Pipeline — the central orchestration logic.
Chains: data fetch → agent analysis → scoring → memo generation → delivery.
"""

import asyncio
import json
from datetime import datetime

from config.tickers import UNIVERSE
from agents.macro_agent import MacroRegimeAgent
from agents.catalyst_agent import CatalystAgent
from agents.fundamental_agent import FundamentalAgent
from agents.pattern_agent import PatternAgent
from agents.reddit_agent import RedditSentimentAgent
from scoring.engine import ScoringEngine
from memo.generator import MemoGenerator
from execution.alpaca_client import AlpacaClient
from execution.risk_manager import RiskManager
from execution.position_manager import PositionManager
from execution.order_manager import OrderManager
from orchestrator.universe import seed_universe, get_active_universe
from utils.anthropic_client import AnthropicClient
from utils.logger import get_logger

log = get_logger("pipeline")


class TradingPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.paused = False

        # Initialize Claude client
        self.anthropic_client = None
        if settings.anthropic_api_key:
            self.anthropic_client = AnthropicClient(settings.anthropic_api_key)

        # Initialize agents
        self.macro_agent = MacroRegimeAgent(settings, self.anthropic_client)
        self.catalyst_agent = CatalystAgent(settings, self.anthropic_client)
        self.fundamental_agent = FundamentalAgent(settings, self.anthropic_client)
        self.pattern_agent = PatternAgent(settings, self.anthropic_client)
        self.reddit_agent = RedditSentimentAgent(settings, self.anthropic_client)

        # Initialize scoring and memo
        self.scoring_engine = ScoringEngine(settings, self.anthropic_client)
        self.memo_generator = MemoGenerator(settings, self.anthropic_client)

        # Initialize execution
        self.alpaca = AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        self.risk_manager = RiskManager(settings)
        self.position_manager = PositionManager(settings)
        self.order_manager = OrderManager(settings, self.alpaca, self.risk_manager, self.position_manager)

        # Telegram notification manager (set after bot starts)
        self.notification_manager = None

        log.info("pipeline_initialized")

    def get_sector(self, ticker: str) -> str:
        """Get sector for a ticker."""
        return UNIVERSE.get(ticker, "Unknown")

    def run_full_scan(self):
        """
        Full universe scan: macro regime → catalyst scan → score → memo.
        Called 3x daily by the scheduler.
        """
        if self.paused:
            log.info("pipeline_paused, skipping scan")
            return

        log.info("full_scan_start", universe_size=len(UNIVERSE))
        run_start = datetime.now()

        # 1. Update macro regime
        regime_output = self.macro_agent.analyze()
        regime = regime_output.raw_data

        # 2. Scan each ticker for catalysts
        memos_generated = 0
        for ticker, sector in UNIVERSE.items():
            try:
                # Catalyst scan
                catalyst = self.catalyst_agent.analyze(ticker=ticker, sector=sector)

                # Only proceed if catalyst is meaningful
                if catalyst.score < 0.3:
                    continue

                # Run remaining agents
                fundamental = self.fundamental_agent.analyze(ticker=ticker, sector=sector)
                pattern = self.pattern_agent.analyze(
                    ticker=ticker,
                    catalyst_data=catalyst.raw_data,
                    catalyst_reasoning=catalyst.reasoning,
                )
                sentiment = self.reddit_agent.analyze(ticker=ticker)

                # Score opportunity
                portfolio_context = self._get_portfolio_context()
                result = self.scoring_engine.score_opportunity(
                    ticker, catalyst, fundamental, pattern, sentiment,
                    regime, portfolio_context,
                )

                # Generate memo if above threshold
                if result.get("meets_memo_threshold"):
                    memo_data = self.memo_generator.generate(
                        ticker, result, catalyst, fundamental, pattern, sentiment, regime,
                    )
                    if memo_data:
                        memos_generated += 1
                        # Telegram delivery happens via the bot (notification manager)
                        log.info("memo_created", ticker=ticker, score=result["final_score"])

            except Exception as e:
                log.error("ticker_scan_failed", ticker=ticker, error=str(e))
                continue

        duration = (datetime.now() - run_start).total_seconds()
        log.info("full_scan_complete", duration_s=duration, memos=memos_generated)

    def run_ad_hoc(self, ticker: str, thesis: str = "") -> dict:
        """
        Run full pipeline for a single ticker (triggered by /test command).
        Skips Haiku pre-screening if thesis is provided.
        Returns memo data dict.
        """
        log.info("ad_hoc_start", ticker=ticker, has_thesis=bool(thesis))

        # Ensure ticker is in DB
        self._ensure_ticker(ticker)

        sector = self.get_sector(ticker)

        # 1. Get regime
        regime = self.macro_agent.get_latest_regime()

        # 2. Run all agents
        catalyst = self.catalyst_agent.analyze(ticker=ticker, sector=sector, thesis=thesis)
        fundamental = self.fundamental_agent.analyze(ticker=ticker, sector=sector)
        pattern = self.pattern_agent.analyze(
            ticker=ticker,
            catalyst_data=catalyst.raw_data,
            catalyst_reasoning=catalyst.reasoning,
        )
        sentiment = self.reddit_agent.analyze(ticker=ticker)

        # 3. Score
        portfolio_context = self._get_portfolio_context()
        result = self.scoring_engine.score_opportunity(
            ticker, catalyst, fundamental, pattern, sentiment,
            regime, portfolio_context,
        )

        # 4. Generate memo (always for ad-hoc, regardless of threshold)
        memo_data = self.memo_generator.generate(
            ticker, result, catalyst, fundamental, pattern, sentiment, regime,
        )

        return memo_data

    async def run_ad_hoc_async(self, ticker: str, thesis: str = "") -> dict:
        """Async wrapper for ad-hoc analysis (called from Telegram handler)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.run_ad_hoc, ticker, thesis)

    def _get_portfolio_context(self) -> str:
        """Build portfolio context string for Opus evaluation."""
        try:
            account = self.alpaca.get_account_info()
            positions = self.alpaca.get_positions_detail()
            parts = [
                f"Portfolio: ${account.get('equity', 0):,.2f}, Cash: ${account.get('cash', 0):,.2f}",
                f"Open positions: {len(positions)}",
            ]
            for p in positions:
                parts.append(f"  {p['ticker']}: {p.get('qty', 0)} shares, P&L {p.get('pnl_pct', 0):+.2f}%")
            return "\n".join(parts)
        except Exception:
            return f"Portfolio: ${self.settings.portfolio_value:,.2f} (initial), 0 positions"

    def _ensure_ticker(self, ticker: str):
        """Make sure a ticker exists in the DB (for ad-hoc analysis)."""
        from database.db import get_session
        from database.models import Ticker
        with get_session() as session:
            existing = session.query(Ticker).filter_by(symbol=ticker).first()
            if not existing:
                sector = UNIVERSE.get(ticker, "Unknown")
                session.add(Ticker(symbol=ticker, sector=sector, in_universe=ticker in UNIVERSE))
