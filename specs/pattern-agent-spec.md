# Pattern Agent — Implementation Spec

## Purpose

When the system generates a trade thesis (e.g., "NVDA earnings beat + guide up"), the Pattern Agent finds the most similar historical situations for the same ticker and its peers, measures what happened to share prices afterward, and feeds that empirical evidence into the scoring engine.

This is **empirical outcome analysis**, not technical analysis. We're asking "what happened in situations like this?" — not "what does the chart say."

---

## Architecture

```
Thesis generated (from Catalyst Agent)
        │
        ▼
┌─────────────────────────┐
│  1. Setup Classification │  ← Sonnet: classify the thesis into a setup type
│     (Sonnet)             │     e.g. "earnings_beat_guide_up", "insider_cluster_buy"
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  2. Historical Search    │  ← FMP/Alpha Vantage: find past instances of same setup
│     (Data Layer)         │     type for this ticker + peers
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  3. Forward Returns      │  ← yfinance: compute price changes at T+5, T+10, T+15, T+20
│     (Computation)        │     from each historical instance
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  4. Interpretation       │  ← Sonnet: interpret the statistical patterns —
│     (Sonnet)             │     "what do these analogs suggest for this trade?"
└───────────┬─────────────┘
            │
            ▼
        AgentOutput
        (score, confidence, direction, reasoning, raw_data)
```

---

## Step 1: Setup Classification (Sonnet)

**Input:** The catalyst agent's output (type, summary, magnitude, direction) + ticker context.

**Task:** Classify the current situation into a standardized setup type that can be matched historically.

**Setup type taxonomy:**

| Setup Type | Description | Historical Data Source |
|-----------|-------------|----------------------|
| `earnings_beat_guide_up` | EPS beat + raised guidance | FMP earnings surprises |
| `earnings_beat_guide_flat` | EPS beat, guidance unchanged | FMP earnings surprises |
| `earnings_beat_guide_down` | EPS beat but lowered guidance | FMP earnings surprises |
| `earnings_miss` | EPS miss (any guidance) | FMP earnings surprises |
| `revenue_acceleration` | QoQ revenue growth accelerating | FMP quarterly financials |
| `insider_cluster_buy` | Multiple insiders buying within 14 days | SEC Form 4 data |
| `analyst_upgrade_cluster` | 2+ analyst upgrades within 7 days | FMP analyst estimates |
| `analyst_downgrade` | Significant downgrade or PT cut | FMP analyst estimates |
| `buyback_announcement` | Share repurchase announced | Finnhub news (keyword) |
| `dividend_initiation` | New dividend or significant increase | FMP dividends |
| `sector_catalyst_positive` | Positive sector-wide event | Price data (sector ETF) |
| `sector_catalyst_negative` | Negative sector-wide event | Price data (sector ETF) |
| `product_launch` | Major product/service announcement | News (no structured data) |
| `regulatory_approval` | FDA, FCC, or other regulatory green light | News (no structured data) |
| `management_change` | CEO/CFO appointment or departure | News (no structured data) |
| `m_and_a` | Acquisition, merger, or activist involvement | News / SEC filings |
| `general_positive_catalyst` | Catch-all for positive catalysts that don't fit above | N/A |
| `general_negative_catalyst` | Catch-all for negative catalysts | N/A |

**Sonnet prompt:** Given the catalyst data, classify into one of these types. Also extract key parameters that define the setup (e.g., for `earnings_beat_guide_up`: beat magnitude %, guidance raise %). These parameters are used to find the closest historical matches.

**Output:**
```json
{
  "setup_type": "earnings_beat_guide_up",
  "setup_params": {
    "beat_magnitude_pct": 12.5,
    "guidance_raise_pct": 3.0
  },
  "search_strategy": "Use FMP earnings surprises for same ticker + peers. Filter to beats >5% with positive guidance."
}
```

---

## Step 2: Historical Search (Data Layer)

**For structured setup types** (earnings, insider, analyst, dividends):

Use FMP and/or Alpha Vantage APIs to pull historical event data. For example:
- `earnings_beat_guide_up`: FMP `/earnings-surprises/{ticker}` — filter to positive surprises, then cross-reference with guidance direction from earnings call data
- `insider_cluster_buy`: SEC EDGAR Form 4 historical data, or FMP `/insider-trading`
- `analyst_upgrade_cluster`: FMP `/analyst-estimates` or `/upgrades-downgrades`

**For unstructured setup types** (product launch, regulatory, M&A, management change):

These are harder to find historically from structured data. Two approaches:
1. **Same-ticker only:** Search our own catalyst database (`catalysts` table) for past instances where the same ticker had the same `catalyst_type`. This gets more valuable over time as the system accumulates data.
2. **Sector proxy:** When the catalyst is sector-level (e.g., regulatory change affecting all pharma), look at how sector ETF + peers responded to similar past sector events via price data patterns.

**Peer selection:**
- Use the existing `config/peers.py` peer mapping (3-5 peers per ticker)
- For tickers not in the peer map, fall back to same-sector tickers with similar market cap (within 0.5x-2x)

**Search parameters:**
- Lookback: 5 years of data (yfinance provides this for free)
- For structured events: match on setup type + rough parameter similarity (e.g., earnings beats >5%, not just any beat)
- Cap at 30 most recent instances (ticker + peers combined) to keep computation bounded

---

## Step 3: Forward Return Computation

For each historical instance found, compute:

```python
{
    "event_date": "2024-01-25",
    "ticker": "NVDA",  # or peer ticker
    "setup_type": "earnings_beat_guide_up",
    "beat_magnitude_pct": 8.3,
    # Forward returns from event date
    "return_t5": 3.2,     # % return at T+5 trading days
    "return_t10": 5.8,    # % return at T+10
    "return_t15": 4.1,    # % return at T+15
    "return_t20": 6.7,    # % return at T+20
    # Risk metrics
    "max_drawdown": -2.1, # worst intraday low vs event-day close
    "max_drawdown_day": 3, # which day the drawdown hit
    "days_to_recover": 1,  # days from drawdown to breakeven
}
```

**Implementation:** Use yfinance to pull daily OHLCV for each ticker around each event date. Compute returns relative to the event-day closing price.

**Summary statistics across all instances:**

```python
{
    "same_ticker_count": 6,
    "peer_count": 18,
    "total_instances": 24,
    # Stats at T+10 (primary horizon for swing trades)
    "median_return_t10": 4.2,
    "mean_return_t10": 3.8,
    "win_rate_t10": 0.75,  # % with positive return
    "avg_winner_t10": 7.1,
    "avg_loser_t10": -2.3,
    "max_drawdown_median": -3.5,
    "max_drawdown_worst": -8.2,
    # Same stats for T+5 and T+20
    ...
}
```

---

## Step 4: Interpretation (Sonnet)

Send the summary statistics to Sonnet with context about the current setup.

**Sonnet prompt:** "Given these historical outcomes for similar setups, what does this suggest for the current trade? Consider sample size, consistency of outcomes, typical drawdown path, and whether the current context differs meaningfully from historical instances."

**Output:** 2-3 sentences of interpretation that go into the IC memo under "HISTORICAL PRECEDENT."

---

## Scoring Logic

**Score (0-1.0) based on:**

```python
# Win rate drives the base score
base_score = win_rate_t10  # e.g., 0.75

# Adjust for sample size
if total_instances < 5:
    confidence_adjustment = 0.5  # heavy penalty
elif total_instances < 10:
    confidence_adjustment = 0.7
elif total_instances < 20:
    confidence_adjustment = 0.85
else:
    confidence_adjustment = 1.0

# Adjust for risk/reward quality
avg_winner = stats["avg_winner_t10"]
avg_loser = abs(stats["avg_loser_t10"])
if avg_loser > 0:
    rr_ratio = avg_winner / avg_loser
    rr_adjustment = min(1.2, max(0.7, rr_ratio / 2.0))  # normalize around 1.0
else:
    rr_adjustment = 1.1

score = base_score * confidence_adjustment * rr_adjustment
score = max(0.0, min(1.0, score))  # clamp to 0-1

# Confidence reflects sample size + consistency
confidence = confidence_adjustment * (1 - std_dev_of_returns / 20)  # normalize
```

**Direction:** Derived from median return. Positive median → bullish, negative → bearish.

---

## AgentOutput

```python
AgentOutput(
    agent_type="pattern",
    ticker=ticker,
    score=score,            # 0-1.0
    confidence=confidence,  # 0-1.0, heavily penalized for small samples
    direction=direction,    # bullish/bearish based on median return
    reasoning=sonnet_interpretation,  # Sonnet's 2-3 sentence interpretation
    raw_data={
        "setup_type": "earnings_beat_guide_up",
        "same_ticker_instances": 6,
        "peer_instances": 18,
        "same_ticker_stats": { ... },
        "peer_stats": { ... },
        "combined_stats": { ... },
        "sample_size_warning": total_instances < 10,
        "historical_instances": [ ... ],  # individual instance details
    }
)
```

---

## Data Requirements

| Data | Source | Free Tier Limits | Notes |
|------|--------|-----------------|-------|
| Historical earnings surprises | FMP `/earnings-surprises/{ticker}` | 250 calls/day shared | Primary structured data source |
| Historical price data (5yr) | yfinance | Unlimited, no key | Forward return computation |
| Peer mappings | `config/peers.py` | N/A (local) | Already exists |
| Insider trading history | FMP `/insider-trading` | Shared 250/day | For insider setups |
| Analyst estimate history | FMP `/analyst-estimates` | Shared 250/day | For upgrade/downgrade setups |
| Our own catalyst history | `catalysts` table in SQLite | N/A (local) | Grows over time |

**FMP rate limit strategy:** Cache aggressively. Historical earnings surprises for a ticker don't change — fetch once and store in SQLite. Only the "find instances" step hits FMP; the forward returns step uses yfinance (unlimited).

---

## New Database Table

```sql
CREATE TABLE historical_patterns (
    id INTEGER PRIMARY KEY,
    ticker_id INTEGER REFERENCES tickers(id),
    setup_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    source_ticker TEXT NOT NULL,  -- which ticker this instance is from (self or peer)
    is_peer BOOLEAN DEFAULT FALSE,
    beat_magnitude REAL,          -- setup-specific parameter
    return_t5 REAL,
    return_t10 REAL,
    return_t15 REAL,
    return_t20 REAL,
    max_drawdown REAL,
    max_drawdown_day INTEGER,
    raw_data TEXT,                -- JSON blob for additional setup params
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_patterns_lookup ON historical_patterns(setup_type, source_ticker);
```

---

## Files to Create / Modify

| File | Action | Description |
|------|--------|-------------|
| `agents/pattern_agent.py` | **Rewrite** | Replace 37-line stub with full implementation |
| `data/pattern_data.py` | **Create** | Data adapter for fetching historical events from FMP + yfinance |
| `database/models.py` | **Add** | `HistoricalPattern` SQLAlchemy model |
| `scoring/engine.py` | No change | Already wired to receive pattern AgentOutput |
| `memo/generator.py` | No change | Already includes pattern in memo assembly |
| `memo/templates/ic_memo.py` | **Minor update** | Render historical stats in memo if available |
| `config/peers.py` | No change | Already has peer mappings |

---

## Implementation Order

1. Add `HistoricalPattern` model to `database/models.py`
2. Create `data/pattern_data.py` — fetch earnings surprises from FMP, cache to DB
3. Rewrite `agents/pattern_agent.py`:
   a. Setup classification (Sonnet call)
   b. Historical search (query FMP data + our DB)
   c. Forward return computation (yfinance)
   d. Summary statistics
   e. Interpretation (Sonnet call)
   f. Return AgentOutput
4. Update `memo/templates/ic_memo.py` to render pattern stats
5. Test with `/test NVDA` (likely to have rich earnings history)

---

## Cost Estimate

Per ticker analysis:
- 1 Sonnet call for setup classification (~500 tokens) = ~$0.005
- 1 Sonnet call for interpretation (~1000 tokens) = ~$0.01
- FMP calls: 1-3 per ticker (cached after first fetch) = free after initial cache
- yfinance: unlimited

**Monthly estimate:** ~200 pattern analyses × $0.015 = ~$3/month additional Sonnet cost.

---

## Edge Cases

- **No historical instances found:** Return score 0.5, confidence 0.1, note "insufficient historical data for this setup type"
- **Only peer instances, no same-ticker:** Weight peer instances lower (0.7x) in scoring
- **Setup type is unstructured (product_launch, etc.):** Fall back to sector-level analysis or return low-confidence stub until our own catalyst DB has enough data
- **FMP rate limit hit:** Degrade gracefully — use whatever cached data exists, note reduced confidence
- **Very recent events only:** If all instances are from the last 6 months, flag "recency bias" in interpretation
