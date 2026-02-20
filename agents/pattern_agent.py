"""
Historical Pattern Agent — finds analogous historical setups and measures outcomes.

Flow:
1. Classify the thesis into a standardized setup type (Sonnet call)
2. Search for historical instances via FMP + cached data
3. Compute forward returns via yfinance
4. Compute summary statistics (win rate, median return, drawdown)
5. Interpret via Sonnet (2-3 sentence summary)
6. Return AgentOutput with score based on win rate × sample confidence × risk/reward
"""

from datetime import datetime as dt
from agents.base_agent import BaseAgent, AgentOutput
from config.peers import get_peers
from data.pattern_data import PatternDataAdapter
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("pattern_agent")

# Setup types that map to structured FMP data
STRUCTURED_SETUP_TYPES = {
    "earnings_beat_guide_up",
    "earnings_beat_guide_flat",
    "earnings_beat_guide_down",
    "earnings_miss",
    "revenue_acceleration",
    "insider_cluster_buy",
    "analyst_upgrade_cluster",
    "analyst_downgrade",
    "buyback_announcement",
    "dividend_initiation",
}

# Setup types where we fall back to sector/news-based matching
UNSTRUCTURED_SETUP_TYPES = {
    "sector_catalyst_positive",
    "sector_catalyst_negative",
    "product_launch",
    "regulatory_approval",
    "management_change",
    "m_and_a",
    "general_positive_catalyst",
    "general_negative_catalyst",
}

ALL_SETUP_TYPES = STRUCTURED_SETUP_TYPES | UNSTRUCTURED_SETUP_TYPES

# Max historical instances to process (ticker + peers combined)
MAX_INSTANCES = 30


class PatternAgent(BaseAgent):
    agent_type = "pattern"

    def __init__(self, settings, anthropic_client=None):
        super().__init__(settings, anthropic_client)
        self.pattern_data = PatternDataAdapter(settings.fmp_api_key)

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """
        Full pattern analysis pipeline:
        1. Classify setup type
        2. Search historical instances
        3. Compute forward returns
        4. Compute summary stats
        5. Interpret via Sonnet
        6. Score and return
        """
        if not ticker:
            return AgentOutput(agent_type=self.agent_type, reasoning="No ticker provided")

        log.info("pattern_analysis_start", ticker=ticker)

        catalyst_data = kwargs.get("catalyst_data", {})
        catalyst_reasoning = kwargs.get("catalyst_reasoning", "")

        # Step 1: Classify the setup type
        setup = self._classify_setup(ticker, catalyst_data, catalyst_reasoning)
        setup_type = setup.get("setup_type", "general_positive_catalyst")
        log.info("setup_classified", ticker=ticker, setup_type=setup_type)

        # Step 2: Search for historical instances
        peers = get_peers(ticker)
        all_tickers = [ticker] + peers
        instances = self._find_historical_instances(ticker, setup_type, setup, all_tickers)

        if not instances:
            log.info("no_historical_instances", ticker=ticker, setup_type=setup_type)
            return AgentOutput(
                agent_type=self.agent_type,
                ticker=ticker,
                score=0.5,
                confidence=0.1,
                direction="neutral",
                reasoning=f"No historical instances found for setup type '{setup_type}'. Insufficient data for pattern analysis.",
                raw_data={
                    "setup_type": setup_type,
                    "same_ticker_instances": 0,
                    "peer_instances": 0,
                    "sample_size_warning": True,
                    "status": "no_data",
                },
                run_id=self.run_id,
            )

        # Step 3: Summary statistics
        stats = PatternDataAdapter.compute_summary_stats(instances)
        log.info("pattern_stats_computed", ticker=ticker, total=stats.get("total_instances", 0),
                 win_rate=stats.get("win_rate_t10", 0))

        # Step 4: Score
        score, confidence, direction = self._compute_score(stats)

        # Step 5: Interpret via Sonnet
        interpretation = self._interpret(ticker, setup_type, setup, stats)

        log.info("pattern_analysis_complete", ticker=ticker, score=score, confidence=confidence)

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=score,
            confidence=confidence,
            direction=direction,
            reasoning=interpretation,
            raw_data={
                "setup_type": setup_type,
                "setup_params": setup.get("setup_params", {}),
                "same_ticker_instances": stats.get("same_ticker_count", 0),
                "peer_instances": stats.get("peer_count", 0),
                "total_instances": stats.get("total_instances", 0),
                "win_rate_t10": stats.get("win_rate_t10", 0),
                "median_return_t10": stats.get("median_return_t10", 0),
                "avg_winner_t10": stats.get("avg_winner_t10", 0),
                "avg_loser_t10": stats.get("avg_loser_t10", 0),
                "max_drawdown_median": stats.get("max_drawdown_median", 0),
                "max_drawdown_worst": stats.get("max_drawdown_worst", 0),
                "sample_size_warning": stats.get("total_instances", 0) < 10,
                "status": "active",
            },
            run_id=self.run_id,
        )

    # ── Step 1: Setup Classification ─────────────────────────────────

    def _classify_setup(self, ticker: str, catalyst_data: dict, catalyst_reasoning: str) -> dict:
        """Classify the current thesis into a standardized setup type via Sonnet."""
        if not self.client:
            return {"setup_type": "general_positive_catalyst", "setup_params": {}, "search_strategy": "default"}

        setup_types_str = "\n".join(f"- {st}" for st in sorted(ALL_SETUP_TYPES))

        prompt = (
            f"Classify this trade catalyst for {ticker} into one of the standardized setup types.\n\n"
            f"CATALYST DATA:\n"
            f"  Type: {catalyst_data.get('catalyst_type', 'N/A')}\n"
            f"  Summary: {catalyst_data.get('catalyst_summary', catalyst_reasoning[:300])}\n"
            f"  Direction: {catalyst_data.get('direction', 'N/A')}\n"
            f"  Magnitude: {catalyst_data.get('magnitude', 'N/A')}/5\n\n"
            f"AVAILABLE SETUP TYPES:\n{setup_types_str}\n\n"
            "Respond with JSON:\n"
            '{"setup_type": "<one of the types above>", '
            '"setup_params": {"beat_magnitude_pct": <number or null>, "guidance_direction": "<up/flat/down or null>"}, '
            '"search_strategy": "<brief description of how to find historical matches>"}'
        )

        try:
            model = get_model("pattern_interpret", self.settings)
            result = self.client.analyze_json(
                model,
                "You classify trade catalysts into standardized historical setup types for pattern matching.",
                prompt,
                max_tokens=300,
            )
            # Validate setup_type
            st = result.get("setup_type", "general_positive_catalyst")
            if st not in ALL_SETUP_TYPES:
                st = "general_positive_catalyst"
                result["setup_type"] = st
            return result
        except Exception as e:
            log.error("setup_classification_failed", ticker=ticker, error=str(e))
            return {"setup_type": "general_positive_catalyst", "setup_params": {}, "search_strategy": "default"}

    # ── Step 2: Historical Search ────────────────────────────────────

    def _find_historical_instances(self, ticker: str, setup_type: str, setup: dict,
                                    all_tickers: list[str]) -> list[dict]:
        """
        Find historical instances of the setup type for ticker + peers.
        Uses cached data first, then fetches from FMP + computes returns via yfinance.
        """
        # Check cache first
        cached = self.pattern_data.get_cached_patterns(setup_type, all_tickers)
        if cached:
            log.info("cache_hit", ticker=ticker, setup_type=setup_type, count=len(cached))
            valid = [c for c in cached if c.get("return_t10") is not None]
            if len(valid) >= 5:
                return valid[:MAX_INSTANCES]

        # Fetch fresh data from FMP based on setup type
        instances = []

        # Route to the right search function based on setup type
        EARNINGS_ROUTED = {
            "earnings_beat_guide_up", "earnings_beat_guide_flat",
            "earnings_beat_guide_down", "earnings_miss",
            "revenue_acceleration",  # Shows up in earnings beats + guidance raises
            "buyback_announcement",  # Often coincides with earnings
            "dividend_initiation",   # Often coincides with earnings
        }

        if setup_type in EARNINGS_ROUTED:
            instances = self._search_earnings_patterns(ticker, setup_type, setup, all_tickers)

        elif setup_type == "insider_cluster_buy":
            instances = self._search_insider_patterns(ticker, all_tickers)

        elif setup_type in ("analyst_upgrade_cluster", "analyst_downgrade"):
            instances = self._search_analyst_patterns(ticker, setup_type, all_tickers)

        else:
            # For unstructured types, use earnings beats as a rough proxy
            instances = self._search_earnings_patterns(
                ticker, "earnings_beat_guide_up", setup, all_tickers
            )

        # Merge with any partial cache
        if cached:
            cached_keys = {(c["source_ticker"], c["event_date"]) for c in cached}
            for inst in instances:
                if (inst["source_ticker"], inst["event_date"]) not in cached_keys:
                    cached.append(inst)
            instances = cached

        # Filter to valid instances and cap
        instances = [i for i in instances if i.get("return_t10") is not None]
        return instances[:MAX_INSTANCES]

    def _search_earnings_patterns(self, ticker: str, setup_type: str, setup: dict,
                                   all_tickers: list[str]) -> list[dict]:
        """Search for earnings-based patterns."""
        instances = []
        beat_threshold = setup.get("setup_params", {}).get("beat_magnitude_pct", 5)
        if not isinstance(beat_threshold, (int, float)):
            beat_threshold = 5

        for t in all_tickers:
            is_peer = t != ticker
            surprises = self.pattern_data.get_earnings_surprises(t)

            for event in surprises:
                surprise_pct = event.get("surprise_pct", 0)

                # Filter based on setup type
                if setup_type in ("earnings_beat_guide_up", "earnings_beat_guide_flat",
                                  "revenue_acceleration", "buyback_announcement",
                                  "dividend_initiation"):
                    if surprise_pct < beat_threshold:
                        continue
                elif setup_type == "earnings_miss":
                    if surprise_pct >= 0:
                        continue
                elif setup_type == "earnings_beat_guide_down":
                    if surprise_pct < 0:
                        continue

                event_date = event.get("event_date", "")
                if not event_date:
                    continue

                returns = self.pattern_data.compute_forward_returns(t, event_date)
                if not returns:
                    continue

                instance = {
                    "setup_type": setup_type,
                    "event_date": event_date,
                    "source_ticker": t,
                    "is_peer": is_peer,
                    "beat_magnitude": surprise_pct,
                    "return_t5": returns.get("return_t5"),
                    "return_t10": returns.get("return_t10"),
                    "return_t15": returns.get("return_t15"),
                    "return_t20": returns.get("return_t20"),
                    "max_drawdown": returns.get("max_drawdown"),
                    "max_drawdown_day": returns.get("max_drawdown_day"),
                }

                self.pattern_data.cache_pattern(
                    setup_type=setup_type,
                    source_ticker=t,
                    event_date=event_date,
                    is_peer=is_peer,
                    beat_magnitude=surprise_pct,
                    returns=returns,
                    target_ticker=ticker,
                )

                instances.append(instance)

                if len(instances) >= MAX_INSTANCES:
                    return instances

        return instances

    def _search_insider_patterns(self, ticker: str, all_tickers: list[str]) -> list[dict]:
        """Search for insider buying cluster patterns."""
        instances = []

        for t in all_tickers:
            is_peer = t != ticker
            trades = self.pattern_data.get_insider_trading(t)

            if not trades:
                continue

            sorted_trades = sorted(trades, key=lambda x: x.get("event_date", ""))

            for i, trade in enumerate(sorted_trades):
                event_date = trade.get("event_date", "")
                if not event_date:
                    continue
                try:
                    trade_dt = dt.strptime(event_date, "%Y-%m-%d")
                except ValueError:
                    continue

                # Count purchases within 14 days
                cluster_count = 1
                for j in range(i + 1, len(sorted_trades)):
                    other_date = sorted_trades[j].get("event_date", "")
                    if not other_date:
                        continue
                    try:
                        other_dt = dt.strptime(other_date, "%Y-%m-%d")
                    except ValueError:
                        continue
                    if (other_dt - trade_dt).days <= 14:
                        cluster_count += 1
                    else:
                        break

                if cluster_count < 2:
                    continue

                returns = self.pattern_data.compute_forward_returns(t, event_date)
                if not returns:
                    continue

                instance = {
                    "setup_type": "insider_cluster_buy",
                    "event_date": event_date,
                    "source_ticker": t,
                    "is_peer": is_peer,
                    "beat_magnitude": cluster_count,
                    "return_t5": returns.get("return_t5"),
                    "return_t10": returns.get("return_t10"),
                    "return_t15": returns.get("return_t15"),
                    "return_t20": returns.get("return_t20"),
                    "max_drawdown": returns.get("max_drawdown"),
                    "max_drawdown_day": returns.get("max_drawdown_day"),
                }

                self.pattern_data.cache_pattern(
                    setup_type="insider_cluster_buy",
                    source_ticker=t,
                    event_date=event_date,
                    is_peer=is_peer,
                    beat_magnitude=cluster_count,
                    returns=returns,
                    target_ticker=ticker,
                )

                instances.append(instance)

                if len(instances) >= MAX_INSTANCES:
                    return instances

        return instances

    def _search_analyst_patterns(self, ticker: str, setup_type: str,
                                  all_tickers: list[str]) -> list[dict]:
        """Search for analyst upgrade/downgrade patterns."""
        instances = []

        for t in all_tickers:
            is_peer = t != ticker
            events = self.pattern_data.get_upgrades_downgrades(t)

            if not events:
                continue

            for event in events:
                action = (event.get("action", "") or "").lower()

                if setup_type == "analyst_upgrade_cluster" and "upgrade" not in action and "reiterat" not in action:
                    continue
                if setup_type == "analyst_downgrade" and "downgrade" not in action:
                    continue

                event_date = event.get("event_date", "")
                if not event_date:
                    continue

                returns = self.pattern_data.compute_forward_returns(t, event_date)
                if not returns:
                    continue

                instance = {
                    "setup_type": setup_type,
                    "event_date": event_date,
                    "source_ticker": t,
                    "is_peer": is_peer,
                    "beat_magnitude": None,
                    "return_t5": returns.get("return_t5"),
                    "return_t10": returns.get("return_t10"),
                    "return_t15": returns.get("return_t15"),
                    "return_t20": returns.get("return_t20"),
                    "max_drawdown": returns.get("max_drawdown"),
                    "max_drawdown_day": returns.get("max_drawdown_day"),
                }

                self.pattern_data.cache_pattern(
                    setup_type=setup_type,
                    source_ticker=t,
                    event_date=event_date,
                    is_peer=is_peer,
                    beat_magnitude=None,
                    returns=returns,
                    target_ticker=ticker,
                )

                instances.append(instance)

                if len(instances) >= MAX_INSTANCES:
                    return instances

        return instances

    # ── Step 4: Scoring ──────────────────────────────────────────────

    def _compute_score(self, stats: dict) -> tuple[float, float, str]:
        """Compute score, confidence, and direction from summary stats."""
        total = stats.get("total_instances", 0)
        win_rate = stats.get("win_rate_t10", 0.5)

        # Base score from win rate
        base_score = win_rate

        # Sample size confidence adjustment
        if total < 5:
            confidence_adj = 0.5
        elif total < 10:
            confidence_adj = 0.7
        elif total < 20:
            confidence_adj = 0.85
        else:
            confidence_adj = 1.0

        # Risk/reward quality adjustment
        avg_winner = stats.get("avg_winner_t10", 0)
        avg_loser = abs(stats.get("avg_loser_t10", -1))
        if avg_loser > 0:
            rr_ratio = avg_winner / avg_loser
            rr_adj = min(1.2, max(0.7, rr_ratio / 2.0))
        else:
            rr_adj = 1.1

        score = base_score * confidence_adj * rr_adj
        score = max(0.0, min(1.0, score))

        # Confidence: sample size × consistency
        std_dev = stats.get("std_dev_t10", 10)
        confidence = confidence_adj * max(0.1, 1 - std_dev / 20)
        confidence = max(0.0, min(1.0, confidence))

        # Direction from median return
        median = stats.get("median_return_t10", 0)
        direction = "bullish" if median > 0 else "bearish" if median < -1 else "neutral"

        return round(score, 3), round(confidence, 3), direction

    # ── Step 5: Interpretation ───────────────────────────────────────

    def _interpret(self, ticker: str, setup_type: str, setup: dict, stats: dict) -> str:
        """Use Sonnet to interpret the statistical patterns."""
        if not self.client:
            return self._fallback_interpretation(stats)

        prompt = (
            f"Interpret these historical pattern statistics for {ticker}.\n\n"
            f"SETUP TYPE: {setup_type}\n"
            f"TOTAL INSTANCES: {stats.get('total_instances', 0)} "
            f"(same ticker: {stats.get('same_ticker_count', 0)}, peers: {stats.get('peer_count', 0)})\n\n"
            f"FORWARD RETURNS (T+10 trading days):\n"
            f"  Win rate: {stats.get('win_rate_t10', 0):.0%}\n"
            f"  Median return: {stats.get('median_return_t10', 0):.1f}%\n"
            f"  Avg winner: +{stats.get('avg_winner_t10', 0):.1f}%\n"
            f"  Avg loser: {stats.get('avg_loser_t10', 0):.1f}%\n\n"
            f"DRAWDOWN:\n"
            f"  Median max drawdown: {stats.get('max_drawdown_median', 0):.1f}%\n"
            f"  Worst drawdown: {stats.get('max_drawdown_worst', 0):.1f}%\n\n"
            "Write 2-3 sentences interpreting what these historical analogs suggest for this trade. "
            "Consider sample size, consistency, typical drawdown path, and risk/reward."
        )

        try:
            model = get_model("pattern_interpret", self.settings)
            result = self.client.analyze(
                model,
                "You are interpreting historical market pattern data for a swing trade thesis.",
                prompt,
                max_tokens=200,
            )
            return result.strip()
        except Exception as e:
            log.error("interpretation_failed", ticker=ticker, error=str(e))
            return self._fallback_interpretation(stats)

    def _fallback_interpretation(self, stats: dict) -> str:
        """Generate a basic interpretation without Sonnet."""
        total = stats.get("total_instances", 0)
        win_rate = stats.get("win_rate_t10", 0)
        median = stats.get("median_return_t10", 0)
        dd = stats.get("max_drawdown_median", 0)

        if total < 5:
            size_note = "Very small sample size limits reliability."
        elif total < 15:
            size_note = "Moderate sample size provides directional guidance."
        else:
            size_note = "Robust sample size supports statistical reliability."

        return (
            f"Across {total} historical instances, the setup showed a {win_rate:.0%} win rate "
            f"with a median T+10 return of {median:+.1f}%. "
            f"Typical max drawdown was {dd:.1f}%. {size_note}"
        )
