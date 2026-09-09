"""The fixed 50-ticker list the SEC coverage checkpoint runs against.

Phase 3a's checkpoint is a coverage measurement, so the list it measures has to
be **frozen**. Re-deriving it from ``config.tickers.UNIVERSE`` on every run
would let an index reconstitution move the denominator, and two coverage
numbers taken a month apart would stop being comparable.

Derivation, recorded so it is reproducible rather than arbitrary: the S&P 500
seed in ``config/tickers.py`` (snapshot 2026-02-21) grouped by GICS sector,
sectors in alphabetical order, and the alphabetically first names taken
round-robin across all eleven sectors until fifty were collected. That gives
even sector spread and no discretion about which names are included.

The list is deliberately *not* checked against the live universe by a test. A
name that later leaves the index still belongs here: dropping it would be the
survivorship bias Spec N section 4.2 spends a page on.
"""

from __future__ import annotations

COVERAGE_UNIVERSE_50: tuple[str, ...] = (
    "CHTR",   # Communication Services
    "ABNB",   # Consumer Discretionary
    "ADM",    # Consumer Staples
    "APA",    # Energy
    "ACGL",   # Financials
    "A",      # Healthcare
    "ADP",    # Industrials
    "ALB",    # Materials
    "AMT",    # Real Estate
    "AAPL",   # Technology
    "AEE",    # Utilities
    "CMCSA",  # Communication Services
    "AMZN",   # Consumer Discretionary
    "BF-B",   # Consumer Staples
    "BKR",    # Energy
    "AFL",    # Financials
    "ABBV",   # Healthcare
    "ALLE",   # Industrials
    "AMCR",   # Materials
    "ARE",    # Real Estate
    "ACN",    # Technology
    "AEP",    # Utilities
    "DIS",    # Communication Services
    "APTV",   # Consumer Discretionary
    "BG",     # Consumer Staples
    "COP",    # Energy
    "AIG",    # Financials
    "ABT",    # Healthcare
    "AME",    # Industrials
    "APD",    # Materials
    "AVB",    # Real Estate
    "ADBE",   # Technology
    "AES",    # Utilities
    "EA",     # Communication Services
    "AZO",    # Consumer Discretionary
    "CAG",    # Consumer Staples
    "CTRA",   # Energy
    "AIZ",    # Financials
    "ALGN",   # Healthcare
    "AOS",    # Industrials
    "AVY",    # Materials
    "BXP",    # Real Estate
    "ADI",    # Technology
    "ATO",    # Utilities
    "FOX",    # Communication Services
    "BBY",    # Consumer Discretionary
    "CHD",    # Consumer Staples
    "CVX",    # Energy
    "AJG",    # Financials
    "AMGN",   # Healthcare
)
