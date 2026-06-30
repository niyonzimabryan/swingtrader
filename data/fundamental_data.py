"""
Fundamental data adapter — yfinance (primary) + FMP (fallback).
Provides: income statement, balance sheet, cash flow, ratios, analyst estimates.
"""

import math
import httpx
import yfinance as yf
from utils.logger import get_logger
from utils.rate_limiter import rate_limiter
from utils.redaction import redact_text

log = get_logger("fundamental_data")

FMP_BASE = "https://financialmodelingprep.com/stable"


def _safe(val, default=0):
    """Return val if it's a finite number, else default."""
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


class FundamentalDataAdapter:
    def __init__(self, fmp_key: str = "", av_key: str = ""):
        self.fmp_key = fmp_key
        self.av_key = av_key
        self._yf_cache: dict[str, yf.Ticker] = {}

    def _get_stock(self, ticker: str) -> yf.Ticker:
        """Get a cached yfinance Ticker object."""
        if ticker not in self._yf_cache:
            self._yf_cache[ticker] = yf.Ticker(ticker)
        return self._yf_cache[ticker]

    # ── Income Statement ─────────────────────────────────────────────

    def get_income_statement(self, ticker: str, period: str = "quarter", limit: int = 8) -> list[dict]:
        """Fetch income statement data. yfinance primary, FMP fallback."""
        result = self._yf_income(ticker, period, limit)
        if result:
            return result
        # FMP fallback
        result = self._fmp_income(ticker, period, limit)
        return result

    def _yf_income(self, ticker: str, period: str, limit: int) -> list[dict]:
        try:
            stock = self._get_stock(ticker)
            df = stock.quarterly_income_stmt if period == "quarter" else stock.income_stmt
            if df is None or df.empty:
                return []

            result = []
            for col in df.columns[:limit]:
                revenue = _safe(df.at["Total Revenue", col]) if "Total Revenue" in df.index else 0
                gross_profit = _safe(df.at["Gross Profit", col]) if "Gross Profit" in df.index else 0
                operating_income = _safe(df.at["Operating Income", col]) if "Operating Income" in df.index else 0
                net_income = _safe(df.at["Net Income", col]) if "Net Income" in df.index else 0
                eps = _safe(df.at["Basic EPS", col]) if "Basic EPS" in df.index else 0
                eps_diluted = _safe(df.at["Diluted EPS", col]) if "Diluted EPS" in df.index else eps

                result.append({
                    "date": col.strftime("%Y-%m-%d"),
                    "revenue": revenue,
                    "gross_profit": gross_profit,
                    "gross_margin": gross_profit / revenue if revenue else 0,
                    "operating_income": operating_income,
                    "operating_margin": operating_income / revenue if revenue else 0,
                    "net_income": net_income,
                    "net_margin": net_income / revenue if revenue else 0,
                    "eps": eps,
                    "eps_diluted": eps_diluted,
                })
            log.info("yfinance_income_loaded", ticker=ticker, count=len(result))
            return result
        except Exception as e:
            log.error("yfinance_income_failed", ticker=ticker, error=str(e))
            return []

    def _fmp_income(self, ticker: str, period: str, limit: int) -> list[dict]:
        data = self._fmp_request("/income-statement", {"symbol": ticker, "period": period, "limit": limit})
        if not data and period == "quarter":
            log.info("fmp_fallback_annual", endpoint="income-statement", ticker=ticker)
            data = self._fmp_request("/income-statement", {"symbol": ticker, "period": "annual", "limit": 4})
        if not data:
            return []
        result = []
        for item in data:
            revenue = item.get("revenue", 0) or 0
            gross_profit = item.get("grossProfit", 0) or 0
            operating_income = item.get("operatingIncome", 0) or 0
            net_income = item.get("netIncome", 0) or 0
            result.append({
                "date": item.get("date"),
                "revenue": revenue,
                "gross_profit": gross_profit,
                "gross_margin": item.get("grossProfitRatio") or (gross_profit / revenue if revenue else 0),
                "operating_income": operating_income,
                "operating_margin": item.get("operatingIncomeRatio") or (operating_income / revenue if revenue else 0),
                "net_income": net_income,
                "net_margin": item.get("netIncomeRatio") or (net_income / revenue if revenue else 0),
                "eps": item.get("eps", 0) or 0,
                "eps_diluted": item.get("epsDiluted") or item.get("epsdiluted", 0) or 0,
            })
        return result

    # ── Balance Sheet ─────────────────────────────────────────────────

    def get_balance_sheet(self, ticker: str, period: str = "quarter", limit: int = 4) -> list[dict]:
        """Fetch balance sheet data. yfinance primary, FMP fallback."""
        result = self._yf_balance(ticker, period, limit)
        if result:
            return result
        return self._fmp_balance(ticker, period, limit)

    def _yf_balance(self, ticker: str, period: str, limit: int) -> list[dict]:
        try:
            stock = self._get_stock(ticker)
            df = stock.quarterly_balance_sheet if period == "quarter" else stock.balance_sheet
            if df is None or df.empty:
                return []

            result = []
            for col in df.columns[:limit]:
                total_assets = _safe(df.at["Total Assets", col]) if "Total Assets" in df.index else 0
                total_liabilities = _safe(df.at["Total Liabilities Net Minority Interest", col]) if "Total Liabilities Net Minority Interest" in df.index else 0
                total_equity = _safe(df.at["Stockholders Equity", col]) if "Stockholders Equity" in df.index else 0
                total_debt = _safe(df.at["Total Debt", col]) if "Total Debt" in df.index else 0
                cash = _safe(df.at["Cash And Cash Equivalents", col]) if "Cash And Cash Equivalents" in df.index else 0
                current_assets = _safe(df.at["Current Assets", col]) if "Current Assets" in df.index else 0
                current_liabilities = _safe(df.at["Current Liabilities", col]) if "Current Liabilities" in df.index else 0

                result.append({
                    "date": col.strftime("%Y-%m-%d"),
                    "total_assets": total_assets,
                    "total_liabilities": total_liabilities,
                    "total_equity": total_equity,
                    "total_debt": total_debt,
                    "cash": cash,
                    "current_assets": current_assets,
                    "current_liabilities": current_liabilities,
                    "current_ratio": current_assets / current_liabilities if current_liabilities > 0 else 0,
                    "debt_to_equity": total_debt / total_equity if total_equity > 0 else 0,
                })
            log.info("yfinance_balance_loaded", ticker=ticker, count=len(result))
            return result
        except Exception as e:
            log.error("yfinance_balance_failed", ticker=ticker, error=str(e))
            return []

    def _fmp_balance(self, ticker: str, period: str, limit: int) -> list[dict]:
        data = self._fmp_request("/balance-sheet-statement", {"symbol": ticker, "period": period, "limit": limit})
        if not data and period == "quarter":
            log.info("fmp_fallback_annual", endpoint="balance-sheet", ticker=ticker)
            data = self._fmp_request("/balance-sheet-statement", {"symbol": ticker, "period": "annual", "limit": 4})
        if not data:
            return []
        return [
            {
                "date": item.get("date"),
                "total_assets": item.get("totalAssets", 0),
                "total_liabilities": item.get("totalLiabilities", 0),
                "total_equity": item.get("totalStockholdersEquity", 0),
                "total_debt": item.get("totalDebt", 0),
                "cash": item.get("cashAndCashEquivalents", 0),
                "current_assets": item.get("totalCurrentAssets", 0),
                "current_liabilities": item.get("totalCurrentLiabilities", 0),
                "current_ratio": (
                    item.get("totalCurrentAssets", 0) / item.get("totalCurrentLiabilities", 1)
                    if item.get("totalCurrentLiabilities", 0) > 0 else 0
                ),
                "debt_to_equity": (
                    item.get("totalDebt", 0) / item.get("totalStockholdersEquity", 1)
                    if item.get("totalStockholdersEquity", 0) > 0 else 0
                ),
            }
            for item in data
        ]

    # ── Cash Flow ─────────────────────────────────────────────────────

    def get_cash_flow(self, ticker: str, period: str = "quarter", limit: int = 4) -> list[dict]:
        """Fetch cash flow statement. yfinance primary, FMP fallback."""
        result = self._yf_cashflow(ticker, period, limit)
        if result:
            return result
        return self._fmp_cashflow(ticker, period, limit)

    def _yf_cashflow(self, ticker: str, period: str, limit: int) -> list[dict]:
        try:
            stock = self._get_stock(ticker)
            df = stock.quarterly_cashflow if period == "quarter" else stock.cashflow
            if df is None or df.empty:
                return []

            result = []
            for col in df.columns[:limit]:
                ocf = _safe(df.at["Operating Cash Flow", col]) if "Operating Cash Flow" in df.index else 0
                capex = _safe(df.at["Capital Expenditure", col]) if "Capital Expenditure" in df.index else 0
                fcf = _safe(df.at["Free Cash Flow", col]) if "Free Cash Flow" in df.index else 0
                dividends = _safe(df.at["Cash Dividends Paid", col]) if "Cash Dividends Paid" in df.index else 0
                buyback = _safe(df.at["Repurchase Of Capital Stock", col]) if "Repurchase Of Capital Stock" in df.index else 0

                result.append({
                    "date": col.strftime("%Y-%m-%d"),
                    "operating_cash_flow": ocf,
                    "capex": capex,
                    "free_cash_flow": fcf if fcf else ocf + capex,  # capex is negative
                    "dividends_paid": dividends,
                    "share_buyback": buyback,
                })
            log.info("yfinance_cashflow_loaded", ticker=ticker, count=len(result))
            return result
        except Exception as e:
            log.error("yfinance_cashflow_failed", ticker=ticker, error=str(e))
            return []

    def _fmp_cashflow(self, ticker: str, period: str, limit: int) -> list[dict]:
        data = self._fmp_request("/cash-flow-statement", {"symbol": ticker, "period": period, "limit": limit})
        if not data and period == "quarter":
            log.info("fmp_fallback_annual", endpoint="cash-flow", ticker=ticker)
            data = self._fmp_request("/cash-flow-statement", {"symbol": ticker, "period": "annual", "limit": 4})
        if not data:
            return []
        return [
            {
                "date": item.get("date"),
                "operating_cash_flow": item.get("operatingCashFlow", 0),
                "capex": item.get("capitalExpenditure", 0),
                "free_cash_flow": item.get("freeCashFlow", 0),
                "dividends_paid": item.get("dividendsPaid", 0),
                "share_buyback": item.get("commonStockRepurchased", 0),
            }
            for item in data
        ]

    # ── Ratios ────────────────────────────────────────────────────────

    def get_ratios(self, ticker: str) -> dict:
        """Fetch key valuation ratios. yfinance primary, FMP fallback."""
        result = self._yf_ratios(ticker)
        if result.get("pe_ttm") or result.get("market_cap"):
            return result
        return self._fmp_ratios(ticker)

    def _yf_ratios(self, ticker: str) -> dict:
        try:
            stock = self._get_stock(ticker)
            info = stock.info or {}

            pe_ttm = _safe(info.get("trailingPE"))
            pe_forward = _safe(info.get("forwardPE")) or pe_ttm
            ev_ebitda = _safe(info.get("enterpriseToEbitda"))
            peg = _safe(info.get("pegRatio"))
            market_cap = _safe(info.get("marketCap"))
            ev = _safe(info.get("enterpriseValue"))

            # Compute P/FCF from market cap and FCF if available
            p_fcf = 0
            try:
                cf = stock.quarterly_cashflow
                if cf is not None and not cf.empty and "Free Cash Flow" in cf.index:
                    # Sum last 4 quarters for TTM FCF
                    ttm_fcf = sum(_safe(cf.at["Free Cash Flow", col]) for col in cf.columns[:4])
                    if ttm_fcf > 0 and market_cap > 0:
                        p_fcf = round(market_cap / ttm_fcf, 2)
            except Exception:
                pass

            result = {
                "pe_forward": pe_forward,
                "pe_ttm": pe_ttm,
                "ev_ebitda": ev_ebitda,
                "p_fcf": p_fcf,
                "peg": peg,
                "market_cap": market_cap,
                "enterprise_value": ev,
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "company_name": info.get("shortName", "") or info.get("longName", ""),
            }
            log.info("yfinance_ratios_loaded", ticker=ticker, pe_ttm=pe_ttm, ev_ebitda=ev_ebitda)
            return result
        except Exception as e:
            log.error("yfinance_ratios_failed", ticker=ticker, error=str(e))
            return {
                "pe_forward": 0, "pe_ttm": 0, "ev_ebitda": 0, "p_fcf": 0,
                "peg": 0, "market_cap": 0, "enterprise_value": 0,
            }

    def _fmp_ratios(self, ticker: str) -> dict:
        data = self._fmp_request("/key-metrics-ttm", {"symbol": ticker})
        profile = self._fmp_request("/profile", {"symbol": ticker})

        result = {
            "pe_forward": 0, "pe_ttm": 0, "ev_ebitda": 0, "p_fcf": 0,
            "peg": 0, "market_cap": 0, "enterprise_value": 0,
        }

        if data and isinstance(data, list) and data:
            metrics = data[0]
            pe_ttm = metrics.get("peRatioTTM", 0) or 0
            if not pe_ttm:
                ey = metrics.get("earningsYieldTTM", 0) or 0
                pe_ttm = round(1.0 / ey, 2) if ey > 0 else 0
            result.update({
                "pe_ttm": pe_ttm,
                "ev_ebitda": metrics.get("evToEBITDATTM", 0) or metrics.get("enterpriseValueOverEBITDATTM", 0) or 0,
                "p_fcf": metrics.get("pfcfRatioTTM", 0) or (
                    round(1.0 / metrics["freeCashFlowYieldTTM"], 2) if metrics.get("freeCashFlowYieldTTM", 0) else 0
                ),
                "peg": metrics.get("pegRatioTTM", 0) or 0,
                "market_cap": metrics.get("marketCap", 0) or metrics.get("marketCapTTM", 0) or 0,
                "enterprise_value": metrics.get("enterpriseValueTTM", 0) or 0,
            })

        if profile and isinstance(profile, list) and profile:
            p = profile[0]
            result["pe_forward"] = p.get("peRatio", 0) or result["pe_ttm"]
            result["market_cap"] = p.get("marketCap", 0) or p.get("mktCap", result["market_cap"]) or 0
            result["sector"] = p.get("sector", "")
            result["industry"] = p.get("industry", "")
            result["company_name"] = p.get("companyName", "")

        return result

    # ── Analyst Estimates ─────────────────────────────────────────────

    def get_analyst_estimates(self, ticker: str) -> dict:
        """Fetch analyst consensus estimates. yfinance primary, FMP fallback."""
        result = self._yf_estimates(ticker)
        if result:
            return result
        return self._fmp_estimates(ticker)

    def _yf_estimates(self, ticker: str) -> dict:
        try:
            stock = self._get_stock(ticker)
            # yfinance has analyst_price_targets and earnings_estimate
            targets = stock.analyst_price_targets
            if targets is not None and not targets.empty:
                return {
                    "estimated_revenue": 0,  # Not available via this endpoint
                    "estimated_eps": 0,
                    "number_of_analysts": int(_safe(targets.get("numberOfAnalysts", 0))),
                }
            return {}
        except Exception:
            return {}

    def _fmp_estimates(self, ticker: str) -> dict:
        data = self._fmp_request("/analyst-estimates", {"symbol": ticker, "period": "annual", "limit": 4})
        if not data:
            return {}
        latest = data[0] if data else {}
        return {
            "estimated_revenue": latest.get("revenueAvg", 0),
            "estimated_eps": latest.get("epsAvg", 0),
            "number_of_analysts": latest.get("numAnalystsRevenue", 0),
        }

    # ── Sector PE ─────────────────────────────────────────────────────

    def get_sector_pe(self, sector: str) -> float:
        """Get approximate sector median P/E (uses sector ETFs as proxy)."""
        sector_tickers = {
            "Technology": "XLK", "Financials": "XLF", "Healthcare": "XLV",
            "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
            "Industrials": "XLI", "Energy": "XLE", "Communication Services": "XLC",
            "Utilities": "XLU", "Real Estate": "XLRE", "Materials": "XLB",
        }
        etf = sector_tickers.get(sector)
        if not etf:
            return 20.0

        # Try yfinance first
        try:
            info = yf.Ticker(etf).info or {}
            pe = _safe(info.get("trailingPE"))
            if pe > 0:
                return pe
        except Exception:
            pass

        # FMP fallback
        data = self._fmp_request("/profile", {"symbol": etf})
        if data and isinstance(data, list) and data:
            return data[0].get("peRatio", 20.0) or 20.0
        return 20.0

    # ── FMP Request Helper ────────────────────────────────────────────

    def _fmp_request(self, endpoint: str, params: dict = None) -> list | dict | None:
        """Make a rate-limited request to FMP API."""
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            url = f"{FMP_BASE}{endpoint}"
            p = {"apikey": self.fmp_key}
            if params:
                p.update(params)
            resp = httpx.get(url, params=p, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("fmp_request_failed", endpoint=endpoint, error=redact_text(str(e)))
            return None
