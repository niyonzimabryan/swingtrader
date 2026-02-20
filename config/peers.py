"""
Peer group mappings — 3-5 nearest peers per ticker.
Used by Fundamental Agent for relative valuation.
Seeded for high-priority names; auto-generated for the rest via sector + market cap.
"""

PEER_GROUPS = {
    # Mega-cap Tech
    "AAPL": ["MSFT", "GOOGL", "AMZN", "META"],
    "MSFT": ["AAPL", "GOOGL", "AMZN", "CRM"],
    "GOOGL": ["META", "MSFT", "AMZN", "NFLX"],
    "META": ["GOOGL", "NFLX", "PINS", "SNAP"],
    "AMZN": ["MSFT", "GOOGL", "WMT", "COST"],
    # Semiconductors
    "NVDA": ["AMD", "AVGO", "INTC", "QCOM", "MU"],
    "AMD": ["NVDA", "INTC", "QCOM", "MU"],
    "AVGO": ["QCOM", "TXN", "NVDA", "AMAT"],
    "INTC": ["AMD", "NVDA", "TXN", "QCOM"],
    "QCOM": ["AVGO", "TXN", "AMD", "MRVL"],
    "MU": ["WDC", "NVDA", "AMD", "INTC"],
    "AMAT": ["LRCX", "KLAC", "ASML", "TER"],
    "TXN": ["AVGO", "QCOM", "ADI", "MCHP"],
    # Software
    "CRM": ["NOW", "ADBE", "ORCL", "WDAY"],
    "ADBE": ["CRM", "NOW", "INTU", "ANSS"],
    "NOW": ["CRM", "ADBE", "WDAY", "SNOW"],
    "ORCL": ["CRM", "IBM", "SAP", "MSFT"],
    "PLTR": ["SNOW", "DDOG", "NOW", "CRWD"],
    "PANW": ["CRWD", "ZS", "FTNT", "S"],
    "CRWD": ["PANW", "ZS", "FTNT", "S"],
    "SHOP": ["WIX", "BIGC", "SQ", "AMZN"],
    "ROKU": ["NFLX", "DIS", "PARA", "FUBO"],
    # Banks
    "JPM": ["BAC", "WFC", "GS", "MS"],
    "BAC": ["JPM", "WFC", "C", "USB"],
    "WFC": ["BAC", "JPM", "USB", "PNC"],
    "GS": ["MS", "JPM", "SCHW", "BLK"],
    "MS": ["GS", "JPM", "SCHW", "BLK"],
    # Payments
    "V": ["MA", "PYPL", "AXP", "SQ"],
    "MA": ["V", "PYPL", "AXP", "SQ"],
    # Pharma
    "LLY": ["NVO", "MRK", "ABBV", "PFE"],
    "JNJ": ["PFE", "MRK", "ABBV", "BMY"],
    "PFE": ["MRK", "JNJ", "BMY", "ABBV"],
    "ABBV": ["LLY", "MRK", "BMY", "JNJ"],
    "MRK": ["PFE", "LLY", "ABBV", "BMY"],
    "AMGN": ["GILD", "BIIB", "REGN", "VRTX"],
    # Healthcare services
    "UNH": ["ELV", "CI", "HUM", "CNC"],
    "TMO": ["DHR", "ABT", "A", "IQV"],
    "ABT": ["TMO", "DHR", "MDT", "BSX"],
    # Consumer
    "TSLA": ["GM", "F", "RIVN", "NIO"],
    "HD": ["LOW", "WMT", "COST", "TGT"],
    "NKE": ["LULU", "UAA", "DECK", "ON"],
    "MCD": ["SBUX", "CMG", "YUM", "QSR"],
    "COST": ["WMT", "TGT", "BJ", "AMZN"],
    # Energy
    "XOM": ["CVX", "COP", "EOG", "SLB"],
    "CVX": ["XOM", "COP", "EOG", "PXD"],
    # Industrials
    "CAT": ["DE", "CMI", "PCAR", "AGCO"],
    "BA": ["LMT", "RTX", "GD", "NOC"],
    "HON": ["MMM", "EMR", "ROK", "ETN"],
}


def get_peers(ticker: str) -> list[str]:
    """Get peer group for a ticker. Falls back to sector-based matching."""
    if ticker in PEER_GROUPS:
        return PEER_GROUPS[ticker]
    return []
