# Historical Pattern Analysis Robust Fix Spec

Last updated: 2026-06-21

## Executive Summary

The current Pattern Agent mostly answers one narrow question: "What happened after historical earnings beats for this ticker and a small hardcoded peer list?" That is useful for earnings setups, but it fails or gives misleading confidence for product launches, analyst actions, management changes, regulatory events, sector catalysts, negative catalysts, and small/obscure tickers with no hardcoded peers.

The fix is to replace the earnings-proxy path with an Event Analog Engine:

1. Resolve a real peer universe for every ticker using cached structured sources first, then search fallbacks.
2. Build a persistent historical event store across catalyst types.
3. Discover historical event candidates with Gemini Search Grounding first and Perplexity Search API as a fallback/source expander.
4. Extract structured event records from news, transcripts, press releases, filings, and analyst/news reports.
5. Compute forward returns and context from prices after the event timestamp.
6. Rank analogs by catalyst similarity, peer similarity, context similarity, evidence quality, and outcome availability.
7. Return honest status states when the system cannot support a catalyst, rather than silently substituting unrelated earnings-beat data.

Phase 1 only needs the Perplexity Search API. Do not require the Perplexity Agent API to ship the robust fix. Keep Agent API/finance_search as an optional later fallback if peer/profile/transcript coverage is still weak after the FMP + Gemini + Perplexity Search path is measured.

Do not store API keys in this spec, logs, tests, or commits. The key shown in the screenshot should be treated as exposed and rotated before production use.

## Current Failure Modes To Fix

The following issues were observed from production data and local code review:

- `agents/pattern_agent.py` routes unstructured setup types to `_search_earnings_patterns(..., "earnings_beat_guide_up", ...)`. This silently substitutes earnings beats for unrelated catalysts.
- `config/peers.py:get_peers()` returns hardcoded peers for roughly large-cap names and `[]` for everything else, despite its docstring promising sector/market-cap fallback.
- Production `historical_patterns` contains only `earnings_beat_guide_up` rows, so analyst/product/management/sector/negative setups usually have no direct historical base.
- Production `historical_contexts` was empty in the recent audit, so similarity defaults to 0.5 and `highly_similar_count` is often 0.
- Recent events without enough forward price data are filtered out, but the system does not clearly distinguish "no historical event" from "event exists but forward returns are not mature."
- Unsupported/general catalyst types are returned as `no_data` or earnings proxies instead of showing an explicit unsupported/decomposed state.
- Pattern-stage telemetry is not persistently auditable enough. Pipeline runs can succeed while the pattern stage is effectively unavailable.
- Scoring/memo field mismatch exists: downstream code expects `hs_count` and `most_similar_instance`, while the agent emits `highly_similar_count` and `most_similar`.

## Goals

- Historical analogs should cover all first-class catalyst types, not just earnings.
- Peer discovery should work for most public US tickers without a handbuilt massive database.
- Expensive APIs should populate local cache; normal memo generation should mostly read local DB.
- Pattern outputs should be explainable: show event evidence, source URLs, peer/source quality, forward returns, and why an analog was matched.
- Failures should be visible and typed: unsupported, no matches, provider error, insufficient forward returns, or low-confidence peers.
- The implementation should avoid lookahead bias by anchoring events to the first available timestamped source before computing returns.

## Non-Goals

- Do not build a full market-data warehouse in this pass.
- Do not use LLMs to estimate returns; returns must come from price data.
- Do not use Perplexity/Gemini output as the final source of truth without storing citations and extracted event rows.
- Do not score a catalyst using unrelated fallback types unless the memo clearly labels that as broad base-rate evidence.
- Do not depend on Perplexity Agent API/finance_search for the initial rollout.

## Provider Strategy

### Primary Providers

1. FMP structured APIs:
   - Peer discovery via `stock-peers`.
   - Company profile/screener data for sector, industry, exchange, market cap, beta, and candidate fallback peers.
   - Existing earnings, analyst, insider endpoints already used in `data/pattern_data.py`.

2. Gemini API:
   - Existing app already has `gemini_api_key` and `utils/web_search_client.py`.
   - Use Search Grounding for historical event candidate discovery.
   - Use structured JSON output for event extraction and catalyst decomposition.
   - Use Gemini embeddings for event semantic similarity if local dependencies and model availability are acceptable.

3. Perplexity Search API:
   - Add as a raw source discovery fallback and search expander.
   - Endpoint: `POST https://api.perplexity.ai/search`.
   - Response shape includes ranked `results[]` with `title`, `url`, `snippet`, `date`, and `last_updated`.
   - Useful controls: `max_results`, `search_domain_filter`, publication date filters, and `max_tokens_per_page`.
   - Pricing is request-only, no token cost, currently documented at $5 per 1,000 Search API requests.

### Optional Later Provider

Perplexity Agent API with `finance_search`:

- Keep behind a separate feature flag.
- Use only after bakeoff if direct finance/search grounding improves peers or transcript/source discovery materially.
- Current documented tool pricing is $0.005 per invocation for `finance_search`, separate from model token costs.

### Why This Provider Order

Gemini is already integrated and good enough for grounded event discovery plus structured extraction. Perplexity Search is better as a deterministic raw-result fallback because it returns a clean ranked source array with date/domain filters and no token billing. FMP should be the peer backbone because it is structured, cacheable, and more deterministic than web search.

## Environment Variables And Settings

Add to `config/settings.py` and `.env.example`:

```python
perplexity_api_key: str = ""
perplexity_search_enabled: bool = True
perplexity_search_max_requests_per_run: int = 20
pattern_event_search_provider: str = "gemini"  # gemini | perplexity | hybrid
pattern_event_search_enabled: bool = True
pattern_event_cache_ttl_days: int = 90
pattern_peer_cache_ttl_days: int = 30
pattern_max_peer_count: int = 20
pattern_min_direct_matches: int = 5
pattern_min_total_matches: int = 10
pattern_max_search_queries_per_catalyst: int = 8  # TOTAL queries per run, not per-peer
pattern_max_events_per_query: int = 10
pattern_embedding_provider: str = "gemini"  # gemini | perplexity | off
pattern_analog_engine_enabled: bool = False
pattern_stage_wallclock_budget_s: int = 45  # hard cap on inline discovery in the live memo path
pattern_cold_ticker_async_backfill: bool = True  # enqueue backfill instead of doing full fan-out inline
pattern_price_source: str = "fmp"  # fmp | yfinance — outcome/context price source (see Outcome Computation)
```

Flag precedence (document explicitly to avoid confusion): `pattern_analog_engine_enabled` is the master switch. When it is `False`, none of `pattern_event_search_enabled` / `perplexity_search_enabled` / `pattern_cold_ticker_async_backfill` take effect and the legacy path runs unchanged. `pattern_max_search_queries_per_catalyst` is a per-run total budget shared across all peers, NOT a per-peer multiplier.

Rollout rule: leave `pattern_analog_engine_enabled=False` until schema, adapters, tests, and backfill command are present. Then enable locally, test on known failures, and only then enable in Railway.

## Data Model

Add new SQLAlchemy models in `database/models.py`. This repo uses `Base.metadata.create_all()` + manual `ALTER` migrations in `database/db.py:_run_migrations()`. The 6 new tables here are purely additive, so `create_all()` creates them (and their declared indexes) automatically on existing prod DBs — no `ALTER` migration is needed for new tables. Only add to `_run_migrations()` if you later add a column to an EXISTING table; do not invent a migration for the new tables.

### CompanyProfile

Purpose: cache structured profile data used for peers, context, and search query building.

Fields:

- `id`
- `ticker` unique indexed string
- `name`
- `exchange`
- `sector`
- `industry`
- `market_cap`
- `beta`
- `description`
- `country`
- `currency`
- `raw_json`
- `profile_source`
- `updated_at`
- `expires_at`

### PeerEdge

Purpose: persist ranked peers so every analysis does not call APIs.

Fields:

- `id`
- `target_ticker` indexed
- `peer_ticker` indexed
- `rank`
- `score`
- `source` such as `manual`, `fmp_stock_peers`, `fmp_screener`, `correlation`, `perplexity_search`, `gemini_search`
- `reasons_json`
- `as_of_date`
- `expires_at`
- unique constraint on `(target_ticker, peer_ticker, source, as_of_date)`

### PatternSearchRun

Purpose: audit every analog search and explain failures.

Fields:

- `id`
- `run_id`
- `ticker`
- `setup_type`
- `catalyst_hash`
- `status`
- `provider_plan_json`
- `queries_json`
- `peer_set_json`
- `result_counts_json`
- `cost_estimate`
- `duration_s`
- `error`
- `created_at`

Statuses:

- `active`
- `unsupported`
- `decomposed`
- `no_matches`
- `insufficient_forward_returns`
- `provider_error`
- `low_confidence_peers`
- `cache_hit`
- `disabled`

### HistoricalEvent

Purpose: canonical normalized event table. This becomes the source for analog retrieval.

Fields:

- `id`
- `ticker` indexed
- `company_name`
- `event_type` indexed
- `event_subtype`
- `event_date`
- `event_timestamp`
- `event_timing` enum-ish string: `pre_market`, `regular_hours`, `after_hours`, `unknown`
- `polarity` string: `bullish`, `bearish`, `mixed`, `neutral`
- `magnitude` float nullable
- `headline`
- `summary`
- `evidence`
- `source_url`
- `source_domain`
- `source_type` string: `company_ir`, `sec_filing`, `earnings_transcript`, `press_release`, `regulator`, `news`, `analyst_report`, `other`
- `provider`
- `provider_query`
- `confidence`
- `dedupe_key` unique
- `embedding_json` nullable
- `raw_json`
- `created_at`
- `updated_at`

Indexes:

- `(ticker, event_type, event_date)`
- `(event_type, event_date)`
- `(dedupe_key)`

Dedupe key:

```text
sha256(normalized_ticker + event_type + event_date_bucket)
```

where `event_date_bucket` collapses dates within a small window (e.g. same date +/- 1 trading day) so the same real event found on different days does not split. Do NOT include source_domain or headline in the dedupe key: the same event discovered via Gemini vs Perplexity has different domains/headlines, and including them would let duplicates survive and inflate `total_instances`. Instead, when a collision occurs, MERGE: keep the highest-confidence / most-primary `source_type` (company_ir > sec_filing > transcript > press_release > regulator > news > analyst_report > recap), and retain all distinct `source_url`s in `raw_json.sources[]`. Headline/domain are evidence to store, not identity.

### EventOutcome

Purpose: deterministic price outcomes after an event.

Fields:

- `id`
- `event_id` foreign key
- `ticker`
- `anchor_price`
- `anchor_trade_date`
- `return_t1`
- `return_t3`
- `return_t5`
- `return_t10`
- `return_t20`
- `return_t60`
- `abnormal_return_t5`
- `abnormal_return_t10`
- `abnormal_return_t20`
- `benchmark_symbol`
- `sector_benchmark_symbol`
- `max_drawdown_t20`
- `max_drawdown_day`
- `volume_ratio_t1`
- `gap_pct`
- `matured_horizons_json`
- `status` string: `complete`, `partial`, `insufficient_forward_returns`, `price_error`
- `computed_at`

### EventContext

Purpose: similarity inputs at the event date.

Fields:

Point-in-time (PIT) safety is mandatory here: every field must reflect only information that was public on `event_date`. Fields are split into PIT-safe (price/market-derived, computed from history) and PIT-reconstructable (valuation from as-of-filed financials). Forward P/E and short interest are intentionally excluded because they cannot be sourced PIT on the current FMP plan (see "Point-in-Time Sourcing Rules" below).

Fields:

- `id`
- `event_id` foreign key unique
- `macro_regime`            # PIT-safe: derived from VIX/SPY history at event_date
- `vix_level`               # PIT-safe: ^VIX close at event_date
- `sp500_distance_200ma`    # PIT-safe: SPY history
- `sector_momentum_20d`     # PIT-safe: sector ETF history
- `ticker_momentum_20d`     # PIT-safe: ticker price history
- `ticker_volatility_20d`   # PIT-safe: ticker price history
- `market_cap`              # PIT-reconstructable: FMP historical-market-capitalization at event_date
- `trailing_pe_ratio`       # PIT-reconstructable: historical market cap / TTM EPS from financials filed <= event_date (REPLACES fwd_pe_ratio)
- `ev_sales`                # PIT-reconstructable: (historical mkt cap + net debt from last filed balance sheet) / TTM revenue filed <= event_date
- `valuation_source_filing_date`  # accepted/filing date of the financials used; proves no lookahead
- `pit_quality`             # string: full | partial | price_only | unavailable — how much PIT context was sourced
- `raw_json`
- `computed_at`

NOTE: `fwd_pe_ratio` and `short_interest_pct_float` are removed from this model. Do not store current-as-of values here and pretend they are historical. If a future PIT data vendor is added, reintroduce them behind a new flag with their own as-of date.

### Migration Compatibility

Keep existing `HistoricalPattern` and `HistoricalContext` during rollout. The new engine can read them as legacy structured events, but do not extend `HistoricalPattern` to cover semantic events. It is too earnings-shaped and already overloaded.

## Catalyst Taxonomy

Replace broad catch-alls with a taxonomy that can decompose vague catalysts.

Tier A, structured reliable:

- `earnings_beat_guide_up`
- `earnings_beat_guide_flat`
- `earnings_beat_guide_down`
- `earnings_miss`
- `revenue_acceleration`
- `analyst_upgrade_cluster`
- `analyst_downgrade_cluster`
- `insider_cluster_buy`
- `buyback_announcement`
- `dividend_initiation_or_raise`
- `m_and_a_confirmed`
- `fda_or_regulatory_approval`

Tier B, semantic-search supported:

- `product_launch`
- `major_contract_win`
- `partnership_announcement`
- `management_change`
- `pricing_change`
- `strategic_pivot`
- `litigation_resolution`
- `sector_catalyst_positive`
- `sector_catalyst_negative`
- `ai_or_platform_narrative_shift`
- `capital_raise_or_debt_refi`
- `guidance_or_preannouncement`

Tier C, unsupported until decomposed:

- `general_positive_catalyst`
- `general_negative_catalyst`
- `momentum_without_identified_catalyst`
- `rumor_unconfirmed`

Taxonomy reconciliation: this taxonomy renames/extends the current `ALL_SETUP_TYPES` in `agents/pattern_agent.py` (e.g. `analyst_downgrade` -> `analyst_downgrade_cluster`). Update the classifier prompt to emit the new vocabulary, and provide a mapping from legacy `setup_type` strings already stored in `historical_patterns` so old cached rows still resolve under the legacy path during rollout. Do not orphan existing rows.

Tier C behavior:

- Try one LLM decomposition pass using catalyst summary and web research.
- If decomposition yields a Tier A/B type with confidence >= 0.65, continue and set search status `decomposed`.
- If not, return `unsupported` with score 0.5, confidence <= 0.15, and memo copy that pattern analysis did not have a specific catalyst class to test.

## Peer Resolver Design

Create `data/peer_resolver.py` and route `config/peers.py:get_peers()` through it, while keeping manual overrides.

Resolution order:

1. Manual `PEER_GROUPS` from `config/peers.py`.
2. Cached `PeerEdge` rows that are not expired.
3. FMP `stock-peers?symbol={ticker}`.
4. FMP profile + screener fallback:
   - same exchange where possible
   - same sector
   - same or related industry
   - market cap within 0.25x to 4x, prefer 0.5x to 2x
5. Price correlation fallback:
   - use 90 or 180 trading days of returns for candidates from same sector/industry
   - add correlation score when price data exists
6. Perplexity Search fallback:
   - only if fewer than `pattern_max_peer_count / 2` peers or confidence < 0.55
   - query examples:
     - `{ticker} public company closest competitors peers sector market cap`
     - `{company name} competitors publicly traded peers`
7. Gemini Search fallback:
   - use when the business is niche or thematic peers are needed.

Peer scoring formula:

```text
score =
  0.25 * same_industry_score +
  0.20 * market_cap_proximity +
  0.15 * profile_source_quality +
  0.15 * return_correlation +
  0.10 * beta_volatility_similarity +
  0.10 * business_description_similarity +
  0.05 * manual_or_known_override_bonus
```

Return shape:

```python
{
    "ticker": "OSCR",
    "peers": [
        {
            "ticker": "CLOV",
            "score": 0.82,
            "rank": 1,
            "source": "fmp_screener+correlation",
            "reasons": ["same industry: healthcare plans", "market cap 0.8x", "90d corr 0.62"]
        }
    ],
    "status": "active",
    "confidence": 0.74,
    "generated_at": "2026-06-21T..."
}
```

Important: a massive handbuilt peer DB is not necessary. A cached `peer_edges` table with 5,000 tickers and 20 peers each is only about 100,000 rows, which is small. The expensive part is provider calls, so cache aggressively.

## Event Discovery Design

Create `data/event_discovery.py`.

### Search Inputs

Input object:

```python
{
    "target_ticker": "AAPL",
    "company_name": "Apple Inc.",
    "setup_type": "product_launch",
    "catalyst_summary": "...",
    "direction": "bullish",
    "peers": [...],
    "lookback_years": 7,
    "max_events": 50
}
```

### Query Generation

Generate search queries by catalyst type and peer set. Use deterministic templates first, then Gemini for extra query expansion if needed.

CRITICAL — outcome-neutral queries (anti-survivorship-bias rule): discovery queries MUST NOT contain any term that conditions on the price outcome. The entire purpose of the engine is to measure an honest base rate (win rate, median return); if discovery only finds events where "the stock rose," every statistic it produces is upward-biased and a memo reader will trust a structurally optimistic number. Direction/outcome is determined ONLY from price data after the event is found, never from the query.

- Banned tokens in any generated query (case-insensitive): rose, jumped, jump, surged, surge, soared, rallied, rally, popped, plunged, plummeted, sank, fell, dropped, crashed, tanked, gained, gainer, loser, winner, "shares rise", "shares fall", "stock up", "stock down", "best/worst performing", "after beating", "stock reaction" framed as a result.
- Queries should describe the EVENT, optionally with a year/quarter to pin the date, never the result.

Examples (outcome-neutral):

- Product launch:
  - `"{peer}" product launch announcement {year}`
  - `"{peer}" unveils new product press release {year}`
  - `"{peer}" earnings call transcript product roadmap {year}`
- Analyst upgrade cluster:
  - `"{peer}" analyst rating change {year}`
  - `"{peer}" analyst price target revision {year}`
- Management change:
  - `"{peer}" names new CEO {year}`
  - `"{peer}" CEO transition announcement {year}`
- Regulatory approval:
  - `"{peer}" FDA approval announcement {year}`
  - `"{peer}" regulatory decision press release {year}`
- Sector catalyst:
  - `"{industry}" sector regulation announcement {year}`
  - `"{sector ETF}" policy change {year}`

Implementation requirement: a single `assert_outcome_neutral(query)` guard runs on every generated query (deterministic templates AND Gemini-expanded queries) and rejects/strips any containing a banned token. This is covered by a dedicated unit test (see Testing).

Search domains to prefer:

- company IR/newsroom domains
- `sec.gov`
- regulator domains such as `fda.gov`
- reputable finance/news sources
- transcript sources already used or accessible by FMP

### Gemini Search Grounding Path

Use existing `utils/web_search_client.py` but add a method that returns both parsed JSON and grounding metadata. Prompt Gemini to output only event candidates in a strict schema.

Required output:

```json
{
  "events": [
    {
      "ticker": "MSFT",
      "event_type": "product_launch",
      "event_subtype": "AI product launch",
      "event_date": "2023-03-16",
      "event_timestamp": null,
      "event_timing": "unknown",
      "polarity": "bullish",
      "magnitude": 0.7,
      "headline": "Microsoft announces Copilot for Microsoft 365",
      "summary": "Microsoft launched Copilot across Office apps...",
      "evidence": "Source says Microsoft announced Copilot for Microsoft 365...",
      "source_url": "https://...",
      "source_type": "company_ir",
      "confidence": 0.86
    }
  ],
  "queries_used": ["..."],
  "coverage_notes": "..."
}
```

### Perplexity Search Path

Create `utils/perplexity_search_client.py`:

- `search(query, max_results=10, domains=None, after=None, before=None, max_tokens_per_page=512) -> dict`
- `search_many(queries, budget) -> list[SearchResult]`
- Rate-limit and enforce `perplexity_search_max_requests_per_run`.
- Cache raw responses by query hash and filters in DB or local table.

Then feed returned snippets/pages into the same extractor used for Gemini.

Do not use Perplexity Agent API in Phase 1.

## Extraction And Validation

Create `data/event_extractor.py`.

Responsibilities:

- Normalize provider results into `HistoricalEvent`.
- Use structured outputs for extraction.
- Validate required fields.
- Reject weak event rows.
- Dedupe events.
- Preserve source URL and evidence.

Acceptance rules:

- `ticker`, `event_type`, `event_date`, `headline` or `summary`, and at least one source URL are required.
- `event_date` must be a concrete date, not just a year/month.
- `event_date` is the date the catalyst became public, derived from the SOURCE CONTENT (filing date, press-release dateline, transcript date). It must NOT be copied from a provider's result `date`/`last_updated` field — for Perplexity Search and Gemini grounding those reflect the page's publish/crawl date, which is frequently a years-later recap. If the content does not yield a concrete public date, reject the event (do not fall back to the result date).
- `confidence >= 0.55` for storage.
- For analyst/news events, source must be published near the event date where possible.
- For after-the-fact recap articles, use them only to discover the event, then search for or infer the original event date/source. Mark `source_type="recap"` only if no original source is found and reduce confidence.
- If event date is after current date or within the return horizon, store event but outcome status should be `partial` or `insufficient_forward_returns`.

## Outcome Computation

Extend `data/pattern_data.py` or create `data/event_outcomes.py`. Prefer a new file to keep the old adapter stable.

Price source (important — yfinance flakiness/rate-limiting was an original root cause of the empty-result bug): default to FMP historical prices (`pattern_price_source="fmp"`), which is already authenticated on this plan, and fetch a full window once per ticker then slice all horizons from it. yfinance is a fallback only. Whichever source is used, results MUST be cached (a `price_history` cache keyed by ticker+date-range) and rate-limited with backoff, because computing T+1..T+60 + abnormal returns across many events x many peers will otherwise hammer the provider. Batch by ticker: one fetch covers all of that ticker's events.

Anchor rules:

- If `event_timestamp` and `event_timing` are known:
  - pre-market: anchor to same-day open or prior close plus gap metrics; compute returns from first tradable price after news.
  - regular hours: anchor to event-day close unless intraday data is available.
  - after-hours: anchor to next trading day close/open policy consistently.
- If only `event_date` is known:
  - use event-date close for regular/unknown by default.
  - store `event_timing="unknown"` and lower context confidence.

Compute:

- T+1, T+3, T+5, T+10, T+20, T+60 returns.
- Abnormal returns vs SPY and sector ETF when available.
- Gap percent.
- Volume ratio versus prior 20 trading days.
- Max drawdown through T+20.
- Matured horizon list. Do not drop recent events entirely; store partial outcomes.

## Context Backfill

Create `scripts/backfill_event_contexts.py`.

For every `HistoricalEvent` with missing context:

- Pull VIX, SPY, sector ETF, ticker price history around the event (PIT-safe by construction).
- Compute the price-derived fields: macro regime, VIX level, SPY distance from 200-day MA, ticker momentum 20d, ticker volatility 20d, sector momentum 20d.
- Compute PIT-reconstructable valuation (see rules below): market cap, trailing P/E, EV/Sales, and record `valuation_source_filing_date`.
- Set `pit_quality` based on how many fields were sourced (`full` / `partial` / `price_only` / `unavailable`).
- Insert/update `EventContext`.

#### Point-in-Time Sourcing Rules (no lookahead)

These rules are mandatory; they are what make historical context honest rather than leaking the future.

- Market cap at `event_date`: FMP `historical-market-capitalization?symbol=&from=&to=` (do NOT use current `/profile` market cap for a past event).
- Valuation multiples are reconstructed from the most-recently-FILED financials whose `acceptedDate`/`fillingDate <= event_date` — never the fiscal period-end. A quarter ending Dec 31 is not public until the ~Feb filing; using period-end leaks ~6 weeks of hindsight. Use FMP `/income-statement` and `/balance-sheet-statement` (already on this plan) with `period=quarter`, then filter by filing date.
  - `trailing_pe_ratio = historical_market_cap / TTM_diluted_EPS` (TTM from the 4 most recent quarters filed before the event).
  - `ev_sales = (historical_market_cap + total_debt - cash_and_equivalents) / TTM_revenue`, using the last balance sheet filed before the event.
- EXCLUDED — cannot be sourced PIT on the current FMP plan, so they are intentionally NOT stored: forward P/E (FMP `/analyst-estimates` returns only current estimates, no as-of-date snapshot — estimates are revised over time) and short interest (sparse/gated history, float drifts). Do not approximate these with current values.
- If valuation cannot be reconstructed (e.g. missing historical market cap), set those fields null and `pit_quality` accordingly; never fall back to current values.

Verification note: this was assessed against FMP's documented endpoints and the endpoints already proven on this plan, NOT live-tested (no key/network in the review environment). Implementer should confirm `historical-market-capitalization` returns data on the production key during Phase 2; if it does not, escalate and we will pick a PIT source rather than degrade to current-as-of values.

Also add a compatibility script to fill existing `HistoricalContext` rows for current `HistoricalPattern` records so the old path is not degraded during rollout.

## Analog Ranking

Create `data/analog_ranker.py`.

Candidate set:

- Same ticker, same event type.
- Direct peers, same event type.
- High-confidence sector/industry peers, same event type.
- Broad base-rate market examples for rare catalysts.

Rank score:

```text
analog_score =
  0.35 * event_semantic_similarity +
  0.20 * peer_similarity +
  0.15 * context_similarity +
  0.10 * event_type_specific_magnitude_similarity +
  0.10 * source_quality_confidence +
  0.05 * recency_score +
  0.05 * outcome_completeness
```

Embedding cost/availability (semantic similarity is the largest weight at 0.35, but `embedding_json` is nullable and embeddings cost money/latency):

- Bound the candidate set BEFORE embedding. Candidates are first filtered by hard predicates (same `event_type`, ticker in {self, resolved peers, base-rate universe}, outcome maturity) and capped (e.g. top N by recency/peer-score). Only then embed/compare. Never embed the whole event store per memo.
- Compute and cache embeddings at write time (during discovery/backfill), not at ranking time, so the live path reads `embedding_json` from the DB.
- Fallback when an embedding is missing or `pattern_embedding_provider="off"`: substitute a deterministic lexical/structured similarity (event_type exact match + subtype token overlap + magnitude proximity), and renormalize the 0.35 weight across the remaining terms so a null embedding does not silently zero out a candidate. SQLite has no vector index; brute-force cosine over the bounded candidate set is acceptable precisely because the set is capped.

Similarity requirements:

- Same ticker analogs can rank high even with weaker semantic match.
- Peer analogs require both event type match and peer score >= 0.45.
- Broad base-rate analogs must be labeled as broad evidence and not mixed with high-confidence peers without disclosure.
- Context similarity uses ONLY PIT-safe `EventContext` fields (see Data Model). Never weight a historical analog by current-as-of fundamentals.

Output should include evidence tiers:

```python
{
    "status": "active",
    "setup_type": "product_launch",
    "evidence_tiers": {
        "same_ticker": [...],
        "close_peer": [...],
        "sector_peer": [...],
        "broad_base_rate": [...]
    },
    "summary_stats": {...},
    "top_analogs": [...],
    "warnings": [...]
}
```

## Pattern Agent Integration

Modify `agents/pattern_agent.py` in an additive path:

1. Classify/decompose setup.
2. Resolve peers with `PeerResolver`.
3. If `pattern_analog_engine_enabled`:
   - run event analog engine for Tier A/B types
   - fallback to legacy structured methods only for earnings/insider/analyst if event engine returns provider error
4. If disabled:
   - keep legacy behavior for rollout safety.
5. Never route unstructured setup types to earnings beats.
6. Emit consistent raw_data keys:
   - `status`
   - `setup_type`
   - `setup_type_used`
   - `fallback_note`
   - `same_ticker_instances`
   - `peer_instances`
   - `broad_base_rate_instances`
   - `total_instances`
   - `highly_similar_count`
   - `hs_count` for backwards compatibility
   - `most_similar`
   - `most_similar_instance` for backwards compatibility
   - `top_analogs`
   - `provider_usage`
   - `search_run_id`
   - `warnings`

Scoring behavior:

- `active`: score from analog stats and similarity.
- `decomposed`: same as active, but slightly lower confidence unless evidence is strong.
- `no_matches`: score 0.5, confidence 0.15.
- `unsupported`: score 0.5, confidence 0.10.
- `insufficient_forward_returns`: score 0.5, confidence 0.20, include event count and partial outcomes.
- `provider_error`: score 0.5, confidence 0.10, include provider error metadata.
- `low_confidence_peers`: allow same-ticker/broad evidence but cap confidence at 0.35.

## Memo And Scoring Changes

Update memo rendering so historical precedent is not a black box:

- Show status when unavailable: "Pattern analysis unsupported for vague/general catalyst" or "No matured forward-return analogs yet."
- Show evidence tiers:
  - same ticker
  - close peers
  - sector peers
  - broad base rate
- For top 3 analogs show:
  - ticker
  - date
  - event headline
  - T+10/T+20 returns
  - source domain
  - similarity score
- Show a warning if broad base-rate evidence dominates.

Update `scoring/engine.py` so unsupported/no-data pattern output does not drag a strong catalyst down. Be precise about the mechanism: the existing disagreement penalty already excludes `neutral` directions, so direction is NOT the problem. The problem is the raw weighted composite (`raw_score = ... + pattern.score * SIGNAL_WEIGHTS["pattern"]`): a `no_data`/`unsupported` pattern returns `score=0.5` at full pattern weight (0.22), which pulls an 0.8 catalyst toward the mean regardless of direction.

Required fix: when `pattern.raw_data["status"]` is NOT in `{active, decomposed}`, EXCLUDE pattern from the weighted sum and renormalize the remaining signal weights to sum to 1 (redistribute pattern's weight pro-rata across catalyst/fundamental/sentiment). Do the same for any other agent that reports a non-active/no-opinion status, so "no opinion" means "not counted," not "counted as 0.5." 

Acceptance test: `strong_catalyst + unsupported_pattern` must score >= `strong_catalyst + pattern_absent` and must not score lower than the same inputs with a neutral-but-active pattern at 0.5.

## Telemetry And Cost Tracking

Persist:

- Search run status and provider usage in `PatternSearchRun`.
- Per-provider request count and rough cost in `provider_usage`.
- Query list and domains used.
- Number of events discovered, extracted, deduped, rejected, and matured.
- Peer resolver source counts and confidence.

Log events:

- `peer_resolution_start/complete`
- `pattern_event_search_start/complete`
- `event_extraction_rejected`
- `event_outcome_computed`
- `analog_ranked`
- `pattern_status`

### Live-path budget and cold-ticker policy (prevents the timeout/cost blowup on uncached tickers)

The tickers that fail today (`OSCR`, `HNGE`, `DFTX`) are obscure and uncached, so a naive implementation would run the FULL fan-out inline on first request: peer resolution (FMP + maybe Perplexity + Gemini) + discovery across up to `pattern_max_peer_count` peers x `pattern_max_search_queries_per_catalyst` queries x `pattern_max_events_per_query` events + an extraction LLM call per result + a price fetch per event + context per event. That is dozens of LLM calls and potentially hundreds of price fetches inside one memo, and it will exceed `parallel_timeout_pattern_s` and the cost caps — on exactly the tickers this project is meant to fix.

Mandatory two-phase, budget-bounded flow:

- The live pattern stage enforces a hard wall-clock budget `pattern_stage_wallclock_budget_s` (default 45s) AND the per-run request caps, whichever binds first.
- Within budget the live stage does: peer resolution (cached first) + a CAPPED discovery pass that stops as soon as it reaches `pattern_min_total_matches` or exhausts the budget.
- If the budget/caps are hit before reaching `pattern_min_total_matches` and `pattern_cold_ticker_async_backfill=True`: enqueue an async backfill job (peers + full discovery + outcomes + context) and RETURN THIS RUN with a typed partial status (`insufficient_forward_returns`, `no_matches`, or `low_confidence_peers` as applicable) plus whatever partial evidence was gathered. The next run for that ticker reads warm cache.
- Backfill jobs run outside the memo hot path (scheduled/queue worker) and populate `HistoricalEvent`/`EventOutcome`/`EventContext`/`PeerEdge`.
- Acceptance: a cold obscure ticker must return within the pattern stage timeout and must never block memo generation on unbounded provider fan-out.

### Caching and gating

- Cache peers for 30 days by default.
- Cache search results by query/filter hash for 90 days.
- Cache extracted events forever unless source changes; events are historical.
- Cache price history per ticker+range so outcome/context computation does not refetch.
- Run expensive search only when local DB has fewer than `pattern_min_total_matches`.
- Cap Perplexity Search API calls per ticker/run.
- For scheduled scans, only run semantic event discovery after catalyst confidence passes existing escalation threshold.
- Add a backfill command for known catalyst types and peers so production memo path is mostly local cache.

Expected steady-state:

- First run for a new obscure ticker: several FMP/Gemini/Perplexity calls.
- Repeated runs: DB reads plus price outcome refresh for partial events.

## Implementation Phases

### Phase 0 - Safety And Secrets

- Add `.env.example` entries without real values.
- Add settings.
- Ensure screenshots/API keys are not copied into repo.
- Rotate the Perplexity key from the screenshot before using it in production.
- Redact persisted provider payloads: `PatternSearchRun.provider_plan_json`/`queries_json` and `HistoricalEvent.raw_json` will contain raw provider responses, and provider error envelopes can echo the request (including the API key). Route every persisted blob through the existing redaction helper used by the Robinhood broker audit path before writing. Add a test asserting no stored blob contains a key-shaped token.

### Phase 1 - Peer Resolver

Files:

- `data/peer_resolver.py`
- `config/peers.py`
- `database/models.py`
- `tests/test_peer_resolver.py`

Tasks:

- Add `CompanyProfile` and `PeerEdge`.
- Implement manual, cached, FMP stock-peers, FMP screener/profile, correlation, Perplexity, Gemini fallback order.
- Keep a pure/offline mode for tests with mocked provider clients.
- Change `get_peers(ticker)` to use resolver when settings/session is available, else manual map.
- Verify recent failures like `OSCR`, `HNGE`, `DFTX` get non-empty peer candidates or an explicit low-confidence status.

### Phase 2 - Event Store And Outcome Engine

Files:

- `database/models.py`
- `data/event_outcomes.py`
- `scripts/backfill_event_outcomes.py`
- `tests/test_event_outcomes.py`

Tasks:

- Add `HistoricalEvent`, `EventOutcome`, `EventContext`, `PatternSearchRun`.
- Implement outcome computation with partial maturity.
- Implement context computation.
- Add legacy backfill for existing `HistoricalPattern` rows.

### Phase 3 - Gemini Event Discovery

Files:

- `data/event_discovery.py`
- `data/event_extractor.py`
- `utils/web_search_client.py`
- `tests/test_event_discovery_gemini.py`

Tasks:

- Add strict event-candidate schema.
- Return grounding metadata and queries.
- Store events with dedupe.
- Add fixtures for product launch, management change, analyst cluster, and regulatory approval.

### Phase 4 - Perplexity Search Fallback

Files:

- `utils/perplexity_search_client.py`
- `data/event_discovery.py`
- `tests/test_perplexity_search_client.py`

Tasks:

- Add Search API client.
- Use Search API only, not Agent API.
- Add domain/date filter support.
- Add per-run request budget and cache.
- Use Perplexity when Gemini returns too few sources, source quality is low, or query requires tighter domain/date filtering.

### Phase 5 - Analog Ranking And Agent Integration

Files:

- `data/analog_ranker.py`
- `agents/pattern_agent.py`
- `memo/templates/ic_memo.py`
- `memo/generator.py`
- `scoring/engine.py`
- `tests/test_pattern_agent_event_analogs.py`
- `tests/test_memo_pattern_analogs.py`

Tasks:

- Add event analog path behind `pattern_analog_engine_enabled`.
- Remove earnings proxy for unstructured types.
- Add honest statuses.
- Emit backward-compatible raw_data keys.
- Render evidence tiers and top analogs in memos.
- Ensure unsupported/no-data pattern output is not treated as bearish evidence.

### Phase 6 - Backfill And Bakeoff

Files:

- `scripts/backfill_historical_events.py`
- `scripts/evaluate_pattern_analog_engine.py`
- `docs` or `specs` bakeoff notes if needed.

Tasks:

- Seed peer edges for current universe/watchlist.
- Backfill events for Tier A/B catalyst types across top peers.
- Evaluate 20-30 known examples, including recent failures.
- Determinism: the evaluation replays from STORED `HistoricalEvent`/`EventOutcome` rows (not live Gemini/Perplexity, which drift run-to-run). Discovery is run once to populate the store; the bakeoff reads the store so results are reproducible. Discovery unit tests use frozen fixtures.
- Bias control: include a neutral-query control. Because survivorship bias is the top correctness risk (H1), the bakeoff must report measured win rates and median returns and sanity-check them against a plausible base rate. A computed win rate that is implausibly high (e.g. >75% across a broad event class) is a red flag that outcome-conditioned discovery leaked in — investigate before trusting the engine.
- Compare:
  - legacy pattern status
  - new status
  - analog count
  - direct/peer/broad mix
  - provider calls
  - cost estimate
  - measured win rate / median return (with bias sanity-check)
  - memo usefulness

## Bakeoff Examples

Include at least:

- Recent failures from production: `DFTX`, `OSCR`, `HNGE`, `AAPL`.
- Large-cap product/AI launches.
- Analyst upgrade clusters.
- Management change examples.
- Regulatory approval examples.
- Negative catalysts and sector catalysts.
- Earnings beat examples to ensure legacy quality does not regress.

## Acceptance Criteria

- `get_peers("OSCR")` and other non-hardcoded tickers no longer return empty without attempting structured fallback.
- `product_launch`, `management_change`, `regulatory_approval`, `sector_catalyst_*`, and `general_*` no longer silently route to `earnings_beat_guide_up`.
- Pattern output includes a typed `status` for every run.
- At least four non-earnings catalyst classes can produce historical analog rows from search-backed event discovery.
- Recent events with immature T+10/T+20 returns are stored as partial, not discarded.
- Existing earnings examples still work.
- Memo historical precedent section shows evidence tiers or an explicit unavailable reason.
- A cold, uncached, obscure ticker returns within the pattern-stage wall-clock budget and never blocks memo generation on unbounded provider fan-out.
- No generated discovery query contains an outcome-conditioned token (anti-survivorship-bias guard).
- `EventContext` for a past event never contains current-as-of forward P/E or short interest; valuation carries a `valuation_source_filing_date <= event_date`.
- A strong catalyst paired with an unsupported/no-data pattern does not score lower than the same catalyst with pattern absent.
- Provider calls are cached and capped.

Tests must cover:

- Peer fallback (manual / FMP / screener / correlation / search), including `OSCR`/`HNGE`/`DFTX` returning non-empty candidates or an explicit low-confidence status.
- `assert_outcome_neutral` query guard rejects banned outcome tokens (H1).
- Point-in-time context: a past-dated event reconstructs valuation only from financials filed before the event; forward P/E and short interest are absent (H3).
- Cold-ticker budget: discovery halts at the wall-clock/request cap and returns a typed partial status; async backfill is enqueued (H2).
- Scoring: pattern weight is dropped and renormalized when status not in {active, decomposed} (H4).
- Cross-provider dedup: the same event from Gemini and Perplexity collapses to one `HistoricalEvent` with merged sources (M1).
- Event extraction: `event_date` derived from content, rejected when only a provider result `date` is available (M4).
- Embedding-absent fallback ranks candidates without an embedding instead of zeroing them (M3).
- Event outcome maturity (partial vs complete vs insufficient).
- Unsupported / decomposed / no-match catalyst statuses.
- Perplexity fallback request budget enforced.
- Pattern-agent raw_data compatibility: BOTH `hs_count` and `highly_similar_count`, and BOTH `most_similar` and `most_similar_instance`, are populated and consumed correctly by the scoring path (`scoring/engine.py`) AND the memo path (`memo/templates/ic_memo.py`), which currently read different keys.
- Redaction: no persisted provider blob contains a key-shaped token (M5).

## Quick Questions For Bryan

Defaults are specified so implementation can start without blocking.

1. Should Phase 1 cover US-listed equities only?
   - Default: yes. Avoid ADR/international edge cases until the core path works.
2. Are we comfortable rotating the Perplexity key from the screenshot and adding the new key only through Railway/local `.env`?
   - Default: yes. Treat the screenshot key as exposed.
3. Should Agent API/finance_search stay out of scope until after the bakeoff?
   - Default: yes. Search API is enough for Phase 1; Agent API only if measured gaps remain.

## References

- Perplexity Search API: https://docs.perplexity.ai/api-reference/search-post
- Perplexity pricing: https://docs.perplexity.ai/docs/getting-started/pricing
- Gemini Search Grounding: https://ai.google.dev/gemini-api/docs/google-search
- Gemini embeddings: https://ai.google.dev/gemini-api/docs/embeddings
- Gemini structured outputs: https://ai.google.dev/gemini-api/docs/structured-output
- FMP Stock Peer Comparison API: https://site.financialmodelingprep.com/developer/docs/stable/peers
