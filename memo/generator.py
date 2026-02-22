"""
IC Memo Generator — assembles all signal data into a structured memo.
V2: Web research replaces sentiment. Includes web research in narratives.
Calls Sonnet for thesis + bear case, computes trade parameters.
"""

import json
from datetime import datetime
from agents.base_agent import AgentOutput
from data.market_data import MarketDataAdapter
from database.db import get_session
from database.models import Memo, Ticker
from memo.templates.ic_memo import format_memo_plain
from scoring.weights import CONVICTION_MULTIPLIERS
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("memo_generator")


class MemoGenerator:
    def __init__(self, settings, anthropic_client=None):
        self.settings = settings
        self.client = anthropic_client
        self.market_data = MarketDataAdapter()

    def generate(
        self,
        ticker: str,
        scoring_result: dict,
        catalyst: AgentOutput,
        fundamental: AgentOutput,
        pattern: AgentOutput,
        web_research: AgentOutput,
        regime: dict,
    ) -> dict:
        """Generate a full IC memo. Returns memo data dict and persists to DB."""
        log.info("memo_generation_start", ticker=ticker)

        # Get current price for trade parameters
        price_data = self.market_data.get_current_price(ticker)
        current_price = price_data.get("price", 0)
        atr = self.market_data.get_atr(ticker)

        if current_price <= 0:
            log.error("no_price_for_memo", ticker=ticker)
            return {}

        # Compute trade parameters
        trade_params = self._compute_trade_params(
            current_price, atr, regime,
            scoring_result.get("final_score", 0),
            scoring_result.get("classification", "moderate"),
        )

        # Generate thesis and bear case via Sonnet
        thesis = ""
        bear_case = ""
        if self.client:
            thesis, bear_case = self._generate_narratives(
                ticker, catalyst, fundamental, pattern, web_research, regime, scoring_result
            )

        # Assemble memo data
        memo_data = {
            "ticker": ticker,
            "direction": "long" if scoring_result.get("direction", "bullish") == "bullish" else (
                "short" if scoring_result.get("direction") == "bearish" else "long"  # Phase 1 default: long
            ),
            "direction_raw": scoring_result.get("direction", "bullish"),
            "composite_score": scoring_result.get("final_score", 0),
            "classification": scoring_result.get("classification", "unknown"),
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "thesis": thesis,
            "bear_case": bear_case,
            "catalyst": {**catalyst.raw_data, "confidence": catalyst.confidence, "direction": catalyst.direction},
            "fundamental": fundamental.raw_data,
            "pattern": {**pattern.raw_data, "confidence": pattern.confidence, "direction": pattern.direction, "reasoning": pattern.reasoning},
            "web_research": web_research.raw_data,
            "regime": regime,
            "trade_params": trade_params,
            "signal_breakdown": scoring_result.get("signal_breakdown", {}),
            "signal_agreement": scoring_result.get("signal_agreement", "unknown"),
            "opus_evaluation": scoring_result.get("opus_evaluation", {}),
            "risk_analysis": catalyst.raw_data.get("risk_analysis", {}),
        }

        # Save to DB
        memo_id = self._save_memo(memo_data)
        memo_data["memo_id"] = memo_id

        log.info("memo_generated", ticker=ticker, memo_id=memo_id, score=scoring_result.get("final_score"))
        return memo_data

    def _compute_trade_params(self, price: float, atr: float, regime: dict,
                               score: float, classification: str) -> dict:
        """Compute entry, stop-loss, targets, position size."""
        # Entry: slight buffer above current price for limit order
        entry_price = round(price * 1.001, 2)

        # Stop-loss: max of default_stop and 2*ATR
        atr_stop_pct = (2 * atr / price) if price > 0 and atr > 0 else self.settings.default_stop_loss_pct
        stop_pct = max(self.settings.default_stop_loss_pct, atr_stop_pct)
        stop_pct = min(stop_pct, self.settings.max_stop_loss_pct)
        stop_loss = round(entry_price * (1 - stop_pct), 2)

        # Targets: 2:1 and 3:1 risk/reward
        target_1 = round(entry_price * (1 + 2 * stop_pct), 2)
        target_2 = round(entry_price * (1 + 3 * stop_pct), 2)
        risk_reward = 2.0  # Minimum 2:1 by construction

        # Position sizing
        regime_multiplier = regime.get("position_size_multiplier", 1.0)
        conviction_multiplier = CONVICTION_MULTIPLIERS.get(classification, 1.0)

        # Volatility adjustment: reduce size for high-vol names
        vol_adjustment = 1.0
        if atr > 0 and price > 0:
            atr_pct = atr / price
            if atr_pct > 0.03:  # >3% daily range = high vol
                vol_adjustment = 0.7
            elif atr_pct > 0.02:
                vol_adjustment = 0.85

        position_pct = self.settings.base_position_pct * regime_multiplier * conviction_multiplier * vol_adjustment
        position_pct = max(self.settings.min_position_pct, min(self.settings.max_position_pct, position_pct))

        dollar_amount = self.settings.portfolio_value * position_pct
        shares = int(dollar_amount / entry_price) if entry_price > 0 else 0

        return {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "stop_pct": round(stop_pct * 100, 1),
            "target_1": target_1,
            "target_1_pct": round((target_1 / entry_price - 1) * 100, 1),
            "target_2": target_2,
            "target_2_pct": round((target_2 / entry_price - 1) * 100, 1),
            "risk_reward": round(risk_reward, 1),
            "position_pct": round(position_pct * 100, 1),
            "dollar_amount": round(dollar_amount, 2),
            "shares": shares,
            "max_hold_days": self.settings.max_holding_days,
            "regime_multiplier": regime_multiplier,
            "conviction_multiplier": conviction_multiplier,
            "vol_adjustment": vol_adjustment,
        }

    def _generate_narratives(self, ticker, catalyst, fundamental, pattern, web_research, regime, scoring_result):
        """Use Sonnet to draft thesis and bear case."""
        try:
            model = get_model("memo_draft", self.settings)
            opus_eval = scoring_result.get("opus_evaluation", {})

            # V2: Include web research key finding in narrative context
            web_key = web_research.raw_data.get("key_finding", "") if web_research else ""
            web_synthesis = web_research.reasoning if web_research else ""

            prompt = (
                f"You are drafting an IC (Investment Committee) memo for a swing trade on {ticker}.\n\n"
                f"CATALYST: {catalyst.reasoning}\n"
                f"FUNDAMENTALS: {fundamental.reasoning}\n"
                f"WEB RESEARCH: {web_synthesis[:300]}\n"
                f"KEY FINDING: {web_key}\n"
                f"MACRO REGIME: {regime.get('regime', 'neutral')}\n"
                f"COMPOSITE SCORE: {scoring_result.get('final_score', 0):.2f}\n"
                f"OPUS ASSESSMENT: {opus_eval.get('reasoning', 'N/A')}\n"
                f"OPUS STRESS TEST: {opus_eval.get('stress_test', 'N/A')}\n"
                f"KEY RISK: {opus_eval.get('key_risk', 'N/A')}\n\n"
                "Write two sections:\n"
                "1. THESIS: 2-3 sentences summarizing why this is a compelling swing trade right now. "
                "Incorporate the most relevant web research finding.\n"
                "2. BEAR_CASE: 2-3 sentences on what could go wrong, incorporating Opus's stress test.\n\n"
                'Respond with JSON: {"thesis": "...", "bear_case": "..."}'
            )

            result = self.client.analyze_json(
                model,
                "You are a senior equity analyst writing concise trade memos.",
                prompt,
                max_tokens=500,
            )
            return result.get("thesis", ""), result.get("bear_case", "")
        except Exception as e:
            log.error("narrative_generation_failed", ticker=ticker, error=str(e))
            return "Thesis generation failed.", "Bear case generation failed."

    def _save_memo(self, memo_data: dict) -> int:
        """Persist memo to database. Returns memo ID."""
        try:
            with get_session() as session:
                ticker_obj = session.query(Ticker).filter_by(symbol=memo_data["ticker"]).first()
                if not ticker_obj:
                    return 0
                memo = Memo(
                    ticker_id=ticker_obj.id,
                    composite_score=memo_data.get("composite_score", 0),
                    classification=memo_data.get("classification", ""),
                    direction=memo_data.get("direction", "long"),
                    full_text=format_memo_plain(memo_data),
                    trade_params=json.dumps(memo_data.get("trade_params", {})),
                    signal_breakdown=json.dumps(memo_data.get("signal_breakdown", {})),
                    opus_critique=json.dumps(memo_data.get("opus_evaluation", {})),
                    memo_data_json=json.dumps(memo_data),
                    thesis=memo_data.get("thesis", ""),
                    bear_case=memo_data.get("bear_case", ""),
                    status="pending",
                )
                session.add(memo)
                session.flush()
                memo_id = memo.id
                return memo_id
        except Exception as e:
            log.error("save_memo_failed", error=str(e))
            return 0
