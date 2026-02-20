"""
Ticker universe for Phase 1 — S&P 100 equivalent (~100 names).
Sector assignments for exposure tracking.
"""

UNIVERSE = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Technology", "META": "Technology", "AVGO": "Technology",
    "ADBE": "Technology", "CRM": "Technology", "AMD": "Technology",
    "INTC": "Technology", "CSCO": "Technology", "ORCL": "Technology",
    "QCOM": "Technology", "TXN": "Technology", "NOW": "Technology",
    "IBM": "Technology", "AMAT": "Technology", "MU": "Technology",
    "PLTR": "Technology", "PANW": "Technology",
    "CRWD": "Technology", "SHOP": "Technology", "ROKU": "Communication Services",
    # Financials
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "BLK": "Financials", "SCHW": "Financials",
    "AXP": "Financials",
    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "PFE": "Healthcare", "ABBV": "Healthcare", "MRK": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "AMGN": "Healthcare",
    "BMY": "Healthcare",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "LOW": "Consumer Discretionary", "TJX": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    # Consumer Staples
    "PG": "Consumer Staples", "KO": "Consumer Staples",
    "PEP": "Consumer Staples", "COST": "Consumer Staples",
    "WMT": "Consumer Staples", "PM": "Consumer Staples",
    "CL": "Consumer Staples", "MDLZ": "Consumer Staples",
    # Industrials
    "CAT": "Industrials", "HON": "Industrials", "UPS": "Industrials",
    "BA": "Industrials", "GE": "Industrials", "RTX": "Industrials",
    "DE": "Industrials", "LMT": "Industrials", "UNP": "Industrials",
    "MMM": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy",
    # Communication Services
    "GOOG": "Communication Services", "DIS": "Communication Services",
    "NFLX": "Communication Services", "CMCSA": "Communication Services",
    "T": "Communication Services", "VZ": "Communication Services",
    # Utilities
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    # Real Estate
    "AMT": "Real Estate", "PLD": "Real Estate", "SPG": "Real Estate",
    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials",
}

# Sector ETFs for macro regime analysis
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLC": "Communication Services",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
}
