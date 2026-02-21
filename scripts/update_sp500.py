"""
Fetch current S&P 500 constituents and update config/tickers.py.
Source: Wikipedia S&P 500 page (public, well-maintained table).
Run manually whenever rebalancing occurs (~quarterly).

Usage:
    python scripts/update_sp500.py
"""

import sys
from pathlib import Path
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    print("pandas is required. Install with: pip install pandas lxml")
    sys.exit(1)

# GICS Sector mapping (Wikipedia uses GICS sector names)
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


def fetch_sp500() -> dict:
    """Fetch S&P 500 list from Wikipedia."""
    import urllib.request
    from io import StringIO
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    print(f"Fetching S&P 500 from {url}...")
    # Wikipedia blocks requests without a User-Agent
    req = urllib.request.Request(url, headers={"User-Agent": "SwingTrader/2.0"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8")
    tables = pd.read_html(StringIO(html))
    df = tables[0]  # First table is current constituents

    universe = {}
    for _, row in df.iterrows():
        symbol = str(row["Symbol"]).strip()
        # yfinance uses - instead of . for class shares (BRK.B → BRK-B)
        symbol = symbol.replace(".", "-")
        sector = SECTOR_MAP.get(row["GICS Sector"], str(row["GICS Sector"]))
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
        f'Last updated: {datetime.now().strftime("%Y-%m-%d")}',
        'Sector assignments from GICS classification.',
        'Regenerate with: python scripts/update_sp500.py',
        '"""',
        '',
        'UNIVERSE = {',
    ]

    for sector in sorted(by_sector.keys()):
        tickers = sorted(by_sector[sector])
        lines.append(f'    # {sector} ({len(tickers)})')
        # Write ~4 tickers per line for readability
        for i in range(0, len(tickers), 4):
            chunk = tickers[i:i + 4]
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

    # Print summary
    print("\nSector breakdown:")
    by_sector_sorted = {k: by_sector[k] for k in sorted(by_sector.keys())}
    for sector, tickers in by_sector_sorted.items():
        print(f"  {sector}: {len(tickers)} tickers")


if __name__ == "__main__":
    universe = fetch_sp500()
    write_tickers_file(universe)
    print(f"\nS&P 500 universe updated: {len(universe)} tickers")
