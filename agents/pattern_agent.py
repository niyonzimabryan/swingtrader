"""
Historical Pattern Agent — finds analogous historical setups and measures outcomes.
V2: Contextual similarity scoring — weights historical instances by how similar
their conditions were to the current setup.

Flow:
1. Classify the thesis into a standardized setup type (Sonnet call)
2. Search for historical instances via FMP + cached data
3. Compute contextual similarity for each instance (V2)
4. Compute similarity-weighted summary statistics (win rate, median return, drawdown)
5. Interpret via Sonnet (2-3 sentence summary, now including similarity info)
6. Return AgentOutput with score based on weighted win rate × sample confidence × risk/reward
"""

import numpy as np
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

# V2: Contextual similarity weights
SIMILARITY_WEIGHTS = {
    "valuation": 0.30,    # Forward P/E regime proximity
    "beat_magnitude": 0.20,  # Catalyst magnitude similarity
    "macro_regime": 0.20,  # Macro regime match (risk-on/neutral/risk-off)
    "momentum": 0.15,     # Prior 20-day momentum proximity
    "vix": 0.15,          # Market sentiment (VIX level proximity)
}

# Threshold: instances with similarity above this are "highly similar"
HIGH_SIMILARITY_THRESHOLD = 0.60


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
        3. Compute contextual similarity (V2)
        4. Compute similarity-weighted summary stats
        5. Interpret via Sonnet (with similarity data)
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

        # Step 3: V2 — Compute similarity scores for each instance
        current_context = self._get_current_context(ticker, catalyst_data)
        instances = self._score_instance_similarities(instances, current_context)

        highly_similar = [i for i in instances if i.get("similarity", 0) >= HIGH_SIMILARITY_THRESHOLD]
        log.info("similarity_scored", ticker=ticker, total=len(instances),
                 highly_similar=len(highly_similar))

        # Step 4: Similarity-weighted summary statistics
        stats = PatternDataAdapter.compute_summary_stats(instances)

        # V2: Also compute similarity-weighted stats
        weighted_stats = self._compute_weighted_stats(instances)
        stats.update(weighted_stats)

        log.info("pattern_stats_computed", ticker=ticker, total=stats.get("total_instances", 0),
                 win_rate=stats.get("win_rate_t10", 0),
                 weighted_win_rate=stats.get("weighted_win_rate_t10", 0))

        # Step 5: Score (V2: uses weighted stats when available)
        score, confidence, direction = self._compute_score(stats)

        # Step 6: Interpret via Sonnet (V2: includes similarity data)
        interpretation = self._interpret(ticker, setup_type, setup, stats)

        # Find most similar instance for memo display
        most_similar = self._get_most_similar_instance(instances)

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
                # V2: Similarity data
                "highly_similar_count": len(highly_similar),
                "weighted_win_rate_t10": stats.get("weighted_win_rate_t10", stats.get("win_rate_t10", 0)),
                "weighted_median_return_t10": stats.get("weighted_median_return_t10", stats.get("median_return_t10", 0)),
                "most_similar": most_similar,
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

    # ── Step 3: Contextual Similarity (V2) ─────────────────────────

    def _get_current_context(self, ticker: str, catalyst_data: dict) -> dict:
        """
        Gather current market context for similarity comparison.
        Returns dict with: fwd_pe, beat_magnitude, macro_regime, momentum_20d, vix.
        Uses best-effort — returns None for unavailable dimensions.
        """
        import yfinance as yf

        context = {
            "fwd_pe": None,
            "beat_magnitude": None,
            "macro_regime": None,
            "momentum_20d": None,
            "vix": None,
        }

        try:
            # Beat magnitude from catalyst data
            context["beat_magnitude"] = catalyst_data.get("beat_magnitude") or catalyst_data.get("magnitude")

            # Get macro regime from the most recent DB entry, fallback to inference
            try:
                from database.db import get_session
                from database.models import MacroRegime
                with get_session() as session:
                    latest = session.query(MacroRegime).order_by(MacroRegime.date.desc()).first()
                    if latest:
                        context["macro_regime"] = latest.regime
            except Exception:
                pass

            # VIX level
            try:
                vix = yf.Ticker("^VIX")
                vix_hist = vix.history(period="5d")
                if not vix_hist.empty:
                    context["vix"] = float(vix_hist["Close"].iloc[-1])
            except Exception:
                pass

            # Fallback: infer macro regime from VIX if DB didn't have it
            if not context["macro_regime"] and context["vix"] is not None:
                vix_val = context["vix"]
                if vix_val > 25:
                    context["macro_regime"] = "risk-off"
                elif vix_val < 16:
                    context["macro_regime"] = "risk-on"
                else:
                    context["macro_regime"] = "neutral"

            # Forward P/E and 20-day momentum for the ticker
            try:
                stock = yf.Ticker(ticker)
                info = stock.info
                context["fwd_pe"] = info.get("forwardPE") or info.get("forwardEps")

                hist = stock.history(period="30d")
                if len(hist) >= 20:
                    close_now = float(hist["Close"].iloc[-1])
                    close_20d_ago = float(hist["Close"].iloc[-20])
                    if close_20d_ago > 0:
                        context["momentum_20d"] = round(((close_now / close_20d_ago) - 1) * 100, 2)
            except Exception:
                pass

        except Exception as e:
            log.warning("context_gathering_failed", ticker=ticker, error=str(e))

        return context

    def _score_instance_similarities(self, instances: list[dict], current_context: dict) -> list[dict]:
        """
        Score each historical instance's similarity to current context.
        Adds 'similarity' key (0-1) to each instance.
        Instances without context data get similarity 0.5 (neutral).
        """
        from database.db import get_session
        from database.models import HistoricalContext, HistoricalPattern

        # Batch-load contexts for these instances
        context_map = {}
        try:
            with get_session() as session:
                # Find pattern IDs for these instances
                for inst in instances:
                    pattern = (
                        session.query(HistoricalPattern)
                        .filter_by(
                            setup_type=inst.get("setup_type", ""),
                            source_ticker=inst.get("source_ticker", ""),
                            event_date=inst.get("event_date", ""),
                        )
                        .first()
                    )
                    if pattern:
                        ctx = session.query(HistoricalContext).filter_by(pattern_id=pattern.id).first()
                        if ctx:
                            key = (inst["source_ticker"], inst["event_date"])
                            context_map[key] = {
                                "fwd_pe": ctx.fwd_pe_ratio,
                                "macro_regime": ctx.macro_regime,
                                "vix": ctx.vix_level,
                                "momentum_20d": ctx.momentum_20d,
                            }
        except Exception as e:
            log.warning("context_load_failed", error=str(e))

        # Score each instance
        for inst in instances:
            key = (inst.get("source_ticker", ""), inst.get("event_date", ""))
            hist_context = context_map.get(key)

            if not hist_context:
                inst["similarity"] = 0.5  # Neutral when no context available
                continue

            inst["similarity"] = self._compute_similarity(current_context, hist_context, inst)

        return instances

    def _compute_similarity(self, current: dict, historical: dict, instance: dict) -> float:
        """
        Compute similarity score (0-1) between current context and a historical instance.

        5 dimensions:
        1. Valuation regime (forward P/E proximity) — weight 0.30
        2. Beat/catalyst magnitude — weight 0.20
        3. Macro regime match — weight 0.20
        4. Prior 20-day momentum proximity — weight 0.15
        5. Market sentiment / VIX proximity — weight 0.15
        """
        total_score = 0.0
        total_weight = 0.0

        # 1. Valuation regime (forward P/E)
        cur_pe = current.get("fwd_pe")
        hist_pe = historical.get("fwd_pe")
        if cur_pe and hist_pe and isinstance(cur_pe, (int, float)) and isinstance(hist_pe, (int, float)):
            if cur_pe > 0 and hist_pe > 0:
                pe_ratio = min(cur_pe, hist_pe) / max(cur_pe, hist_pe)
                pe_sim = pe_ratio ** 0.5  # Gentler penalty for moderate differences
                total_score += pe_sim * SIMILARITY_WEIGHTS["valuation"]
                total_weight += SIMILARITY_WEIGHTS["valuation"]

        # 2. Beat/catalyst magnitude
        cur_beat = current.get("beat_magnitude")
        hist_beat = instance.get("beat_magnitude")
        if cur_beat is not None and hist_beat is not None:
            try:
                cur_b = float(cur_beat)
                hist_b = float(hist_beat)
                if max(abs(cur_b), abs(hist_b)) > 0:
                    beat_diff = abs(cur_b - hist_b) / max(abs(cur_b), abs(hist_b), 1)
                    beat_sim = max(0, 1 - beat_diff)
                    total_score += beat_sim * SIMILARITY_WEIGHTS["beat_magnitude"]
                    total_weight += SIMILARITY_WEIGHTS["beat_magnitude"]
            except (ValueError, TypeError):
                pass

        # 3. Macro regime match
        cur_regime = current.get("macro_regime")
        hist_regime = historical.get("macro_regime")
        if cur_regime and hist_regime:
            if cur_regime == hist_regime:
                regime_sim = 1.0
            elif {cur_regime, hist_regime} in ({"risk-on", "neutral"}, {"neutral", "risk-off"}):
                regime_sim = 0.5  # Adjacent regimes
            else:
                regime_sim = 0.0  # Opposite regimes
            total_score += regime_sim * SIMILARITY_WEIGHTS["macro_regime"]
            total_weight += SIMILARITY_WEIGHTS["macro_regime"]

        # 4. Momentum proximity
        cur_mom = current.get("momentum_20d")
        hist_mom = historical.get("momentum_20d")
        if cur_mom is not None and hist_mom is not None:
            try:
                mom_diff = abs(float(cur_mom) - float(hist_mom))
                mom_sim = max(0, 1 - mom_diff / 30)  # 30% difference = 0 similarity
                total_score += mom_sim * SIMILARITY_WEIGHTS["momentum"]
                total_weight += SIMILARITY_WEIGHTS["momentum"]
            except (ValueError, TypeError):
                pass

        # 5. VIX proximity
        cur_vix = current.get("vix")
        hist_vix = historical.get("vix")
        if cur_vix is not None and hist_vix is not None:
            try:
                vix_diff = abs(float(cur_vix) - float(hist_vix))
                vix_sim = max(0, 1 - vix_diff / 25)  # 25 point VIX diff = 0 similarity
                total_score += vix_sim * SIMILARITY_WEIGHTS["vix"]
                total_weight += SIMILARITY_WEIGHTS["vix"]
            except (ValueError, TypeError):
                pass

        # Normalize: if we have partial data, scale by what we could compute
        if total_weight > 0:
            return round(total_score / total_weight, 3)
        return 0.5  # Default when no context dimensions available

    def _compute_weighted_stats(self, instances: list[dict]) -> dict:
        """
        Compute similarity-weighted summary statistics.
        Returns weighted win rate, weighted median return, and highly similar subset stats.
        """
        # Filter to instances with valid T+10 returns
        valid = [i for i in instances if i.get("return_t10") is not None]
        if not valid:
            return {}

        similarities = np.array([i.get("similarity", 0.5) for i in valid])
        returns_t10 = np.array([i["return_t10"] for i in valid])

        # Weighted win rate
        winners = (returns_t10 > 0).astype(float)
        if similarities.sum() > 0:
            weighted_win_rate = float(np.average(winners, weights=similarities))
            weighted_mean_return = float(np.average(returns_t10, weights=similarities))
        else:
            weighted_win_rate = float(np.mean(winners))
            weighted_mean_return = float(np.mean(returns_t10))

        # Weighted median approximation: sort by return, find weighted median
        sorted_indices = np.argsort(returns_t10)
        sorted_returns = returns_t10[sorted_indices]
        sorted_weights = similarities[sorted_indices]
        cum_weights = np.cumsum(sorted_weights)
        if cum_weights[-1] > 0:
            median_idx = np.searchsorted(cum_weights, cum_weights[-1] / 2)
            median_idx = min(median_idx, len(sorted_returns) - 1)
            weighted_median = float(sorted_returns[median_idx])
        else:
            weighted_median = float(np.median(returns_t10))

        # Highly similar subset stats
        highly_similar = [i for i in valid if i.get("similarity", 0) >= HIGH_SIMILARITY_THRESHOLD]
        hs_stats = {}
        if highly_similar:
            hs_returns = [i["return_t10"] for i in highly_similar]
            hs_winners = [r for r in hs_returns if r > 0]
            hs_stats = {
                "hs_count": len(highly_similar),
                "hs_win_rate_t10": round(len(hs_winners) / len(hs_returns), 3) if hs_returns else 0,
                "hs_median_return_t10": round(float(np.median(hs_returns)), 2),
            }

        return {
            "weighted_win_rate_t10": round(weighted_win_rate, 3),
            "weighted_median_return_t10": round(weighted_median, 2),
            "weighted_mean_return_t10": round(weighted_mean_return, 2),
            **hs_stats,
        }

    def _get_most_similar_instance(self, instances: list[dict]) -> dict:
        """Find the single most similar historical instance for memo display."""
        valid = [i for i in instances if i.get("similarity", 0) > 0.5 and i.get("return_t10") is not None]
        if not valid:
            return {}
        best = max(valid, key=lambda i: i.get("similarity", 0))
        return {
            "ticker": best.get("source_ticker", "?"),
            "event_date": best.get("event_date", "?"),
            "similarity": best.get("similarity", 0),
            "return_t10": best.get("return_t10", 0),
            "beat_magnitude": best.get("beat_magnitude"),
        }

    # ── Step 4: Scoring ──────────────────────────────────────────────

    def _compute_score(self, stats: dict) -> tuple[float, float, str]:
        """
        Compute score, confidence, and direction from summary stats.
        V2: Uses similarity-weighted win rate when available (prefers highly similar subset).
        """
        total = stats.get("total_instances", 0)

        # V2: Prefer highly similar subset stats, then weighted, then raw
        hs_count = stats.get("hs_count", 0)
        if hs_count >= 5:
            # Enough highly similar instances — use their stats directly
            win_rate = stats.get("hs_win_rate_t10", stats.get("win_rate_t10", 0.5))
            median = stats.get("hs_median_return_t10", stats.get("median_return_t10", 0))
            log.info("score_using_highly_similar", hs_count=hs_count, win_rate=win_rate)
        elif stats.get("weighted_win_rate_t10") is not None:
            # Use similarity-weighted stats across full sample
            win_rate = stats.get("weighted_win_rate_t10", 0.5)
            median = stats.get("weighted_median_return_t10", stats.get("median_return_t10", 0))
        else:
            # Fallback to raw equal-weighted stats
            win_rate = stats.get("win_rate_t10", 0.5)
            median = stats.get("median_return_t10", 0)

        # Base score from win rate
        base_score = win_rate

        # Sample size confidence adjustment
        # V2: Highly similar instances count more toward confidence
        effective_sample = total + hs_count * 0.5  # Bonus for having similar context
        if effective_sample < 5:
            confidence_adj = 0.5
        elif effective_sample < 10:
            confidence_adj = 0.7
        elif effective_sample < 20:
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

        # Direction from median return (using similarity-aware median)
        direction = "bullish" if median > 0 else "bearish" if median < -1 else "neutral"

        return round(score, 3), round(confidence, 3), direction

    # ── Step 5: Interpretation ───────────────────────────────────────

    def _interpret(self, ticker: str, setup_type: str, setup: dict, stats: dict) -> str:
        """Use Sonnet to interpret the statistical patterns (V2: includes similarity data)."""
        if not self.client:
            return self._fallback_interpretation(stats)

        # V2: Build similarity context section
        hs_count = stats.get("hs_count", 0)
        weighted_wr = stats.get("weighted_win_rate_t10")
        weighted_med = stats.get("weighted_median_return_t10")

        similarity_section = ""
        if weighted_wr is not None:
            similarity_section = (
                f"\nSIMILARITY-WEIGHTED STATS (instances weighted by context similarity to current setup):\n"
                f"  Weighted win rate: {weighted_wr:.0%}\n"
                f"  Weighted median return: {weighted_med:+.1f}%\n"
            )
            if hs_count > 0:
                hs_wr = stats.get("hs_win_rate_t10", 0)
                hs_med = stats.get("hs_median_return_t10", 0)
                similarity_section += (
                    f"  Highly similar instances: {hs_count}\n"
                    f"  Highly similar win rate: {hs_wr:.0%}\n"
                    f"  Highly similar median: {hs_med:+.1f}%\n"
                )
            similarity_section += (
                "  (Similarity considers: valuation regime, beat magnitude, macro regime, momentum, VIX level)\n"
            )

        prompt = (
            f"Interpret these historical pattern statistics for {ticker}.\n\n"
            f"SETUP TYPE: {setup_type}\n"
            f"TOTAL INSTANCES: {stats.get('total_instances', 0)} "
            f"(same ticker: {stats.get('same_ticker_count', 0)}, peers: {stats.get('peer_count', 0)})\n\n"
            f"EQUAL-WEIGHTED STATS (T+10 trading days):\n"
            f"  Win rate: {stats.get('win_rate_t10', 0):.0%}\n"
            f"  Median return: {stats.get('median_return_t10', 0):.1f}%\n"
            f"  Avg winner: +{stats.get('avg_winner_t10', 0):.1f}%\n"
            f"  Avg loser: {stats.get('avg_loser_t10', 0):.1f}%\n"
            f"{similarity_section}\n"
            f"DRAWDOWN:\n"
            f"  Median max drawdown: {stats.get('max_drawdown_median', 0):.1f}%\n"
            f"  Worst drawdown: {stats.get('max_drawdown_worst', 0):.1f}%\n\n"
            "Write 2-3 sentences interpreting what these historical analogs suggest for this trade. "
            "If similarity-weighted stats differ materially from equal-weighted, highlight the divergence "
            "and explain what it means (e.g., 'similar market conditions historically produced better/worse outcomes'). "
            "Consider sample size, consistency, typical drawdown path, and risk/reward."
        )

        try:
            model = get_model("pattern_interpret", self.settings)
            result = self.client.analyze(
                model,
                "You are interpreting historical market pattern data for a swing trade thesis.",
                prompt,
                max_tokens=250,
            )
            return result.strip()
        except Exception as e:
            log.error("interpretation_failed", ticker=ticker, error=str(e))
            return self._fallback_interpretation(stats)

    def _fallback_interpretation(self, stats: dict) -> str:
        """Generate a basic interpretation without Sonnet (V2: includes similarity data)."""
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

        base = (
            f"Across {total} historical instances, the setup showed a {win_rate:.0%} win rate "
            f"with a median T+10 return of {median:+.1f}%. "
            f"Typical max drawdown was {dd:.1f}%. {size_note}"
        )

        # V2: Add similarity context if available
        hs_count = stats.get("hs_count", 0)
        weighted_wr = stats.get("weighted_win_rate_t10")
        if weighted_wr is not None and abs(weighted_wr - win_rate) > 0.05:
            sim_note = (
                f" Similarity-weighted win rate is {weighted_wr:.0%}"
                f" ({hs_count} highly similar instances)."
            )
            base += sim_note

        return base
