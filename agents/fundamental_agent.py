"""
Fundamental Agent — scores business quality, balance sheet, valuation, and growth.
Mostly deterministic math from financial data. Uses Sonnet only for narrative synthesis.
"""

import json
from datetime import date
from agents.base_agent import BaseAgent, AgentOutput
from data.fundamental_data import FundamentalDataAdapter
from data.market_data import MarketDataAdapter
from config.peers import get_peers
from database.db import get_session
from database.models import FundamentalData, Ticker
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("fundamental_agent")


class FundamentalAgent(BaseAgent):
    agent_type = "fundamental"

    def __init__(self, settings, anthropic_client=None):
        super().__init__(settings, anthropic_client)
        self.fund_data = FundamentalDataAdapter(settings.fmp_api_key, settings.alpha_vantage_api_key)
        self.market_data = MarketDataAdapter()

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Score a ticker's fundamental backdrop."""
        if not ticker:
            return AgentOutput(agent_type=self.agent_type, reasoning="No ticker provided")

        log.info("fundamental_analysis_start", ticker=ticker)

        # Fetch data
        income = self.fund_data.get_income_statement(ticker, limit=4)  # FMP free plan caps at 4
        balance = self.fund_data.get_balance_sheet(ticker, limit=4)
        cashflow = self.fund_data.get_cash_flow(ticker, limit=4)
        ratios = self.fund_data.get_ratios(ticker)
        sector = ratios.get("sector", kwargs.get("sector", "Technology"))

        if not income:
            return AgentOutput(
                agent_type=self.agent_type, ticker=ticker,
                score=0.5, confidence=0.2,
                reasoning="Insufficient financial data available.",
            )

        # Score each dimension (0.0 - 1.0)
        quality = self._score_quality(income, cashflow)
        balance_health = self._score_balance_sheet(balance)
        valuation = self._score_valuation(ratios, sector)
        growth = self._score_growth(income)

        # Composite: quality 30%, balance sheet 20%, valuation 30%, growth 20%
        composite = quality * 0.30 + balance_health * 0.20 + valuation * 0.30 + growth * 0.20

        # Detect flags
        flags = self._detect_flags(income, balance, ratios, quality, balance_health, valuation, growth)

        # Peer comparison narrative via Sonnet (if client available)
        peer_comparison = ""
        if self.client:
            peers = get_peers(ticker)
            peer_comparison = self._generate_peer_narrative(ticker, ratios, peers, sector)

        # Build reasoning
        reasoning = (
            f"Quality: {quality:.2f} | Balance Sheet: {balance_health:.2f} | "
            f"Valuation: {valuation:.2f} | Growth: {growth:.2f} | "
            f"Composite: {composite:.2f}. "
        )
        if flags:
            reasoning += f"Flags: {', '.join(flags)}. "
        if peer_comparison:
            reasoning += peer_comparison

        # Direction based on composite
        if composite >= 0.65:
            direction = "bullish"
        elif composite <= 0.35:
            direction = "bearish"
        else:
            direction = "neutral"

        # Save to DB
        self._save_fundamental(ticker, quality, balance_health, valuation, growth, composite, flags, peer_comparison, reasoning, ratios)

        log.info(
            "fundamental_result", ticker=ticker,
            quality=quality, balance=balance_health,
            valuation=valuation, growth=growth, composite=composite,
        )

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=composite,
            confidence=0.8 if income and balance else 0.4,
            direction=direction,
            reasoning=reasoning,
            raw_data={
                "quality_score": round(quality, 3),
                "balance_sheet_score": round(balance_health, 3),
                "valuation_score": round(valuation, 3),
                "growth_score": round(growth, 3),
                "composite_score": round(composite, 3),
                "flags": flags,
                "peer_comparison": peer_comparison,
                "ratios": ratios,
            },
            run_id=self.run_id,
        )

    def _score_quality(self, income: list, cashflow: list) -> float:
        """Business quality: margins, FCF conversion, revenue consistency."""
        if not income:
            return 0.5
        score = 0.0
        weights = 0.0

        # Gross margin (higher is better, normalize 0-60% → 0-1)
        latest = income[0]
        gm = latest.get("gross_margin", 0) or 0
        score += min(1.0, gm / 0.60) * 0.35
        weights += 0.35

        # Operating margin (higher is better, normalize 0-40% → 0-1)
        om = latest.get("operating_margin", 0) or 0
        score += min(1.0, max(0, om) / 0.40) * 0.25
        weights += 0.25

        # Margin trend (improving or stable = good)
        if len(income) >= 4:
            recent_gm = sum(q.get("gross_margin", 0) or 0 for q in income[:2]) / 2
            older_gm = sum(q.get("gross_margin", 0) or 0 for q in income[2:4]) / 2
            if recent_gm >= older_gm:
                score += 0.20 * 1.0
            else:
                score += 0.20 * 0.3
            weights += 0.20

        # FCF conversion
        if cashflow:
            revenue = latest.get("revenue", 1) or 1
            fcf = cashflow[0].get("free_cash_flow", 0) or 0
            fcf_margin = fcf / revenue if revenue > 0 else 0
            score += min(1.0, max(0, fcf_margin / 0.25)) * 0.20
            weights += 0.20

        return score / weights if weights > 0 else 0.5

    def _score_balance_sheet(self, balance: list) -> float:
        """Balance sheet health: leverage, liquidity, coverage."""
        if not balance:
            return 0.5
        latest = balance[0]
        score = 0.0

        # Current ratio (>1.5 good, <1.0 bad)
        cr = latest.get("current_ratio", 1.5) or 1.5
        if cr >= 2.0:
            score += 0.35
        elif cr >= 1.5:
            score += 0.25
        elif cr >= 1.0:
            score += 0.15
        else:
            score += 0.05

        # Debt to equity (<0.5 great, >2.0 concerning)
        de = latest.get("debt_to_equity", 1.0) or 1.0
        if de < 0.3:
            score += 0.35
        elif de < 0.7:
            score += 0.25
        elif de < 1.5:
            score += 0.15
        elif de < 2.5:
            score += 0.08
        else:
            score += 0.02

        # Cash position (cash / total debt)
        cash = latest.get("cash", 0) or 0
        debt = latest.get("total_debt", 1) or 1
        cash_ratio = cash / debt if debt > 0 else 2.0
        if cash_ratio >= 1.0:
            score += 0.30
        elif cash_ratio >= 0.5:
            score += 0.20
        elif cash_ratio >= 0.2:
            score += 0.10
        else:
            score += 0.03

        return min(1.0, score)

    def _score_valuation(self, ratios: dict, sector: str) -> float:
        """Valuation attractiveness: relative to own history and peers."""
        pe = ratios.get("pe_forward") or ratios.get("pe_ttm", 25)
        ev_ebitda = ratios.get("ev_ebitda", 15) or 15
        peg = ratios.get("peg", 1.5) or 1.5

        # Get sector median PE for comparison
        sector_pe = self.fund_data.get_sector_pe(sector)

        score = 0.0

        # P/E relative to sector (lower = more attractive)
        if pe > 0:
            pe_ratio = pe / sector_pe if sector_pe > 0 else 1.0
            if pe_ratio < 0.7:
                score += 0.35  # Cheap vs sector
            elif pe_ratio < 1.0:
                score += 0.25
            elif pe_ratio < 1.3:
                score += 0.15
            elif pe_ratio < 2.0:
                score += 0.08
            else:
                score += 0.02
        else:
            score += 0.15  # Negative PE — neutral

        # EV/EBITDA (lower = cheaper, typical range 8-25)
        if ev_ebitda > 0:
            if ev_ebitda < 10:
                score += 0.35
            elif ev_ebitda < 15:
                score += 0.25
            elif ev_ebitda < 20:
                score += 0.15
            elif ev_ebitda < 30:
                score += 0.08
            else:
                score += 0.02
        else:
            score += 0.15

        # PEG ratio (lower = better growth-adjusted value)
        if 0 < peg < 0.8:
            score += 0.30
        elif peg < 1.2:
            score += 0.22
        elif peg < 1.8:
            score += 0.12
        elif peg < 3.0:
            score += 0.05
        else:
            score += 0.02

        return min(1.0, score)

    def _score_growth(self, income: list) -> float:
        """Growth trajectory: revenue and EPS trends."""
        if len(income) < 4:
            return 0.5
        score = 0.0

        # Revenue growth YoY (compare Q0 to Q4)
        recent_rev = income[0].get("revenue", 0) or 0
        year_ago_rev = income[3].get("revenue", 0) or 0 if len(income) > 3 else 0
        if year_ago_rev > 0 and recent_rev > 0:
            rev_growth = (recent_rev - year_ago_rev) / year_ago_rev
            if rev_growth > 0.30:
                score += 0.40
            elif rev_growth > 0.15:
                score += 0.30
            elif rev_growth > 0.05:
                score += 0.20
            elif rev_growth > 0:
                score += 0.12
            elif rev_growth > -0.05:
                score += 0.05
            else:
                score += 0.02
        else:
            score += 0.15

        # EPS growth
        recent_eps = income[0].get("eps_diluted", 0) or 0
        year_ago_eps = income[3].get("eps_diluted", 0) or 0 if len(income) > 3 else 0
        if year_ago_eps > 0 and recent_eps > 0:
            eps_growth = (recent_eps - year_ago_eps) / abs(year_ago_eps)
            if eps_growth > 0.30:
                score += 0.35
            elif eps_growth > 0.15:
                score += 0.25
            elif eps_growth > 0.05:
                score += 0.15
            elif eps_growth > 0:
                score += 0.08
            else:
                score += 0.02
        else:
            score += 0.12

        # Acceleration check: is growth accelerating or decelerating?
        if len(income) >= 6:
            recent_q_rev = income[0].get("revenue", 0) or 0
            prev_q_rev = income[1].get("revenue", 0) or 0
            two_q_rev = income[2].get("revenue", 0) or 0
            if prev_q_rev > 0 and two_q_rev > 0:
                recent_seq = (recent_q_rev - prev_q_rev) / prev_q_rev
                prev_seq = (prev_q_rev - two_q_rev) / two_q_rev
                if recent_seq > prev_seq:
                    score += 0.25  # Accelerating
                else:
                    score += 0.10  # Decelerating
            else:
                score += 0.10
        else:
            score += 0.10

        return min(1.0, score)

    def _detect_flags(self, income, balance, ratios, quality, balance_health, valuation, growth) -> list[str]:
        """Detect warning flags."""
        flags = []
        if balance and balance[0].get("debt_to_equity", 0) > 2.0:
            flags.append("high_debt")
        if income and len(income) >= 2:
            if (income[0].get("gross_margin", 0) or 0) < (income[1].get("gross_margin", 0) or 0):
                flags.append("margin_compression")
        if growth > 0.7:
            flags.append("accelerating_growth")
        if valuation < 0.25:
            flags.append("expensive_valuation")
        if valuation > 0.75:
            flags.append("attractive_valuation")
        if balance_health < 0.3:
            flags.append("weak_balance_sheet")
        return flags

    def _generate_peer_narrative(self, ticker: str, ratios: dict, peers: list, sector: str) -> str:
        """Use Sonnet to generate a peer comparison narrative."""
        if not self.client or not peers:
            return ""
        try:
            # Fetch peer ratios
            peer_data = {}
            for peer in peers[:3]:  # Limit to 3 to save API calls
                pr = self.fund_data.get_ratios(peer)
                if pr:
                    peer_data[peer] = {
                        "pe": pr.get("pe_forward") or pr.get("pe_ttm", 0),
                        "ev_ebitda": pr.get("ev_ebitda", 0),
                        "market_cap": pr.get("market_cap", 0),
                    }

            model = get_model("peer_comparison", self.settings)
            prompt = (
                f"Compare {ticker}'s valuation to its peers in 2-3 sentences.\n\n"
                f"{ticker}: P/E={ratios.get('pe_forward', 'N/A')}, "
                f"EV/EBITDA={ratios.get('ev_ebitda', 'N/A')}, "
                f"Market Cap=${ratios.get('market_cap', 0):,.0f}\n\n"
                f"Peers:\n"
            )
            for p, d in peer_data.items():
                prompt += f"  {p}: P/E={d['pe']}, EV/EBITDA={d['ev_ebitda']}, Market Cap=${d['market_cap']:,.0f}\n"
            prompt += f"\nSector: {sector}"

            result = self.client.analyze(
                model, "You are a concise equity research analyst. Keep it to 2-3 sentences.",
                prompt, max_tokens=200,
            )
            return result.strip()
        except Exception as e:
            log.error("peer_narrative_failed", ticker=ticker, error=str(e))
            return ""

    def _save_fundamental(self, ticker, quality, balance_health, valuation, growth, composite, flags, peer_comparison, reasoning, ratios):
        """Persist fundamental scores to database."""
        try:
            with get_session() as session:
                ticker_obj = session.query(Ticker).filter_by(symbol=ticker).first()
                if not ticker_obj:
                    return
                today = date.today()
                existing = (
                    session.query(FundamentalData)
                    .filter_by(ticker_id=ticker_obj.id, as_of_date=today)
                    .first()
                )
                if existing:
                    existing.quality_score = quality
                    existing.balance_sheet_score = balance_health
                    existing.valuation_score = valuation
                    existing.growth_score = growth
                    existing.composite_score = composite
                    existing.flags = json.dumps(flags)
                    existing.peer_comparison = peer_comparison
                    existing.reasoning = reasoning
                    existing.raw_data = json.dumps(ratios, default=str)
                else:
                    entry = FundamentalData(
                        ticker_id=ticker_obj.id,
                        as_of_date=today,
                        quality_score=quality,
                        balance_sheet_score=balance_health,
                        valuation_score=valuation,
                        growth_score=growth,
                        composite_score=composite,
                        flags=json.dumps(flags),
                        peer_comparison=peer_comparison,
                        reasoning=reasoning,
                        raw_data=json.dumps(ratios, default=str),
                    )
                    session.add(entry)
        except Exception as e:
            log.error("save_fundamental_failed", ticker=ticker, error=str(e))
