# Swing Trading Agent System — Product Requirements Document

## Document Purpose

This PRD serves as the source of truth for building an automated swing trading system. It is designed to be consumed by both human developers and AI coding agents (Claude Code). When building, refer to this document for architectural decisions, scope boundaries, and acceptance criteria.

---

## 1. Vision & Objective

Build an agentic system that identifies, evaluates, and executes swing trades on US mid-to-mega cap equities. The system blends quantitative signals with qualitative AI-driven analysis to generate trade theses, manage positions, and improve over time through a closed feedback loop.

**Core thesis:** Alpha comes from the intersection of catalyst identification, fundamental validation, and disciplined execution — not from any single signal in isolation.

**Success metric for Phase 1:** Generate paper trading track record over 60+ days with a documented Sharpe ratio and clear attribution of which signal layers contributed to winners vs. losers.

---

## 2. Phased Roadmap

### Phase 1 — Foundation (This Build)
- Catalyst scanner + fundamental scorer
- Paper trading via Alpaca
- All trades generate IC memos delivered to the operator
- No autonomous execution
- Performance tracking and signal attribution logging
- Historical pattern matching for thesis support
- Macro regime classification (simple)
- Reddit sentiment as supplementary signal

### Phase 2 — Selective Autonomy
- Graduate high-confidence, well-validated setup types to autonomous execution
- Tighten IC memo threshold (only novel/ambiguous setups need approval)
- Introduce position management automation (trailing stops, scaling)
- Backtest framework for validating new signal ideas against historical data

### Phase 3 — Expansion
- Real capital deployment (small, graduated)
- Additional asset classes or prediction markets
- Multi-strategy support
- Portfolio-level optimization

### Phase 4+ — Reinforcement Learning & Adaptive Intelligence
- Reward-weighted signal calibration (contextual bandits on trade outcomes)
- Position sizing and exit timing optimization (full RL)
- Fine-tuned judgment models from operator decision history
- Adversarial thesis stress-testing via self-play
- Constitutional trading principles as RL regularizer

**This PRD scopes Phase 1 only. Phases 2-3 are directional. Phase 4+ is a detailed roadmap (Section 13) with data collection requirements that must be baked into Phase 1.**

---

## 3. System Architecture

### 3.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     ORCHESTRATOR                         │
│         (Scheduler + Signal Aggregator + Router)         │
└──────┬──────┬──────┬──────┬──────┬──────┬───────────────┘
       │      │      │      │      │      │
       ▼      ▼      ▼      ▼      ▼      ▼
   ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐┌──────────┐
   │Macro ││Cata- ││Funda-││Hist. ││Reddit││ Scoring  │
   │Regime││lyst  ││mental││Match ││Senti-││ Engine   │
   │Agent ││Agent ││Agent ││Agent ││ment  ││          │
   └──────┘└──────┘└──────┘└──────┘└──────┘└────┬─────┘
                                                 │
                                          ┌──────▼──────┐
                                          │  Decision    │
                                          │  Router      │
                                          └──┬───────┬───┘
                                             │       │
                                    ┌────────▼┐  ┌───▼──────────┐
                                    │IC Memo  │  │Auto-Execute  │
                                    │Pipeline │  │(Phase 2)     │
                                    └────┬────┘  └──────────────┘
                                         │
                                    ┌────▼────┐
                                    │Alpaca   │
                                    │Execution│
                                    └────┬────┘
                                         │
                                    ┌────▼────────┐
                                    │ Performance  │
                                    │ Tracker      │
                                    └──────────────┘
```

### 3.2 Orchestrator

The orchestrator is the central scheduler and coordinator. It:

- Runs on a configurable schedule (default: 3x daily — pre-market 7:00 AM ET, midday 12:00 PM ET, post-market 5:00 PM ET)
- Triggers each signal agent independently
- Collects outputs and passes them to the Scoring Engine
- Routes scored opportunities to the appropriate action (IC memo or log-only)
- Maintains the universe of tracked tickers

**Ticker Universe:**
- US equities, market cap floor of $2B (captures mid-cap without going into micro-cap noise)
- No hard ceiling on market cap
- Exclude ADRs, SPACs, and recent IPOs (< 6 months public) in Phase 1
- Universe should be refreshable — pull from a screener or index membership list periodically
- Expect 800-1,500 tickers in scope

**TWO-WAY DOOR:** Scheduling mechanism — cron job, APScheduler, Celery, or cloud-native scheduler (CloudWatch, Cloud Scheduler). Choose based on deployment environment. For local dev, a simple cron or APScheduler is fine.

**DECIDED:** Deployment — local machine for Phase 1. Plan migration to cloud (EC2, Railway, or Render) when moving to Phase 2 autonomous execution, where uptime reliability matters. Design with this migration in mind — use environment variables, avoid hardcoded paths, containerize early (Dockerfile from day 1 even if running locally).

### 3.3 Data Layer

All agents read from a shared data layer. This avoids redundant API calls and provides a single source of truth.

**Storage:**

- SQLite for Phase 1 (simple, zero-config, good enough for the data volumes here)
- Schema should be designed to migrate to PostgreSQL later if needed

**Core tables (conceptual):**

- `tickers` — universe of tracked symbols, sector, market cap, metadata
- `price_data` — OHLCV daily bars, cached from market data API
- `catalysts` — detected catalysts with type, source, timestamp, raw text
- `fundamentals` — quarterly financials, valuation metrics, cached/refreshed quarterly
- `signals` — output from each agent per ticker per run (score, confidence, reasoning)
- `trades` — all trade decisions (entry, exit, P&L, setup type, signal attribution)
- `memos` — full IC memo text, approval status, operator notes
- `macro_regime` — daily regime classification with inputs
- `reddit_sentiment` — ticker-level sentiment snapshots

**TWO-WAY DOOR:** Database choice. SQLite is the recommendation for Phase 1. If deploying to cloud with multiple workers in Phase 2, migrate to PostgreSQL.

### 3.4 API Integrations

| Service | Purpose | Free Tier | Paid Tier (Future) | Notes |
|---------|---------|-----------|---------------------|-------|
| Alpaca | Paper trading execution | Yes (paper) | $0 for live trading | Primary broker API |
| Finnhub | News, earnings calendar, company news | 60 calls/min | $49/mo for faster + more data | Good catalyst source |
| Financial Modeling Prep (FMP) | Fundamentals, SEC filings, financial statements | 250 calls/day | $29/mo for 750/day | Best free fundamental data |
| Alpha Vantage | Backup financial data, earnings | 25 calls/day | $50/mo for 75/min | Rate limited but solid backup |
| FRED (St. Louis Fed) | Macro indicators | Unlimited | N/A | Fed funds, yield curve, etc. |
| Yahoo Finance (yfinance) | Price data, quick fundamentals | Unofficial, no key needed | N/A | Reliable for daily OHLCV |
| SEC EDGAR | Raw filings (13F, Form 4, 8-K, 10-Q) | Unlimited | N/A | Requires parsing |
| Reddit API / PRAW | Subreddit scanning | Free with app registration | N/A | Rate limited |
| Anthropic API | Agent intelligence layer | Pay per token | Same | See model tiering strategy below |
| Telegram Bot API | Memo delivery + operator interaction | Free | N/A | Primary operator interface |

**Phase 1 target: $0 data costs + ~$15-40/mo Anthropic API.** If the system proves out and we need faster/more data, the paid tiers of Finnhub + FMP (~$80/mo combined) unlock meaningfully more capacity. Design adapters so this is a config change, not a code change.

**TWO-WAY DOOR:** Specific data provider choices. The system should use an adapter pattern so data sources can be swapped without changing agent logic. For example, if FMP's free tier runs out, switch to Alpha Vantage for the same data type.

**Important:** All API keys should be stored in environment variables or a `.env` file, never hardcoded. Use a config module that reads from env.

---

## 4. Signal Agents — Detailed Specifications

### 4.1 Macro Regime Agent

**Purpose:** Classify the current market environment to adjust portfolio-level risk parameters. This agent does NOT generate trade ideas.

**Inputs:**
- Federal funds rate (current + futures-implied trajectory) — FRED
- US Treasury yield curve (2Y/10Y spread) — FRED
- VIX level and 1-month vs. 3-month term structure — Yahoo Finance
- Investment-grade credit spreads (OAS) — FRED
- S&P 500 and Russell 2000 50-day vs. 200-day moving average relationship — Yahoo Finance
- Sector ETF relative performance (30-day momentum across XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU) — Yahoo Finance

**Output:**
```json
{
  "regime": "risk-on" | "neutral" | "risk-off",
  "confidence": 0.0-1.0,
  "position_size_multiplier": 0.5-1.5,
  "max_concurrent_positions": 3-8,
  "reasoning": "string — brief explanation of classification"
}
```

**Logic:**
- Use a simple rules-based scoring system, not ML. Each indicator contributes a score.
- VIX < 16 and declining = risk-on signal. VIX > 25 and rising = risk-off signal.
- Yield curve inverted = caution. Yield curve steepening from inversion = potential risk-on.
- Credit spreads widening = risk-off. Narrowing = risk-on.
- Broad market above 200-day MA = risk-on. Below = risk-off.
- Classify based on majority of signals. Require strong consensus for risk-on or risk-off; default to neutral.

**Run frequency:** Once daily, pre-market.

**Risk parameters by regime:**

| Regime | Position Size Multiplier | Max Concurrent Positions | Max Portfolio Exposure |
|--------|--------------------------|--------------------------|----------------------|
| Risk-on | 1.0-1.5x | 6-8 | 80% |
| Neutral | 0.75-1.0x | 4-6 | 60% |
| Risk-off | 0.5-0.75x | 2-4 | 40% |

---

### 4.2 Catalyst Agent

**Purpose:** Scan for actionable catalysts that could drive price moves in the near term (1-20 trading days). This is the primary trade idea generator.

**Catalyst types to detect (ranked by typical signal strength):**

1. **Earnings surprises** — beat/miss magnitude, guidance revision direction, full-year raise/lower
2. **Insider buying clusters** — multiple insiders buying within 2 weeks, especially C-suite (Form 4 via EDGAR)
3. **Analyst revisions** — upgrades/downgrades, price target changes, estimate revisions (especially clusters of revisions)
4. **M&A activity** — acquisition announcements, activist involvement (13D filings), takeover rumors from credible sources
5. **Product/regulatory catalysts** — FDA approvals, major product launches, regulatory decisions, contract wins
6. **Management changes** — CEO/CFO turnover, board shakeups
7. **Capital allocation events** — buyback announcements, dividend initiations/increases, significant debt paydown
8. **Sector/macro catalysts** — tariff changes, regulation shifts, commodity price moves affecting specific sectors

**Inputs:**
- Finnhub company news feed (per ticker in universe, filtered to last 48 hours)
- SEC EDGAR recent filings (8-K, Form 4, 13D, SC13D)
- Earnings calendar (Finnhub or FMP) — pre-scan upcoming earnings within 5 trading days
- FMP or Alpha Vantage analyst estimate revisions

**Processing (Claude API):**

For each detected catalyst, call the Anthropic API with:
- The raw catalyst text (article, filing excerpt, data point)
- Company context (sector, recent price action, upcoming events)
- Prompt: classify catalyst type, assess magnitude (1-5), estimate expected price impact direction and range, assess timing (immediate vs. slow burn), flag any ambiguity or counter-arguments

**Output per catalyst:**
```json
{
  "ticker": "AAPL",
  "catalyst_type": "earnings_surprise",
  "catalyst_summary": "string — 2-3 sentence summary",
  "magnitude": 1-5,
  "direction": "bullish" | "bearish" | "ambiguous",
  "expected_impact_pct": {"low": -2.0, "mid": 5.0, "high": 10.0},
  "time_horizon_days": 10,
  "confidence": 0.0-1.0,
  "raw_source": "string — source URL or filing ID",
  "detected_at": "ISO timestamp",
  "reasoning": "string — detailed analysis"
}
```

**Filtering:** Only pass catalysts with magnitude >= 3 and confidence >= 0.6 to the scoring engine. Log everything else for pattern analysis.

**Run frequency:** 3x daily (each orchestrator cycle).

**Token optimization:** Pre-filter catalysts with keyword/regex matching before sending to Claude. Don't spend API tokens analyzing routine press releases or minor analyst notes. Only escalate to Claude when the catalyst matches a relevance heuristic (mentions earnings, guidance, insider, upgrade, FDA, acquisition, activist, buyback, etc.).

---

### 4.3 Fundamental Agent

**Purpose:** Validate that a catalyst-driven trade thesis is supported by underlying business quality and valuation. This agent scores the fundamental backdrop — it doesn't generate trade ideas on its own.

**Inputs (per ticker, refreshed quarterly + on catalyst trigger):**
- Income statement: revenue, gross margin, operating margin, net income (TTM and last 4 quarters)
- Balance sheet: total debt, cash, current ratio, debt/equity
- Cash flow: operating cash flow, free cash flow, capex as % of revenue
- Valuation: P/E (forward and TTM), EV/EBITDA, P/FCF, PEG ratio
- Growth: revenue growth (YoY, QoQ), EPS growth, FCF growth
- Relative valuation: current multiples vs. 3-year average, vs. sector median, vs. closest 5 peers

**Scoring dimensions:**

1. **Business quality (0-1):** Margin stability/expansion, FCF conversion, revenue consistency
2. **Balance sheet health (0-1):** Leverage, liquidity, interest coverage
3. **Valuation attractiveness (0-1):** Where current valuation sits relative to own history and peers. Lower relative valuation = higher score for long trades.
4. **Growth trajectory (0-1):** Accelerating vs. decelerating topline and bottom line

**Output:**
```json
{
  "ticker": "AAPL",
  "quality_score": 0.0-1.0,
  "balance_sheet_score": 0.0-1.0,
  "valuation_score": 0.0-1.0,
  "growth_score": 0.0-1.0,
  "composite_fundamental_score": 0.0-1.0,
  "flags": ["high_debt", "margin_compression", "accelerating_growth"],
  "peer_comparison": "string — brief relative positioning",
  "reasoning": "string — key fundamental takeaways"
}
```

**Implementation:** Most of this can be computed deterministically from financial data (no Claude API needed). Use Claude only for the peer comparison narrative and flag interpretation. This saves significant token costs.

**Peer mapping:** Maintain a lookup table of 3-5 nearest peers per ticker (by sector, market cap, business model). This can be seeded manually for high-priority names and auto-generated using SIC codes + market cap bands for the broader universe.

---

### 4.4 Historical Pattern Match Agent

**Purpose:** Provide empirical context for a trade thesis by examining how the stock (and peers) behaved in similar historical setups.

**Trigger:** Runs only when a catalyst has been detected and scored. Not a continuous scanner.

**Methodology:**

1. Identify the setup type (e.g., "earnings beat > 10% + guide up" or "insider buying cluster > $1M")
2. Search historical data for the same ticker: find past instances of the same setup type
3. Search peer group: find instances of the same setup type among the 3-5 closest peers
4. For each historical instance, compute forward returns at T+5, T+10, T+15, T+20 trading days
5. Compute summary statistics: median return, win rate (% positive), average winner, average loser, max drawdown before eventual gain

**Inputs:**
- Price data (at least 5 years of daily OHLCV) — Yahoo Finance
- Historical catalyst log (built over time as the system runs — this gets more valuable with age)
- For bootstrapping: use earnings surprise data from FMP/Alpha Vantage to reconstruct historical earnings setups

**Output:**
```json
{
  "ticker": "AAPL",
  "setup_type": "earnings_beat_guide_up",
  "same_ticker_instances": 6,
  "same_ticker_stats": {
    "median_return_t10": 4.2,
    "win_rate_t10": 0.83,
    "avg_winner": 7.1,
    "avg_loser": -2.3,
    "max_drawdown_before_gain": -3.5
  },
  "peer_instances": 18,
  "peer_stats": {
    "median_return_t10": 3.1,
    "win_rate_t10": 0.72,
    "avg_winner": 5.8,
    "avg_loser": -3.1
  },
  "confidence": 0.0-1.0,
  "sample_size_warning": true | false,
  "reasoning": "string — interpretation of patterns"
}
```

**Confidence calibration:** If combined sample size (ticker + peers) is < 10, flag low confidence. Pattern match should never be the primary signal with sparse data — it's confirmatory.

**Important:** This agent explicitly does NOT do traditional technical analysis (RSI, MACD, Bollinger Bands, etc.). It does empirical outcome analysis for specific setup types. The distinction matters — we're asking "what happened in similar situations" not "what does the chart pattern say."

---

### 4.5 Reddit Sentiment Agent

**Purpose:** Gauge retail sentiment as a supplementary/contrarian signal. Low weight in the scoring model.

**Subreddits to monitor:**
- r/wallstreetbets (high volume, momentum/meme signal)
- r/stocks (more moderate retail sentiment)
- r/investing (conservative retail sentiment)
- r/options (unusual options activity discussion)
- Sector-specific: r/semiconductors, r/biotech, r/energy, r/technology (add based on where your universe concentrates)

**What to measure:**
- Mention volume (ticker mentions per day, normalized against baseline)
- Sentiment polarity (bullish/bearish/neutral classification of posts and top comments)
- Momentum of sentiment (is it shifting? newly bullish or newly bearish?)
- Quality filter: weight posts with higher upvotes and engagement more heavily

**Processing:**
- Use PRAW to pull posts and top-level comments mentioning tickers in the universe
- Pre-filter to posts with >10 upvotes to reduce noise
- Batch ticker mentions and send to Claude API for sentiment classification (batch multiple tickers per call to save tokens)

**Output:**
```json
{
  "ticker": "NVDA",
  "mention_volume": "high" | "normal" | "low",
  "mention_volume_zscore": 2.3,
  "sentiment": "bullish" | "bearish" | "mixed" | "neutral",
  "sentiment_shift": "newly_bullish" | "increasingly_bearish" | "stable" | "reversing",
  "contrarian_flag": true | false,
  "reasoning": "string — summary of retail narrative"
}
```

**Contrarian logic:** If sentiment is extremely bullish (z-score > 2.5) AND the stock has already moved significantly in that direction, flag as contrarian (potential mean reversion). Conversely, if sentiment is extremely negative on a fundamentally sound name with a positive catalyst, that's a potential opportunity.

**Run frequency:** Once daily (post-market). Reddit sentiment doesn't change fast enough to justify more frequent scanning.

---

## 5. Scoring Engine

### 5.1 Signal Aggregation

The Scoring Engine receives outputs from all agents and computes a composite score for each opportunity.

**Weight allocation:**

| Signal Layer | Weight | Rationale |
|-------------|--------|-----------|
| Catalyst strength | 35% | Primary trade trigger — no catalyst, no trade |
| Fundamental support | 30% | Validates the thesis has substance |
| Historical pattern match | 20% | Empirical confirmation of setup |
| Reddit sentiment | 15% | Supplementary/contrarian color |

**Macro regime is not weighted** — it acts as a multiplier on position sizing, not on the trade score itself.

**Composite score calculation:**

```
raw_score = (catalyst_score × 0.35) + (fundamental_score × 0.30) + (pattern_score × 0.20) + (sentiment_score × 0.15)

# Sentiment score flipped for contrarian setups
if contrarian_flag:
    sentiment_contribution = (1 - sentiment_score) × 0.15
else:
    sentiment_contribution = sentiment_score × 0.15

# Direction alignment check
if not all signals agree on direction:
    apply confidence penalty (reduce by 15-25% based on degree of disagreement)

final_score = raw_score × direction_alignment_modifier
```

**Score interpretation:**

| Score Range | Classification | Action |
|-------------|---------------|--------|
| 0.75 - 1.00 | High conviction | Generate IC memo, flag as priority |
| 0.55 - 0.74 | Moderate conviction | Generate IC memo, standard priority |
| 0.40 - 0.54 | Low conviction | Log only, add to watchlist |
| 0.00 - 0.39 | No action | Log and discard |

### 5.2 Signal Disagreement Handling

When signals conflict (e.g., strong catalyst but weak fundamentals), the system should:

1. Flag the disagreement explicitly in the IC memo
2. Note which signals agree and disagree
3. Reduce position size recommendation
4. Present the bull and bear case separately

This is especially important for the operator's learning — understanding *why* signals disagree is where investment intuition develops.

---

## 6. IC Memo System

### 6.1 Memo Structure

Every trade idea scoring >= 0.55 generates a structured Investment Committee memo delivered to the operator.

**Memo template:**

```
TRADE IDEA: [TICKER] — [DIRECTION] — [SETUP TYPE]
Generated: [timestamp]
Composite Score: [X.XX] — [Classification]

═══════════════════════════════════════════════

THESIS (2-3 sentences)
[AI-generated trade thesis in plain language]

CATALYST
Type: [catalyst_type]
Summary: [catalyst_summary]
Magnitude: [X/5] | Confidence: [X.XX]
Time horizon: [X] trading days
Source: [source link]

FUNDAMENTAL BACKDROP
Quality: [X.XX] | Valuation: [X.XX] | Growth: [X.XX] | Balance Sheet: [X.XX]
Key metrics: [2-3 most relevant data points]
Peer positioning: [1-2 sentences]
Flags: [any warning flags]

HISTORICAL PRECEDENT
Same-ticker instances: [N] | Peer instances: [N]
Median forward return (T+10): [X.X%] | Win rate: [XX%]
Typical drawdown before gain: [X.X%]
[1-2 sentence interpretation]

SENTIMENT
Reddit: [bullish/bearish/mixed] | Volume: [high/normal/low]
Contrarian flag: [yes/no]
[1 sentence summary]

MACRO CONTEXT
Regime: [risk-on/neutral/risk-off]
Position size multiplier: [X.Xx]

═══════════════════════════════════════════════

TRADE PARAMETERS (SUGGESTED)
Direction: LONG / SHORT
Entry: [suggested entry price or "at market"]
Stop-loss: [price] ([X.X%] from entry)
Target 1: [price] ([X.X%] from entry)
Target 2: [price] ([X.X%] from entry)
Position size: [X.X%] of portfolio ($[X,XXX])
Risk/reward ratio: [X.X:1]
Max holding period: [X] trading days

═══════════════════════════════════════════════

SIGNAL AGREEMENT
[Visual indicator of which signals align/disagree]
Catalyst: ✅ Bullish | Fundamental: ✅ Supportive | Pattern: ⚠️ Mixed | Sentiment: ✅ Confirming

BEAR CASE
[2-3 sentences on what could go wrong]

═══════════════════════════════════════════════
```

### 6.2 Delivery

**DECIDED:** Primary interface is a Telegram bot. Secondary backup via email. Memos also persisted to database for the dashboard (Phase 2).

Telegram is the right call — it's instant, works on mobile, supports rich formatting (Markdown), and the bot API is simple. Critically, it also enables two-way interaction: you can approve/reject trades, query portfolio status, and test ad-hoc trade ideas directly from your phone.

### 6.3 Telegram Bot — Primary Operator Interface

**Bot capabilities (Phase 1):**

1. **Receive IC memos** — formatted with the full memo template, delivered as soon as scoring completes
2. **Approve/reject trades** — inline keyboard buttons on each memo (Approve / Modify / Reject / Watchlist)
3. **Portfolio status** — command to see current positions, P&L, exposure
4. **Test a trade idea** — operator sends a ticker + thesis, system runs it through the full scoring pipeline and returns an ad-hoc memo
5. **Daily digest** — morning summary of regime, watchlist movers, upcoming catalysts
6. **Alerts** — risk management triggers (drawdown warnings, stop-loss hits, position exits)
7. **Manual overrides** — close positions, adjust stops, force-exit
8. **System diagnostics** — check agent health, last run times, error rates
9. **Replay and review** — pull up past memos and trades for review
10. **Natural language queries** — ask questions about the portfolio or market in plain English

**Bot commands:**

| Command | Action |
|---------|--------|
| `/status` | Portfolio dashboard — positions, total P&L, cash, net exposure %, daily P&L, regime |
| `/positions` | Detailed open positions — ticker, entry, current price, P&L, stop, days held, setup type |
| `/regime` | Current macro regime with all input indicators and reasoning |
| `/watchlist` | Active watchlist with latest composite scores and catalysts |
| `/test [TICKER] [thesis]` | Run full ad-hoc analysis pipeline, return scored memo |
| `/score [TICKER]` | Quick fundamental + sentiment snapshot without full catalyst analysis |
| `/performance` | Performance summary — total return, Sharpe, win rate, profit factor, best/worst trades |
| `/performance [period]` | Filtered performance — `/performance 7d`, `/performance 30d`, `/performance mtd` |
| `/history` | Recent trade log — last 10 trades with P&L and setup type |
| `/history [TICKER]` | All trades for a specific ticker |
| `/memo [ID]` | Retrieve a specific past memo by ID |
| `/close [TICKER]` | Manually close a position (with confirmation) |
| `/adjust [TICKER] stop [price]` | Adjust stop-loss on an open position |
| `/adjust [TICKER] target [price]` | Adjust profit target on an open position |
| `/pause` | Pause all new scanning and memo generation. Existing positions continue to be monitored. |
| `/resume` | Resume normal operation |
| `/agents` | Agent health check — last run time, success/fail, error count per agent |
| `/config [param] [value]` | View or update config params (e.g., `/config max_positions 6`) |
| `/exposure` | Exposure breakdown — by sector, by correlation cluster, total long/short |
| `/upcoming` | Upcoming catalysts for watchlist + held positions (earnings dates, FDA dates, etc.) |
| `/risk` | Risk dashboard — portfolio drawdown from peak, daily loss status, circuit breaker proximity |
| `/ask [question]` | Natural language query — "how are my tech positions doing?", "what's my biggest winner this month?", "should I be worried about my NVDA position given today's macro?" |
| `/help` | Full command list with descriptions |

**The `/ask` command — natural language interface:**

This is the most powerful feature. The operator can ask freeform questions and the system routes them to Sonnet with full portfolio context:

```
Operator: /ask what's my current risk if semiconductors sell off 10%?

System: [Loads portfolio state, identifies semiconductor exposure,
         computes hypothetical drawdown, checks correlations]

Bot: "You have 3 semiconductor-adjacent positions (NVDA, AMD, AVGO)
      totaling 14.2% of portfolio. A 10% sector drawdown would
      translate to roughly -1.4% portfolio impact before stops.
      Your stops would trigger on AMD (-3.2% from current) and
      AVGO (-4.1% from current) first, limiting realized loss
      to approximately -0.8%. NVDA stop is wider at -5.8% from
      current. Net worst case with all stops hit: -1.1% portfolio."
```

```
Operator: /ask why did we pass on the CRWD earnings setup last week?

System: [Retrieves CRWD memo from database, checks rejection log]

Bot: "CRWD scored 0.51 on Feb 7 — below the 0.55 memo threshold.
      Catalyst was strong (beat + guide up, magnitude 4/5) but
      fundamental valuation score was low (0.32 — trading at 75x
      forward P/E, well above peer median). Historical pattern
      match was also weak (only 3 comparable instances, mixed
      outcomes). Post-rejection, CRWD is +6.2%. This is being
      logged for signal calibration review."
```

The `/ask` command uses Sonnet with the full database state injected as context. It should be able to answer any question about the portfolio, past decisions, performance, or market conditions.

**Proactive notifications (system-initiated, no command needed):**

The bot doesn't just respond to commands — it proactively pushes critical updates:

| Event | Notification |
|-------|-------------|
| IC memo ready | Full memo with inline approval buttons |
| Order filled | Entry confirmation with position details |
| Stop-loss triggered | Exit notification with P&L |
| Target hit | Partial/full exit notification with P&L |
| Time exit approaching | "AAPL position hits max hold in 2 days. Currently +3.2%. Close or extend?" with inline buttons |
| Drawdown warning | "Portfolio drawdown at -7.1% from peak. Circuit breaker triggers at -10%. Consider reducing exposure." |
| Daily P&L threshold | If daily P&L swings more than ±2%, push a summary |
| Regime change | "Macro regime shifted from NEUTRAL → RISK-OFF. Reducing max positions to 4, tightening position sizes." |
| Agent failure | "⚠️ Catalyst agent failed at 12:00 PM run (Finnhub API timeout). Next retry in 30 min. Other agents ran normally." |
| Catalyst on held position | "New catalyst detected for held position NVDA: analyst upgrade from Morgan Stanley, PT raised to $950." |
| Watchlist alert | "Watchlisted ticker PLTR scored 0.72 (up from 0.58). New catalyst: $400M Army contract. Review memo?" with inline button |
| Morning digest | 7:00 AM ET: regime, overnight moves on held positions, today's upcoming catalysts, watchlist movers |
| End of day summary | 5:00 PM ET: daily P&L, trades executed, positions closed, new memos generated |

**Error handling and reliability:**

- If Telegram API is unreachable, queue messages and retry with exponential backoff
- All outbound messages logged to database (audit trail + ability to re-send)
- If the bot process crashes, it should auto-restart and send a "Bot restarted — here's current status" message
- Rate limiting on outbound messages (Telegram limits ~30 msgs/sec) — batch notifications during high-activity periods
- Long messages (>4096 chars) automatically split into multiple messages with continuation indicators
- Inline keyboard callbacks have a timeout — if operator doesn't respond to a memo within a configurable window (default: 4 hours), send a reminder. After 24 hours, auto-archive the memo (don't execute).

**Message formatting:**

Use Telegram's MarkdownV2 formatting for clean, readable output:
- Bold for headers and key metrics
- Monospace for numbers, prices, percentages
- Emoji for quick visual parsing (🟢 profit, 🔴 loss, ⚠️ warning, 📊 data, 🎯 target)
- Keep messages scannable — operator should grasp the key info in 3 seconds on a phone screen

**Authentication & security:**

- Whitelist of authorized Telegram `chat_id` values in config
- All commands from unauthorized users silently ignored (no error response — don't reveal bot exists)
- Trade-modifying commands (close, adjust, approve) require a confirmation step
- `/config` command restricted to non-critical parameters (can't change risk limits via Telegram — those require code/config file changes)
- All operator interactions logged with timestamps for audit trail

**Memo interaction flow:**

```
System → Telegram: [Full IC Memo with formatting]
                   [Approve ✅] [Modify ✏️] [Reject ❌] [Watchlist 👀]

Operator taps "Approve ✅"
System → Telegram: "Confirmed: Submitting limit buy for 50 shares NVDA @ $875.20.
                    Stop-loss at $831.44 (-5.0%). Will confirm fill."

[Order fills]
System → Telegram: "✅ Filled: 50 NVDA @ $875.10. Stop-loss order placed.
                    Position: 4.4% of portfolio."

Operator taps "Modify ✏️"
System → Telegram: "What would you like to modify?"
                   [Entry Price] [Position Size] [Stop-Loss] [Targets]
```

**Ad-hoc analysis flow (the `/test` command):**

```
Operator → Telegram: "/test PLTR Palantir just won a $500M DoD contract,
                      stock barely moved"

System: Runs PLTR through all signal agents with the provided thesis as
        catalyst context. Haiku pre-screens (instant), Sonnet analyzes
        (~30s), Opus scores and stress-tests (~60s), Sonnet drafts memo.
        Total time: ~2-3 minutes.

System → Telegram: [Full IC Memo for PLTR]
                   [Approve ✅] [Modify ✏️] [Reject ❌] [Watchlist 👀]
```

**Technical implementation:**

- Use `python-telegram-bot` library (async, well-maintained, v20+)
- Bot runs as a separate async process alongside the scheduler (both managed by a process supervisor or just two Python processes)
- Shared SQLite database for state (bot reads portfolio state, writes approvals, reads memos)
- Inline keyboards with callback data for approve/reject/modify interactions
- Conversation handlers (from `telegram.ext`) for multi-step flows (modify → which parameter → new value → confirm)
- The `/ask` handler constructs a Sonnet prompt with serialized portfolio state, recent trades, and the operator's question
- All outbound messages go through `message_queue.py` which handles rate limiting (Telegram allows ~30 msgs/sec), retry with backoff, and logging
- Bot uses webhook mode for reliability if deployed to cloud, long-polling for local dev

**TWO-WAY DOOR:** Telegram message formatting for memos — full memo in-chat (split across messages if >4096 chars) vs. condensed summary with a "Show full memo" inline button that sends the rest. Test both during development.

### 6.4 Email Backup

In addition to Telegram, send email notifications as a backup channel. This ensures memos aren't missed if Telegram is down or the bot crashes.

**TWO-WAY DOOR:** Email implementation — SMTP (simpler, works with any email) or Gmail API (richer, requires OAuth). Decide during implementation.

### 6.5 Operator Response

In Phase 1, all trade execution requires explicit operator approval via Telegram. The operator can:
- **Approve** — execute as suggested (single tap)
- **Approve with modifications** — adjust entry, size, stops (guided multi-step flow)
- **Reject** — log rejection with optional reason (important for learning)
- **Watchlist** — don't trade now, but track and re-evaluate

All responses are handled via Telegram inline keyboards and conversation handlers. No need for email replies, CLI commands, or web forms in Phase 1.

---

## 7. Execution Engine (Alpaca Integration)

### 7.1 Paper Trading Setup

- Use Alpaca paper trading API (free, no real money)
- Starting paper portfolio: $100,000 (configurable)
- All orders submitted as limit orders (not market) to simulate realistic fills
- Limit price: use a small buffer from current price (e.g., 0.1% above ask for buys) to simulate slippage

### 7.2 Order Management

**Entry:**
- After operator approval, submit limit order
- If not filled within 1 trading day, cancel and re-evaluate
- Log entry with all context (score, memo ID, regime, timestamp)

**Exit — stop-loss:**
- Submit stop-loss order immediately upon fill
- Stop-loss levels determined by setup type and volatility:
  - Default: 5% below entry for longs
  - Adjusted by ATR (Average True Range) — tighter stops for low-vol names, wider for high-vol
  - Hard maximum stop: 8% (no single trade should lose more than this)

**Exit — take profit:**
- Target prices set in memo
- Consider scaling out: sell 50% at Target 1, let remainder run to Target 2 with trailing stop
- If no target hit within max holding period, close position and log as "time exit"

**Exit — time-based:**
- Max holding period per trade (default: 20 trading days, adjustable per setup type)
- If position is profitable but hasn't hit target at max hold, close and log

### 7.3 Position Sizing

```
base_position_pct = 5%  # of portfolio
regime_multiplier = macro_regime.position_size_multiplier  # 0.5 - 1.5
conviction_multiplier = score_to_conviction(composite_score)  # 0.5 - 1.5
volatility_adjustment = normalize_by_atr(ticker)  # reduce size for high vol

final_position_pct = base_position_pct × regime_multiplier × conviction_multiplier × volatility_adjustment

# Hard limits
min_position_pct = 2%
max_position_pct = 10%
max_portfolio_exposure = regime.max_portfolio_exposure  # 40-80%
max_sector_exposure = 30%  # no more than 30% in one sector
max_single_position = 10%  # hard cap
```

### 7.4 Risk Management Rules (Non-Negotiable)

These rules are hard-coded and cannot be overridden by any signal or score:

1. **Max portfolio drawdown circuit breaker:** If paper portfolio drops 10% from peak, halt all new trades for 5 trading days. Generate alert.
2. **Max daily loss:** If unrealized + realized losses exceed 3% of portfolio in a single day, halt new entries for remainder of day.
3. **Correlation check:** Before entering a new position, check correlation with existing positions. If new position is >0.7 correlated with an existing holding, reduce size or skip.
4. **Earnings blackout:** Do not hold positions through earnings unless the catalyst IS the earnings event itself. If a held position has earnings approaching within 3 days and the trade thesis is not earnings-related, close before earnings.
5. **Max concurrent positions:** Governed by macro regime (2-8 positions). Never exceed 8 regardless of regime.

---

## 8. Performance Tracking & Feedback Loop

### 8.1 Per-Trade Attribution

Every trade must log:

- Entry and exit prices, dates, and fill quality
- P&L (absolute and percentage)
- Holding period
- Setup type classification
- Composite score at entry
- Individual signal scores at entry
- Macro regime at entry
- Whether it was IC-approved or (future) auto-executed
- Exit reason (stop-loss, target, time, manual)
- Operator notes (if any)

### 8.2 Aggregate Performance Metrics

Compute and store (updated daily):

- Total return, annualized return
- Sharpe ratio (using risk-free rate from FRED)
- Sortino ratio
- Max drawdown
- Win rate (overall and by setup type)
- Average winner / average loser ratio
- Profit factor
- Average holding period
- Best and worst trades

### 8.3 Signal Attribution Analysis

This is the critical feedback loop. For each signal layer, track:

- **Signal accuracy:** When this signal was bullish, how often did the trade win?
- **Signal contribution:** What's the marginal improvement in win rate when this signal confirms vs. doesn't?
- **Signal calibration:** Is a 0.8 score from this agent actually better than a 0.6? (Calibration curve)
- **False positive rate:** How often does this signal flag an opportunity that turns out to be a loser?

**Monthly review (automated report):**
- Which setup types are working?
- Which signal layers are contributing most to winners?
- Are there systematic biases (always too early, always too small on position size, etc.)?
- Suggested weight adjustments for the scoring model

### 8.4 Improvement Mechanism

The system should generate a monthly self-assessment that includes:

1. Performance summary with attribution
2. Proposed weight adjustments to the scoring model (not auto-applied — operator reviews)
3. Identification of new setup types that could be added
4. Flag any signals that are consistently wrong (candidates for removal or inversion)
5. Comparison of IC-approved trades vs. logged-but-not-traded opportunities (to measure whether the operator's filtering is adding or destroying value)

---

## 9. Technical Implementation Guidelines

### 9.1 Project Structure

```
swing-trader/
├── README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── settings.py          # All configurable parameters
│   ├── tickers.py            # Universe management
│   └── peers.py              # Peer group mappings
├── agents/
│   ├── base_agent.py         # Abstract base class for all agents
│   ├── macro_agent.py
│   ├── catalyst_agent.py
│   ├── fundamental_agent.py
│   ├── pattern_agent.py
│   └── reddit_agent.py
├── scoring/
│   ├── engine.py             # Composite score calculation
│   └── weights.py            # Weight configuration (editable)
├── execution/
│   ├── alpaca_client.py      # Alpaca API wrapper
│   ├── order_manager.py      # Order lifecycle management
│   ├── position_manager.py   # Position sizing, risk checks
│   └── risk_manager.py       # Circuit breakers, exposure limits
├── memo/
│   ├── generator.py          # IC memo creation
│   ├── delivery.py           # Multi-channel delivery (Telegram + email)
│   └── templates/            # Memo templates
├── bot/
│   ├── telegram_bot.py       # Bot initialization, error handling, auto-restart
│   ├── handlers/
│   │   ├── commands.py       # /status, /regime, /watchlist, /exposure, /risk, /upcoming, /agents
│   │   ├── callbacks.py      # Inline keyboard callbacks (approve/reject/modify/watchlist)
│   │   ├── test_idea.py      # /test and /score commands — ad-hoc analysis flows
│   │   ├── ask.py            # /ask — natural language query handler (Sonnet + portfolio context)
│   │   ├── trade_mgmt.py     # /close, /adjust — position management commands
│   │   ├── performance.py    # /performance, /history, /memo — review and replay
│   │   ├── config.py         # /config — runtime parameter adjustments
│   │   └── conversations.py  # Multi-step flows (modify parameters, confirmations)
│   ├── notifications.py      # Proactive push notifications (fills, stops, alerts, digests)
│   ├── formatters.py         # Memo → Telegram message formatting (MarkdownV2, emoji)
│   ├── keyboards.py          # Inline keyboard layouts
│   ├── message_queue.py      # Outbound message queue with retry and rate limiting
│   └── auth.py               # Chat ID whitelist validation
├── data/
│   ├── market_data.py        # Price data fetching/caching
│   ├── fundamental_data.py   # Financial data fetching/caching
│   ├── news_data.py          # News/catalyst data fetching
│   ├── reddit_data.py        # Reddit data fetching
│   ├── sec_data.py           # SEC EDGAR integration
│   └── macro_data.py         # FRED and macro indicator data
├── database/
│   ├── models.py             # SQLAlchemy/dataclass models
│   ├── migrations/           # Schema migrations
│   └── db.py                 # Database connection/session management
├── orchestrator/
│   ├── scheduler.py          # Run scheduling
│   ├── pipeline.py           # Main orchestration logic
│   └── universe.py           # Ticker universe refresh
├── tracking/
│   ├── performance.py        # P&L and performance metrics
│   ├── attribution.py        # Signal attribution analysis
│   └── reporter.py           # Monthly report generation
├── backtest/
│   ├── engine.py             # Backtesting framework
│   ├── data_loader.py        # Historical data preparation
│   └── analyzer.py           # Backtest result analysis
├── utils/
│   ├── logger.py             # Structured logging
│   ├── anthropic_client.py   # Claude API wrapper with retry/batching
│   ├── model_selector.py     # Task-type → model mapping (Haiku/Sonnet/Opus)
│   ├── escalation_manager.py # Haiku→Sonnet→Opus routing based on scores/task type
│   └── rate_limiter.py       # API rate limit management
├── tests/
│   ├── test_agents/
│   ├── test_scoring/
│   ├── test_execution/
│   └── test_data/
└── scripts/
    ├── setup_alpaca.py       # Initial Alpaca setup
    ├── seed_universe.py      # Populate initial ticker universe
    ├── seed_peers.py         # Populate peer mappings
    └── run_backtest.py       # Ad-hoc backtest runner
```

### 9.2 Key Design Principles

1. **Adapter pattern for data sources.** Every data source should be behind an interface so providers can be swapped without changing agent logic.

2. **Agents are stateless.** Each agent receives inputs and produces outputs. No agent maintains internal state between runs. All state lives in the database.

3. **Aggressive logging.** Log everything — every API call, every score, every decision, every rejection. Storage is cheap. Debugging a bad trade 3 weeks later requires full context.

4. **Fail gracefully.** If one agent fails (API timeout, rate limit, etc.), the system should still produce a result with reduced confidence, not crash entirely. Flag which agents contributed to each score.

5. **Idempotent runs.** Running the pipeline twice for the same timestamp should produce the same result and not create duplicate trades or memos.

6. **Configuration over code.** Weights, thresholds, position sizes, risk parameters — all should be in config files, not hardcoded. The operator should be able to tune the system without touching agent logic.

7. **Telegram bot security.** The bot must validate that incoming messages are from the authorized operator (check `chat_id` against a whitelist in config). Reject all messages from unauthorized users silently. This is critical — the bot can execute trades.

### 9.3 Claude API Usage Optimization

Token costs will be the primary ongoing expense. Optimize by:

- **Batching:** When possible, analyze multiple tickers in a single API call (e.g., Reddit sentiment for 10 tickers at once)
- **Tiered analysis:** Use lightweight pre-filtering (keyword matching, simple rules) before escalating to Claude for deep analysis
- **Caching:** Don't re-analyze fundamentals that haven't changed. Cache Claude's analysis and invalidate only when new data arrives (quarterly for financials, per-event for catalysts)
- **Model tiering strategy — intelligence-heavy with escalation chain:**

  The system uses a three-tier escalation model. Haiku is the workhorse that scrapes, filters, and does fast classification. Sonnet is the analyst that does real reasoning on anything Haiku flags as interesting. Opus is the portfolio manager that evaluates Sonnet's work, scores final trade ideas, and makes the high-stakes judgment calls.

  **Tier 1 — Haiku (scraper + fast filter):**

  | Task | Notes |
  |------|-------|
  | News/RSS ingestion and relevance filtering | "Is this article about a ticker in our universe and potentially material?" — binary yes/no |
  | Reddit post scraping and sentiment classification | Bulk sentiment tagging: bullish/bearish/neutral per post |
  | SEC filing detection and classification | "Is this an insider buy, 8-K, 13D?" — categorize filing type |
  | Earnings data parsing | Extract beat/miss magnitude, guidance direction from structured data |
  | Catalyst pre-screening | Quick relevance score (1-5). Anything scoring 3+ gets escalated to Sonnet |
  | Price data pattern detection | Identify if historical analogs exist (statistical, not interpretive) |

  **Tier 2 — Sonnet (analyst — reasoning and synthesis):**

  | Task | Notes |
  |------|-------|
  | Catalyst deep analysis | Haiku flagged it as relevant — Sonnet reads the full context, assesses magnitude, direction, expected impact, time horizon |
  | Fundamental narrative synthesis | Turn raw financial data into "here's what matters and why" |
  | Peer comparison analysis | Relative positioning, why this name vs. peers |
  | Historical pattern interpretation | Sonnet interprets the statistical patterns Haiku identified — "what do these analogs actually suggest?" |
  | Reddit sentiment synthesis | When Haiku flags unusual activity (z-score > 2), Sonnet reads the actual posts and assesses narrative quality |
  | IC memo generation | Sonnet drafts the full memo with thesis, catalyst, fundamentals, precedent, risk/reward |
  | Ad-hoc `/test` analysis | Full pipeline run on operator-submitted ideas |

  **Tier 3 — Opus (portfolio manager — judgment and scoring):**

  | Task | Notes |
  |------|-------|
  | Final trade scoring and conviction assessment | Opus reviews Sonnet's complete analysis package and assigns the final composite score. This is the critical gate — Opus sees all signal layers together and makes the "is this actually worth a position?" call |
  | Thesis stress-testing | Opus actively tries to poke holes in Sonnet's bull case. "What's the bear case Sonnet missed? Is the catalyst already priced in? Is the sample size too small to trust the pattern match?" |
  | Signal disagreement adjudication | When signals conflict, Opus weighs which signals matter more for this specific setup type and context |
  | Complex/ambiguous catalyst interpretation | M&A implications, regulatory nuance, earnings quality vs. quantity, management credibility assessment |
  | Monthly self-assessment and weight recalibration | Deep reasoning about system performance, signal attribution, and recommended improvements |
  | Risk assessment on correlated positions | "We already hold NVDA — does adding AMD create hidden concentration risk given the current macro setup?" |
  | Regime change detection | When macro inputs are borderline, Opus makes the nuanced call on regime classification |

  **The escalation flow:**

  ```
  Raw data → Haiku (filter: relevant?) 
                 ↓ yes
             Sonnet (analyze: what does this mean?)
                 ↓ produces trade thesis
             Opus (judge: is this actually good? what's the real score?)
                 ↓ score >= 0.55
             Sonnet (draft IC memo incorporating Opus's scoring and critique)
                 ↓
             Telegram delivery
  ```

  The key insight: Sonnet generates ideas, Opus evaluates them. This separation prevents the same model from both proposing and approving a trade, which creates a natural "second opinion" check. Opus has permission to be skeptical — its job is to find the weakness in every thesis.

  Implement this as a `model_selector` utility that maps task types to models, with an `escalation_manager` that routes between tiers based on Haiku's initial relevance scores. The escalation threshold is configurable (default: Haiku score >= 3 out of 5 triggers Sonnet).

- **Structured outputs:** Ask Claude for JSON responses to avoid parsing overhead and reduce output tokens

Estimated monthly token budget for Phase 1 (intelligence-heavy tiering):
- Haiku — scraping/filtering: ~3,000 calls/month × ~500 tokens = ~1.5M tokens → ~$0.40
- Sonnet — catalyst analysis: ~300 calls/month × ~2K tokens = ~600K tokens → ~$5
- Sonnet — memo drafting: ~60 memos/month × ~2K tokens = ~120K tokens → ~$1
- Sonnet — pattern/fundamental/sentiment synthesis: ~200 calls/month × ~1.5K tokens = ~300K tokens → ~$2.50
- Opus — trade scoring & stress-testing: ~120 calls/month × ~3K tokens = ~360K tokens → ~$8
- Opus — signal adjudication & complex catalysts: ~40 calls/month × ~3K tokens = ~120K tokens → ~$3
- Opus — monthly report + regime calls: ~10 calls/month × ~5K tokens = ~50K tokens → ~$1
- **Estimated total: $20-35/month**
- This is higher than a Haiku-heavy approach but the quality difference in trade scoring is where the system earns its keep. Bad scoring costs more than API tokens.

### 9.4 Testing Strategy

- **Unit tests** for scoring math, position sizing, risk rules
- **Integration tests** for each data adapter (mock API responses)
- **Agent tests** with fixture data (known catalysts → expected output format)
- **End-to-end test** with a single ticker through the full pipeline
- **Paper trading IS the acceptance test** for Phase 1 — real market data, fake money

---

## 10. Setup & Prerequisites

Before building, the operator needs:

1. **Alpaca account** — Sign up at alpaca.markets, get paper trading API keys
2. **API keys** — Finnhub, Financial Modeling Prep, Alpha Vantage (all free tier), FRED (free)
3. **Reddit app** — Register at reddit.com/prefs/apps for API access (PRAW)
4. **Anthropic API key** — For Claude analysis calls (Haiku, Sonnet, and Opus access)
5. **Telegram bot** — Create via @BotFather on Telegram, get bot token. Note your Telegram user ID for authorized-user filtering (the bot should only respond to you).
6. **Python 3.11+** environment
7. **Email credentials** (SMTP or Gmail API) for backup memo delivery
8. **Docker** (recommended) — Dockerfile from day 1 for eventual cloud deployment

---

## 11. Build Order (Recommended Sequence)

Building in the right order matters — each step should produce something testable and give you a feedback signal before moving to the next.

**Step 1 — Skeleton + Telegram bot + database**
- Project scaffolding, config system, `.env` setup, Dockerfile
- SQLite database with schema and models
- Telegram bot with basic commands returning dummy/placeholder data (`/status`, `/help`)
- Why first: the bot becomes your development dashboard immediately. Every subsequent step has a visible output channel.

**Step 2 — Data layer + market data adapters**
- Yahoo Finance price data fetching and caching
- FRED macro data fetching
- FMP/Finnhub fundamental data fetching
- Adapter pattern established so all data sources are swappable
- Test: `/test AAPL` returns raw data summary via Telegram

**Step 3 — Macro Regime Agent**
- Rules-based regime classification
- Connects to FRED + Yahoo Finance data
- Test: `/regime` returns real classification with reasoning via Telegram

**Step 4 — Fundamental Agent**
- Scoring logic (mostly deterministic math from financial data)
- Peer comparison (seed a starter peer mapping for top 50 tickers)
- Sonnet integration for narrative synthesis
- Test: `/test NVDA` returns fundamental scores via Telegram

**Step 5 — Catalyst Agent + Haiku→Sonnet escalation**
- Haiku pre-screening pipeline (news ingestion, filing detection)
- Sonnet deep analysis for escalated catalysts
- This is where the escalation chain gets built and tested
- Test: inject a known catalyst (e.g., a recent earnings beat) and verify the escalation flow

**Step 6 — Scoring Engine + Opus evaluation**
- Composite scoring from all available signals
- Opus stress-testing and final scoring
- Signal disagreement handling
- Test: manually trigger a catalyst and watch the full Haiku→Sonnet→Opus flow produce a score

**Step 7 — IC Memo generation + Telegram delivery**
- Sonnet memo drafting incorporating Opus's score and critique
- Full memo formatting for Telegram (with inline keyboards)
- Approve/reject/modify/watchlist flow
- Test: receive a real memo on your phone, approve it

**Step 8 — Alpaca execution + position management**
- Paper trading API integration
- Order submission on approval
- Stop-loss placement, position tracking
- Portfolio state available via `/status` and `/positions`
- Test: approve a memo, verify paper trade executes and stop-loss is placed

**Step 9 — Historical Pattern Agent**
- Backtesting-lite: historical analog identification
- Sonnet interpretation of patterns
- Integrates into the scoring pipeline
- Test: verify pattern data appears in memos

**Step 10 — Reddit Sentiment Agent**
- PRAW integration, Haiku classification, Sonnet synthesis for flagged tickers
- Integrates into scoring pipeline
- Test: verify sentiment data appears in memos

**Step 11 — Orchestrator + scheduling**
- Automated 3x daily pipeline runs
- Full end-to-end: scan → score → memo → approve → execute → track
- Risk management circuit breakers active
- Email backup delivery

**Step 12 — Performance tracking + feedback loop**
- Trade attribution logging
- Performance metrics computation
- Monthly report generation (Opus)
- Signal calibration analysis

**Step 13 — Hardening**
- Error handling, retry logic, graceful degradation
- Rate limiting across all APIs
- Comprehensive logging
- Edge case testing (market holidays, halted tickers, API outages)

Each step should be a PR-sized chunk of work. Don't move to the next step until the current one is tested and visible through the Telegram bot.

---

## 12. Definition of Done — Phase 1

Phase 1 is complete when:

**Infrastructure:**
- [ ] Project runs locally with Docker support ready for cloud migration
- [ ] SQLite database with full schema operational
- [ ] All API adapters functional with free tier keys
- [ ] Config-driven parameters (no hardcoded thresholds)
- [ ] Comprehensive structured logging across all components

**Agents & Model Escalation:**
- [ ] Haiku→Sonnet→Opus escalation chain working end-to-end
- [ ] Macro regime agent classifies correctly and adjusts risk parameters
- [ ] Catalyst agent detects events (Haiku pre-screen → Sonnet deep analysis)
- [ ] Fundamental agent scores any ticker in the universe on demand
- [ ] Historical pattern agent runs for catalyst-triggered opportunities
- [ ] Reddit agent produces daily sentiment snapshots
- [ ] Opus receives full signal package and produces final conviction score with stress-test critique
- [ ] Scoring engine produces composite scores with full signal breakdown

**Telegram Bot — Command Interface:**
- [ ] All commands operational: `/status`, `/positions`, `/regime`, `/watchlist`, `/test`, `/score`, `/performance`, `/history`, `/memo`, `/close`, `/adjust`, `/exposure`, `/risk`, `/upcoming`, `/agents`, `/ask`, `/pause`, `/resume`, `/config`, `/help`
- [ ] `/test` runs full pipeline and returns scored memo within 3 minutes
- [ ] `/ask` handles natural language queries with portfolio context
- [ ] Inline keyboard approve/reject/modify/watchlist flow works end-to-end
- [ ] Proactive notifications: order fills, stop-loss triggers, drawdown warnings, regime changes, agent failures
- [ ] Morning digest and end-of-day summary automated
- [ ] Watchlist alerts when scores change materially
- [ ] Message queue with retry logic for reliability
- [ ] Auth whitelist enforced — unauthorized users silently rejected

**Execution & Risk:**
- [ ] Approved trades execute on Alpaca paper trading
- [ ] Stop-losses placed automatically on fill
- [ ] Position sizing respects regime multiplier and conviction scaling
- [ ] All risk management circuit breakers enforced (drawdown, daily loss, correlation, earnings blackout)
- [ ] Manual position management via Telegram (`/close`, `/adjust`)

**Tracking & Feedback:**
- [ ] Every trade logged with full attribution (all signal scores, regime, setup type)
- [ ] Performance metrics computed daily
- [ ] Monthly Opus-generated self-assessment report
- [ ] Email backup delivery functional
- [ ] System has run for 60+ days of paper trading
- [ ] All two-way doors documented with chosen implementation

---

## 13. Phase 4+: Reinforcement Learning & Adaptive Intelligence

Everything in this section depends on one thing: the data pipeline built in Phase 1. The static scoring engine, the signal attribution logging, the operator's approve/reject decisions, the Opus stress-tests, the per-trade P&L tracking — all of it feeds the learning systems described below. Phase 1 is not just a trading system; it's a data collection apparatus for the adaptive intelligence that follows. Every design choice in the data layer should be made with this section in mind.

The progression is deliberate: start with the simplest learning problem (contextual bandits for weight calibration), graduate to sequential decision-making (position sizing and exit timing), then move to model-level learning (fine-tuning and self-play). Each tier requires more data and more sophisticated infrastructure than the last.

### 13.1 Reward-Weighted Signal Calibration

This is the first RL application to implement once Phase 1 generates sufficient data. It is also the highest-impact one — the scoring engine's signal weights are currently static (35% catalyst, 30% fundamental, 20% pattern, 15% sentiment), but the optimal weights are almost certainly context-dependent. A biotech catalyst play and a mega-cap earnings beat should not be scored with the same weight allocation. This system learns the right weights from realized outcomes.

#### 13.1.1 Problem Formulation

Frame this as a **contextual bandit problem**, not full reinforcement learning. The distinction matters: in a contextual bandit, the action (weight allocation) does not change the environment state. Choosing to weight catalysts at 45% for a biotech trade doesn't affect the next biotech trade's optimal weights. This makes the problem significantly simpler, more stable, and tractable with smaller datasets than full RL would require.

**State (context vector):**

```json
{
  "macro_regime": "risk-on | neutral | risk-off",
  "sector": "GICS sector (11 categories)",
  "setup_type": "earnings_beat | insider_cluster | analyst_upgrade | ...",
  "catalyst_score": 0.0-1.0,
  "fundamental_score": 0.0-1.0,
  "pattern_score": 0.0-1.0,
  "sentiment_score": 0.0-1.0,
  "market_volatility": "low | medium | high",
  "time_since_catalyst_hours": "float",
  "stock_move_since_catalyst_pct": "float",
  "market_cap_bucket": "mid | large | mega"
}
```

**Action:** Weight allocation across the four signal layers.

```
weights = [w_catalyst, w_fundamental, w_pattern, w_sentiment]

Constraints:
  sum(weights) = 1.0
  each w_i ∈ [0.05, 0.60]
```

**Reward:** Risk-adjusted P&L of the trade, specifically the trade's contribution to the portfolio Sortino ratio. Sortino is preferred over Sharpe because we want to reward upside volatility (big winners) while penalizing downside volatility (drawdowns and stop-outs). Raw return is insufficient because a +8% return on a meme stock that could easily have been -15% should be rewarded less than a +5% return on a high-quality name with 2% downside risk.

```python
def compute_reward(trade):
    """
    Reward = excess return / downside deviation contribution.
    Simplified Sortino contribution for individual trades.
    """
    excess_return = trade.pnl_pct - risk_free_daily_rate * trade.holding_days
    if excess_return >= 0:
        # Upside: reward proportional to return, bonus for low drawdown
        max_drawdown_during = trade.max_adverse_excursion_pct
        drawdown_penalty = max(0, max_drawdown_during - 0.03) * 2  # penalize >3% drawdowns
        reward = excess_return - drawdown_penalty
    else:
        # Downside: penalize more heavily than raw loss
        reward = excess_return * 1.5  # asymmetric penalty
    return reward
```

#### 13.1.2 Data Requirements

**Minimum viable dataset:** ~200 completed trades with full signal attribution and outcome data. At Phase 1's expected pace of ~2-4 trades per week, this represents roughly 12-18 months of live paper trading. That's a long time to wait. There are ways to accelerate.

**Bootstrapping with historical data:**

The key insight is that not all signals are equally reconstructable. The system's signals fall into two categories:

*Quantitative signals (fully reconstructable):*
- Fundamental scores: financial data is fully available historically via FMP/Alpha Vantage. Run the exact same scoring logic against historical quarterly data to reconstruct what the fundamental agent would have scored at any point in the past 5 years.
- Pattern match statistics: historical price data exists. For any past catalyst, compute the same forward-return statistics the pattern agent would have produced.
- Macro regime state: FRED data is fully historical. Reconstruct the regime classification for any historical date using the same rules-based logic.
- Market volatility buckets: VIX history is available. Straightforward to reconstruct.

*Qualitative signals (approximated, not perfectly reconstructed):*
- Catalyst analysis quality: Claude's assessment of a catalyst's magnitude, direction, and confidence cannot be perfectly replicated for historical events because the model's "surprise" at a piece of news depends on what it already knew at that moment. However, you CAN retroactively run historical news articles and SEC filings through the same Haiku→Sonnet→Opus pipeline. The analysis won't be identical to what the system would have produced in real-time (the model may "know" the outcome subconsciously), but it's a reasonable approximation. Budget ~$50-100 in API costs to process 2-3 years of historical catalysts across the ticker universe.
- Reddit sentiment: historical Reddit data is available via archives, but the subreddit landscape and posting patterns have changed significantly. Use with caution and mark as lower-confidence in the training set.

**Hybrid bootstrapping approach:**

```
Historical training set (~500-1000 samples):
├── Quantitative signals: reconstructed exactly using same scoring logic
├── Qualitative signals: approximated via two methods
│   ├── Full reconstruction: run historical catalysts through Claude pipeline (~$50-100)
│   └── Proxy features: use quantitative proxies for qualitative scores
│       ├── Earnings surprise magnitude → proxy for catalyst score
│       ├── Analyst revision breadth → proxy for catalyst confidence
│       └── Filing count/type → proxy for insider signal strength
└── Outcomes: historical forward returns at T+5, T+10, T+15, T+20

Live training set (grows over time):
├── All signals: exact values from live pipeline runs
├── Outcomes: actual trade P&L with full attribution
└── Operator decisions: approve/reject with reasoning (invaluable)
```

**Critical caveat on backtested signals:** Historical reconstruction has inherent lookahead bias risk. The model analyzing a 2023 earnings beat "knows" what happened next, even if you don't explicitly tell it. Mitigation:

- Use strict walk-forward validation: train on data up to month N, test on month N+1, roll forward
- Never include the outcome period's data in any feature computation
- Weight live data 2-3x more heavily than historical data in the training set
- Track performance separately for historically-bootstrapped predictions vs. live predictions to measure the bias gap

#### 13.1.3 Implementation Approach

**Algorithm: Thompson Sampling with Bayesian linear regression.**

Thompson Sampling is the right starting point for several reasons: it handles uncertainty naturally (critical with small datasets), it explores efficiently (tries uncertain weight combinations to learn about them), and it's computationally cheap (no neural networks, no gradient descent, runs in milliseconds).

The approach:

1. Maintain a Bayesian posterior distribution over the mapping from state features to optimal weights
2. For each new trade opportunity, sample from the posterior to get a weight allocation
3. After the trade completes, update the posterior with the observed reward
4. As the posterior tightens (more data), the sampled weights converge toward the learned optimum

**State space discretization:**

Continuous features need to be binned to keep the problem tractable with limited data. The state space should be rich enough to capture meaningful differences but small enough that each bin accumulates reasonable sample sizes.

```python
STATE_DISCRETIZATION = {
    "macro_regime": ["risk-on", "neutral", "risk-off"],           # 3 bins
    "sector": GICS_SECTORS,                                        # 11 bins
    "setup_type": [
        "earnings_beat", "earnings_miss_recovery", "insider_cluster",
        "analyst_upgrade", "mna_target", "product_catalyst",
        "capital_allocation", "sector_rotation"
    ],                                                             # 8 bins
    "market_volatility": ["low", "medium", "high"],                # 3 bins
    "conviction_bucket": ["moderate", "high", "very_high"],        # 3 bins
    "market_cap_bucket": ["mid", "large", "mega"],                 # 3 bins
}

# Total theoretical state space: 3 × 11 × 8 × 3 × 3 × 3 = 7,128 combinations
# In practice, many combinations are sparse or empty.
# The Bayesian approach handles this naturally — sparse states
# have wide posteriors and fall back toward the prior (default weights).
```

**Fallback behavior for sparse states:**

This is critical. When the system encounters a state with insufficient data (fewer than 10 observations), it should not blindly use learned weights. Instead:

```python
def get_weights(state, min_observations=10):
    """
    Return learned weights if sufficient data exists for this state,
    otherwise blend with default weights based on confidence.
    """
    obs_count = get_observation_count(state)

    if obs_count >= min_observations:
        # Confident: sample from learned posterior
        learned_weights = thompson_sample(state)
        confidence = min(1.0, obs_count / 50)  # saturates at 50 obs
    else:
        # Sparse: blend learned with defaults
        confidence = obs_count / min_observations
        learned_weights = thompson_sample(state)  # wide posterior, close to prior

    # Blend: as confidence grows, rely more on learned weights
    final_weights = (confidence * learned_weights) + ((1 - confidence) * DEFAULT_WEIGHTS)

    return normalize(clip(final_weights, min=0.05, max=0.60))
```

**Exploration strategy:**

Use epsilon-greedy exploration layered on top of Thompson Sampling's natural exploration:

- 80% of the time: use Thompson-sampled weights (which already have built-in exploration via posterior uncertainty)
- 15% of the time: use the static default weights (ensures baseline comparison data keeps accumulating)
- 5% of the time: use random weight perturbations within the valid range (ensures coverage of the action space)

The exploration rate should decay over time as the posterior tightens, but never drop below 5% to prevent the system from getting stuck in a local optimum.

**Update cadence:**

Weight updates should be batched, not per-trade. Per-trade updates overfit to noise — a single lucky trade on a Tuesday shouldn't shift weights for Wednesday.

- Minimum update interval: weekly (after at least 2 new completed trades)
- Preferred update interval: monthly (after ~8-16 completed trades)
- Each update recomputes the full posterior from all historical data (not incremental — this prevents drift from compounding approximation errors)
- Log the posterior distribution at each update for diagnostic analysis

**Regret tracking:**

Maintain a running estimate of regret — the cumulative difference between the reward achieved with learned weights and the reward that would have been achieved with the (unknowable) optimal weights. In practice, approximate this by comparing:

- Learned weights' reward on held-out data
- Best-in-hindsight static weights on the same data
- Default static weights on the same data

If learned weights consistently underperform the static defaults, something is wrong. This is the primary diagnostic for detecting when the RL system is hurting rather than helping.

#### 13.1.4 What This Learns Over Time

Concrete examples of context-dependent weight adjustments the system should discover:

**Regime-dependent shifts:**
> "In risk-off regimes, fundamental quality should be weighted 45% instead of 30% because only high-quality names hold up. Catalyst strength matters less (25%) because even strong catalysts get overwhelmed by macro selling pressure."

**Sector-specific patterns:**
> "For biotech catalyst plays (FDA approvals, trial results), pattern match is nearly useless — reduce to 5%. These are binary events with no meaningful historical analogy. But catalyst magnitude is everything — increase to 55%. The quality of the catalyst analysis IS the trade."

**Contrarian sentiment signals:**
> "When Reddit sentiment is extremely bullish (z-score > 2.5) AND the stock has already moved >10% since catalyst, the contrarian signal is worth 25% weight. The crowd is late, and mean reversion is the dominant dynamic."

**Large-cap earnings plays:**
> "For mega-cap earnings plays ($100B+ market cap), the signal weights should be nearly equal (catalyst 28%, fundamental 27%, pattern 25%, sentiment 20%) because all signals are informative and none dominates. These are the most efficiently-priced situations."

**Catalyst-age decay:**
> "When the catalyst is >48 hours old and the stock has already moved >5%, catalyst weight should drop to 15% and pattern weight should increase to 35%. The question shifts from 'is this catalyst real?' to 'is there still room to run?'"

#### 13.1.5 Safeguards

The RL system is learning to adjust the brain of the trading system. Safeguards are not optional.

**Hard constraints on weight ranges:**
```python
WEIGHT_CONSTRAINTS = {
    "min_any_signal": 0.05,       # No signal can be zeroed out
    "max_any_signal": 0.60,       # No signal can dominate entirely
    "max_shift_per_update": 0.05, # Max 5 percentage point shift per update cycle
    "sum_must_equal": 1.0,        # Weights must be a valid probability distribution
}
```

**Rate-limiting weight changes:** No single update can shift any weight by more than 5 percentage points. If the posterior suggests a larger shift, it gets clamped and applied over multiple update cycles. This prevents the system from overreacting to a cluster of similar trades that happened to all succeed (or all fail) in the same period.

**Staleness decay:** If a particular state (e.g., "risk-off + biotech + FDA catalyst") hasn't been visited in 3 months, the learned weights for that state should decay toward the default weights at a rate of 10% per month of inactivity. Markets change, and stale learned weights from a different regime can be worse than defaults.

**A/B comparison logging:** Every scored trade must log TWO sets of scores:
1. The score using the current learned weights
2. The score using the static default weights

This creates a continuous A/B test. If the learned weights consistently produce lower-quality scores (measured by the correlation between score and eventual P&L), the system should alert the operator and optionally revert to defaults.

```json
{
  "trade_id": "uuid",
  "scored_with": "learned",
  "learned_weights": {"catalyst": 0.42, "fundamental": 0.28, "pattern": 0.18, "sentiment": 0.12},
  "learned_composite_score": 0.71,
  "default_weights": {"catalyst": 0.35, "fundamental": 0.30, "pattern": 0.20, "sentiment": 0.15},
  "default_composite_score": 0.68,
  "outcome_pnl_pct": 4.2,
  "outcome_reward": 3.1
}
```

**Operator override:** The operator can force a revert to default weights at any time via Telegram (`/config weights default`). The learned weights are preserved in the database for analysis but not used for scoring until the operator re-enables them (`/config weights learned`).

#### 13.1.6 Data Collection Requirements for Phase 1

To enable reward-weighted signal calibration, the following data must be logged from day one of Phase 1 operation, beyond what is already specified in Section 8 (Performance Tracking):

**Per-trade state vector (add to `trades` table):**

```sql
ALTER TABLE trades ADD COLUMN state_vector JSON;
-- Contents:
-- {
--   "macro_regime": "risk-on",
--   "sector": "Technology",
--   "setup_type": "earnings_beat",
--   "catalyst_score": 0.82,
--   "fundamental_score": 0.71,
--   "pattern_score": 0.65,
--   "sentiment_score": 0.45,
--   "market_volatility": "medium",
--   "vix_level": 18.3,
--   "time_since_catalyst_hours": 4.2,
--   "stock_move_since_catalyst_pct": 2.1,
--   "market_cap_bucket": "mega",
--   "sp500_distance_from_200dma_pct": 3.5
-- }
```

**Per-trade weight log (add to `trades` table):**

```sql
ALTER TABLE trades ADD COLUMN weights_used JSON;
-- Contents: {"catalyst": 0.35, "fundamental": 0.30, "pattern": 0.20, "sentiment": 0.15}
-- In Phase 1, this is always the static defaults. The column exists so the
-- infrastructure is ready when learned weights are introduced.
```

**Operator decision log (new table):**

```sql
CREATE TABLE operator_decisions (
    id INTEGER PRIMARY KEY,
    memo_id TEXT REFERENCES memos(id),
    decision TEXT CHECK(decision IN ('approve', 'modify', 'reject', 'watchlist')),
    operator_reasoning TEXT,           -- Free text from operator (especially valuable for rejections)
    decision_timestamp TEXT,
    time_to_decision_seconds INTEGER,  -- How long the operator took (fast = obvious, slow = uncertain)
    modifications JSON                 -- If modified, what changed (entry, size, stops)
);
```

**Opus critique log (new table):**

```sql
CREATE TABLE opus_critiques (
    id INTEGER PRIMARY KEY,
    memo_id TEXT REFERENCES memos(id),
    critique_text TEXT,                -- Full Opus stress-test output
    bear_case_summary TEXT,            -- Extracted bear case
    identified_risks JSON,             -- Structured risk factors Opus flagged
    conviction_delta REAL,             -- How much Opus adjusted Sonnet's initial score
    critique_timestamp TEXT
);
```

**Claude API call archive (new table):**

```sql
CREATE TABLE api_call_archive (
    id INTEGER PRIMARY KEY,
    call_timestamp TEXT,
    model TEXT,                        -- haiku, sonnet, opus
    task_type TEXT,                     -- catalyst_analysis, scoring, memo_draft, etc.
    ticker TEXT,
    prompt_hash TEXT,                   -- Hash of full prompt (for deduplication)
    prompt_text TEXT,                   -- Full prompt sent to Claude
    response_text TEXT,                 -- Full response received
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    associated_memo_id TEXT
);
```

This table is large but invaluable. It serves triple duty: (1) debugging individual trade analyses, (2) fine-tuning data for Section 13.4, and (3) prompt engineering — identifying which prompt formulations correlate with better trade outcomes. Storage cost is negligible relative to its future value. Consider rotating or compressing records older than 6 months if space becomes an issue, but never delete them.

### 13.2 Position Sizing Optimization

Position sizing is where the problem shifts from contextual bandits to genuine reinforcement learning. The critical difference: the optimal size for a new position depends on what you already hold. If you're already 15% exposed to semiconductors and a new AMD catalyst appears, the right size depends on your NVDA and AVGO positions, their correlation, your available cash, and how close you are to a risk limit. A static formula (Section 7.3's `base × regime × conviction × vol`) can't capture this because it evaluates each trade in isolation.

**Problem formulation:**

- **State:** (portfolio composition vector, pairwise correlation matrix of held positions, macro regime, new opportunity's conviction score, recent portfolio volatility, available cash as % of portfolio, distance from drawdown circuit breaker, sector concentration by position)
- **Action:** Continuous position size for the new trade, constrained to [2%, 10%] of portfolio
- **Reward:** The new position's contribution to portfolio-level Sharpe ratio over its lifetime. This is explicitly NOT the individual trade's P&L — a 6% winner that pushes portfolio concentration risk to dangerous levels is a worse sizing decision than a 4% winner that diversifies the book.
- **Method:** Proximal Policy Optimization (PPO) or Soft Actor-Critic (SAC), both designed for continuous action spaces. SAC is preferred for its entropy regularization, which encourages exploration and prevents premature convergence to a single sizing strategy.

**Why rules-based formulas are suboptimal:**

The current Phase 1 formula multiplies independent factors: base size × regime × conviction × volatility. This misses interaction effects. Consider: in a risk-off regime with high conviction on a low-volatility defensive name when the portfolio is already 60% cash — the formula would give a moderate position size (regime penalizes, conviction boosts). But the actual optimal action is a large position because (a) the portfolio has excess cash earning nothing, (b) the name is defensive and fits the regime, and (c) conviction is high. The RL agent can learn these interactions; the formula cannot.

**Data requirements:** ~500+ completed trades with full portfolio state snapshots at entry time. This is a larger dataset than signal calibration (#1) because the state space is inherently larger (portfolio composition is high-dimensional). Partially addressable through simulation: reconstruct historical portfolios by replaying the Phase 1 trade log with different sizing decisions and computing counterfactual portfolio-level Sharpe ratios.

**Implementation sketch:**

```python
class PositionSizingEnv:
    """OpenAI Gym-compatible environment for position sizing."""

    def __init__(self, portfolio_state, opportunity):
        self.state = self._encode_state(portfolio_state, opportunity)

    def _encode_state(self, portfolio, opportunity):
        return {
            "cash_pct": portfolio.cash / portfolio.total_value,
            "num_positions": len(portfolio.positions),
            "sector_concentrations": portfolio.sector_exposure_vector(),  # 11-dim
            "portfolio_vol_30d": portfolio.realized_vol(30),
            "drawdown_from_peak_pct": portfolio.drawdown_from_peak(),
            "max_correlation_with_existing": portfolio.max_corr(opportunity.ticker),
            "opportunity_conviction": opportunity.composite_score,
            "opportunity_sector": opportunity.sector,
            "regime": portfolio.current_regime,
        }

    def step(self, action):
        """action = position size as fraction of portfolio [0.02, 0.10]"""
        # Execute trade at this size, observe portfolio-level outcome
        # Reward = contribution to portfolio Sharpe
        pass
```

**Timeline:** Feasible at ~18-24 months, after signal calibration (#1) is stable and sufficient trade data has accumulated.

### 13.3 Exit Timing Optimization

This is the purest reinforcement learning problem in the system — a true sequential decision process where the agent makes repeated hold/sell decisions over the life of a position, and each decision affects the final outcome. The current exit logic (fixed stop-loss, fixed targets, time-based exit) leaves significant alpha on the table. A learned exit policy can adapt to changing conditions during a trade's lifetime.

**Problem formulation:**

- **State:** (entry price, current price, unrealized P&L %, days held, current macro regime, regime changes since entry, new catalysts since entry, volume trend vs. entry-day volume, ATR trend — expanding or contracting, distance from stop-loss, distance from target, portfolio-level drawdown since entry)
- **Action:** Discrete — {hold, scale out 25%, scale out 50%, close position, tighten stop to breakeven, tighten stop to 50% of gain}
- **Reward:** Final trade P&L minus opportunity cost. Opportunity cost is defined as the risk-free rate plus the average return of trades entered during the holding period. This prevents the agent from learning "just hold everything forever" — if the capital could have been deployed in a better opportunity, holding a mediocre winner is penalized.

```python
def exit_reward(trade):
    """
    Reward that accounts for opportunity cost of capital.
    """
    trade_return = trade.final_pnl_pct
    holding_days = trade.exit_date - trade.entry_date

    # What could this capital have earned?
    risk_free_cost = daily_risk_free_rate * holding_days
    # Average return of trades that entered while this capital was locked up
    opportunity_cost = avg_return_of_concurrent_entries(trade.entry_date, trade.exit_date)

    reward = trade_return - max(risk_free_cost, opportunity_cost)
    return reward
```

- **Method:** Dueling DQN for the discrete action space. The dueling architecture separates state-value estimation from action-advantage estimation, which helps when many states have similar values regardless of action (e.g., a position that's already at the stop-loss — the action barely matters because the stop will execute anyway).

**What this learns:**
- "When a catalyst-driven trade is up 4% in 2 days but volume is declining, scale out 50% — the easy money is made"
- "When a fundamentally-strong name hits target 1 but a new supporting catalyst just appeared, hold the remaining position with a tightened stop"
- "When macro regime shifts to risk-off while holding a high-beta name, close immediately regardless of P&L"

**Data requirements:** Similar to position sizing (~500+ completed trades), but each trade contributes multiple training samples (one per day held, with the action taken that day). A trade held for 10 days generates 10 state-action-reward tuples. This means the effective dataset size grows faster than the trade count.

**Timeline:** Feasible at ~18-24 months, can be developed in parallel with position sizing.

### 13.4 Fine-Tuned Judgment Models

After 12+ months of operation, the system accumulates a unique dataset: (catalyst description, Opus analysis, operator decision, trade outcome) tuples. This is training data for a fine-tuned model that learns the operator's specific investment judgment — the ineffable pattern recognition that makes some catalyst + fundamental combinations compelling and others not, even when the quantitative scores are similar.

The concept: fine-tune a smaller model (Haiku-class) on the operator's decision history to create a personalized "Intore Core" model. This model pre-scores opportunities before they reach Sonnet or Opus, acting as a learned filter that reflects the operator's taste, risk tolerance, and domain expertise.

**Concrete example:** The operator consistently rejects high-scoring biotech setups because they don't trust binary FDA outcomes. The operator consistently approves lower-scoring industrials with insider buying because they view insiders as the most informative signal in that sector. The fine-tuned model learns these preferences and adjusts its pre-screening accordingly — fewer biotech memos that get rejected, more industrial insider plays surfaced proactively.

**Architecture:** Sits between Sonnet and Opus in the escalation chain. Sonnet produces the analysis, the fine-tuned model pre-scores it, and only ideas that pass the fine-tuned model's filter get escalated to Opus. This reduces Opus API costs by ~30-50% while improving signal-to-noise ratio in the memos the operator actually sees.

**Success metric:** Does the fine-tuned model's ranking of opportunities correlate with actual outcomes better than the generic models? Measure rank correlation (Spearman's rho) between the fine-tuned model's conviction scores and realized trade P&L, compared against Opus's scores on the same trades.

### 13.5 Adversarial Thesis Stress-Testing / Self-Play

Inspired by Bridgewater Associates' structured debate process, but automated. Train two competing models: a Bull Advocate and a Bear Advocate. Each model specializes in one side of the argument.

- The Bull model is rewarded when trades it championed succeed (positive P&L at exit)
- The Bear model is rewarded when trades it argued against fail (negative P&L or stop-out)

Over time, both models become increasingly sophisticated at constructing and deconstructing trade theses. The Bull model learns which catalyst-fundamental combinations genuinely predict price appreciation. The Bear model learns which surface-level-attractive setups have hidden risks (valuation traps, catalyst already priced in, adverse sector dynamics).

**Concrete example:** Bull Advocate on a semiconductor earnings beat: "Revenue acceleration + margin expansion + hyperscaler capex cycle = strong setup, pattern shows 78% win rate." Bear Advocate response: "Yes, but this is the 4th consecutive beat — the market has extrapolated this already (forward P/E at 98th percentile). Historical pattern for 'beating raised expectations' at this valuation is actually negative — mean reversion dominates. And three sell-side analysts upgraded this week, which is a contrarian negative signal." The operator (and eventually the system itself) evaluates which argument is stronger. The outcome data trains both models.

### 13.6 Meta-Learning Across Setup Types

The cold-start problem: when the system encounters a novel catalyst type (e.g., "AI chip export ban" or "GLP-1 competitor data"), it has no historical data for that specific setup. Meta-learning addresses this by learning a base "catalyst trade" policy that rapidly adapts to new setup types with minimal data.

Using Model-Agnostic Meta-Learning (MAML) or a similar approach, the system learns initial weight configurations that are few-shot-adaptable. When a new setup type appears, the system already has useful priors from structurally similar setups. An "AI chip export ban" borrows priors from "tariff catalyst" and "regulatory catalyst" setups, adapting with just 3-5 observed outcomes instead of the 20+ that would otherwise be needed.

This is particularly valuable because the catalyst landscape evolves — new types of market-moving events emerge regularly, and a system that requires 200 observations before it can score a new setup type will always be behind.

### 13.7 Constitutional Trading Principles

Inspired by Anthropic's Constitutional AI work. The idea: define a set of inviolable epistemic and behavioral principles that govern not just risk limits (those already exist in Section 7.4) but the quality of reasoning the system uses to make decisions.

**Example principles:**

- "Never increase conviction based on sunk cost. A losing position does not become a better trade because you've already lost money on it."
- "Weight recent evidence more heavily than distant evidence, but do not ignore base rates. A single strong quarter does not override five mediocre ones."
- "Be more skeptical of consensus narratives. When sell-side analysts, Reddit, and financial media all agree, the informational edge is gone."
- "Prefer trades with independent signal convergence. A trade where catalyst, fundamental, and pattern signals all point the same direction for different reasons is stronger than one where a single signal dominates the score."
- "Distinguish between 'this is a good company' and 'this is a good trade.' The best companies at the wrong price or wrong time are bad trades."

The constitution acts as a regularizer on the RL-learned policies. When the RL agent discovers a strategy that works empirically but violates a constitutional principle (e.g., it learns to average down into losers because a few happened to recover), the constitutional layer flags and penalizes this behavior.

Over time, the operator's role shifts from approving individual trades to tuning the constitution — updating the principles that govern how the system thinks, rather than micromanaging what it does.

### 13.8 Data Collection Checklist — Phase 1 Requirements for RL Readiness

The following data collection requirements must be implemented in Phase 1 to enable all learning systems described above. Items marked [NEW] are additions beyond what is already specified in Section 8 (Performance Tracking). Items marked [EXISTING] are already specified but called out here to emphasize their importance for the RL pipeline.

**Per-trade logging:**

- [EXISTING] Entry and exit prices, dates, fill quality, P&L, holding period
- [EXISTING] Setup type classification, composite score, individual signal scores, macro regime
- [EXISTING] Exit reason (stop-loss, target, time, manual)
- [NEW] Full state vector at entry time (Section 13.1.6 schema)
- [NEW] Weights used for scoring (static defaults in Phase 1; creates infrastructure for learned weights)
- [NEW] Max adverse excursion (MAE) — the worst unrealized loss during the trade's lifetime
- [NEW] Max favorable excursion (MFE) — the best unrealized gain during the trade's lifetime
- [NEW] Daily position snapshots: price, unrealized P&L, ATR, volume ratio vs. average (needed for exit timing RL)
- [NEW] Portfolio state snapshot at entry: all held positions, sector exposures, cash %, correlation matrix, drawdown from peak (needed for position sizing RL)

**Operator decision logging:**

- [EXISTING] Approval status per memo
- [NEW] Operator reasoning for rejections (free text — even a few words like "don't trust the setup" is valuable)
- [NEW] Time-to-decision in seconds (proxy for confidence — fast decisions suggest obvious accept/reject)
- [NEW] Modifications made on approval (what did the operator change and why?)

**Model output archival:**

- [NEW] Full Opus critique and stress-test output per scored opportunity (Section 13.1.6 `opus_critiques` table)
- [NEW] Full Claude API prompts and responses for all analysis calls (Section 13.1.6 `api_call_archive` table)
- [NEW] Haiku pre-screening scores for ALL catalysts, including those that didn't escalate to Sonnet (needed to train a better pre-screening model later)

**Counterfactual logging:**

- [EXISTING] Log of opportunities that scored >= 0.40 but below the 0.55 memo threshold
- [NEW] Forward returns on watchlisted but not-traded opportunities (track what would have happened — this is "free" data for the RL system since no capital was at risk)
- [NEW] Forward returns on rejected opportunities (same principle — what did the operator leave on the table?)

### 13.9 Timeline

| Phase | System | Earliest Feasibility | Data Prerequisite | Complexity |
|-------|--------|---------------------|-------------------|------------|
| 4a | Reward-Weighted Signal Calibration (13.1) | ~12 months (with historical bootstrapping) or ~18 months (live data only) | ~200 completed trades | Low — contextual bandits, no neural networks |
| 4b | Position Sizing Optimization (13.2) | ~18-24 months | ~500 completed trades + portfolio state logs | Medium — continuous action RL, portfolio-level reward |
| 4c | Exit Timing Optimization (13.3) | ~18-24 months | ~500 completed trades + daily position snapshots | Medium — sequential decisions, discrete action RL |
| 5a | Fine-Tuned Judgment Model (13.4) | ~12-18 months | ~300 operator decisions with reasoning | Medium — fine-tuning infrastructure required |
| 5b | Adversarial Self-Play (13.5) | ~24+ months | Mature base system + sufficient thesis/outcome pairs | High — multi-agent training, reward design |
| 5c | Meta-Learning (13.6) | ~24+ months | Multiple setup types with 20+ observations each | High — MAML implementation, careful evaluation |
| 5d | Constitutional Principles (13.7) | ~24+ months (but constitution can be drafted immediately) | Mature RL system to regularize | Medium — the framework is simple, calibration is hard |

Phases 4a-4c can be developed somewhat in parallel once the data is available. Signal calibration (4a) should be first because it's simplest, lowest-risk, and validates the entire RL infrastructure. Phases 5a-5d are more speculative and should be sequenced based on what the earlier phases reveal about the system's bottlenecks.

---

## 14. Open Questions & Future Considerations

- **Short selling:** Phase 1 is long-only for simplicity. Short setups add complexity (borrow availability, unlimited risk profile). Consider adding in Phase 2.
- **Options strategies:** Could express views with defined-risk options instead of equity. Significantly more complex. Phase 3 at earliest.
- **News source expansion:** Bloomberg, Reuters, Dow Jones feeds are expensive but much faster than free sources. Evaluate if latency matters for swing trading (probably not much).
- **Earnings whisper numbers:** Community-sourced earnings estimates sometimes differ from consensus. Could be a signal layer.
- **Alternative data:** Satellite imagery, credit card data, web traffic — all interesting but expensive and complex. Out of scope.
- **Tax optimization:** For real trading (Phase 3), wash sale rules and tax-loss harvesting become relevant.

---

## 15. Post-Build Findings & Design Decisions (Feb 2026)

This section captures decisions and bugs found during initial testing of the Phase 1 build.

### 15.1 Direction Logic Overhaul

**Problem found:** The system always outputs "SHORT" in memo headers because `memo/generator.py` uses `"long" if catalyst.direction == "bullish" else "short"`. Since Haiku/Sonnet can return `bullish`, `bearish`, or `ambiguous`, and the AgentOutput default is `"neutral"`, anything that isn't exactly `"bullish"` falls through to SHORT. This also corrupts the scoring engine's direction alignment check, causing false disagreement penalties.

**Decision:**
- Standardize direction values as an enum: `bullish`, `bearish`, `neutral`. Treat `ambiguous` as `neutral` on input.
- Derive primary direction from the highest-confidence non-neutral signal (not just catalyst).
- For Phase 1 (long-only), default to LONG when no signal has a strong directional opinion.
- `neutral` and `ambiguous` should NOT trigger direction disagreement penalties — they represent absence of opinion, not opposition.

### 15.2 Scoring Weight Adjustments

**Problem found:** Pattern (20%) and Reddit Sentiment (15%) agents are stubs returning 0.5, anchoring 35% of the composite score to mediocrity. This makes it nearly impossible for any ticker to cross the 0.55 memo threshold.

**Decision:**
- Phase 1a (Reddit still stubbed, Pattern being built): catalyst 40%, fundamental 30%, pattern 22%, sentiment 8%
- Phase 1b (after Pattern Agent is live): keep above weights, validate with initial results
- Phase 2 (after 50+ closed trades): use signal attribution data to optimize weights empirically
- Long-term: explore RL/Bayesian optimization using trade P&L as reward signal

### 15.3 Pattern Agent Redesign

**Problem found:** Original spec implied Pattern Agent needed trade history to function. This creates a cold-start problem where the most data-hungry agent can never bootstrap.

**Decision:** Pattern Agent will use historical market data (FMP earnings surprises, SEC filings, yfinance 5yr price data) to find analogous past setups for any thesis. Our own trade history supplements this over time but is NOT required to start. See `specs/pattern-agent-spec.md` for full implementation spec.

**Key design choice:** This is empirical outcome analysis ("what happened in similar situations?"), NOT technical analysis (no RSI, MACD, Bollinger Bands, etc.). Sonnet classifies setups and interprets statistical patterns.

### 15.4 Future: Training / RL Loop

Once sufficient trade data accumulates (50+ closed trades), explore:
- Bayesian weight optimization using signal attribution + trade P&L
- Prompt tuning based on which catalyst descriptions led to winning vs losing trades
- Setup classification refinement based on which setup types actually predict winners
- Graduated autonomy: high-confidence, well-validated setup types earn autonomous execution in Phase 2

---

*Last updated: February 2026*
*Version: 1.2 — Phase 1 Build + Initial Testing + RL Roadmap*
