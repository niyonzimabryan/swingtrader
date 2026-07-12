# SwingTrader — Printing Press + Firecrawl Integration Plan

Updated for SwingTrader's actual architecture (Haiku → Sonnet → Pattern/Fundamental/Web agents → Opus → Memo → Telegram) and Bryan's feedback (skip coingecko — no crypto leg in scope).

## SwingTrader's pipeline (recap)

```
Haiku pre-screen → Sonnet deep analysis → [Pattern, Fundamental, Web, Catalyst, Macro] agents
                 → Scoring engine → Opus final → Memo (memo/generator.py) → Telegram
```

The integration points that matter for this plan:
1. **Web research agent** (`agents/web_research.py`-ish) — anywhere the agents fetch external narrative context.
2. **Catalyst / Fundamental agents** — when they need filings, transcripts, or news.
3. **Sonnet deep analysis** — where price-only signals get a narrative overlay.

## Integration 1 — Yahoo Finance as the canonical price/fundamentals source (local research)

**`yahoo-finance-pp-cli`** is the right hands-on tool for Bryan's local research. Free, no key, covers:
- Quotes, charts, options chains, fundamentals, earnings dates, FX.
- Local SQLite portfolio/watchlist with offline querying.

**For Bryan locally:**
- Watchlist briefings: `yahoo-finance-pp-cli digest --watchlist swingtrader.json` for a morning scan.
- Options skew lookups when researching new setups: `yahoo-finance-pp-cli api options-chain <SYM>`.
- Backtest data pulls: `yahoo-finance-pp-cli chart <SYM> --range 5y --interval 1d --json > data/backtest/<SYM>.json`.

**For SwingTrader's deployed bot:**
**Don't add this CLI to the Docker image.** Use the `yfinance` Python library directly (already in requirements presumably) — it hits the same endpoints, no Go runtime needed, no extra binary in the container.

The CLI is for *Bryan's research workflow*. The library is for *production*. Keep them separate.

## Integration 2 — Firecrawl in the Web Research Agent

**Where:** `agents/web_research.py` (the agent that pulls narrative context for a candidate ticker).

**Today (assumed):** the web agent does some flavor of HTTP GET + parsing, or relies on whatever LLM tool-use provides. Likely brittle on JS-rendered pages and paywalled outlets.

**With Firecrawl:** swap the fetch layer to:
```python
from firecrawl import FirecrawlApp
fc = FirecrawlApp(api_key=os.environ['FIRECRAWL_API_KEY'])

# Searching for narrative on a ticker
results = fc.search(
    f"{ticker} {company_name} earnings call transcript",
    limit=5
)

# Scraping a specific URL
page = fc.scrape(url, formats=['markdown'])
```

**Targets the agent should be able to fetch:**
- Earnings call transcripts (seekingalpha, motley fool, the company's IR page).
- Recent SEC filings (EDGAR direct works, but `firecrawl scrape` handles oddly-formatted ones).
- Analyst note snippets (when they appear in news flow).
- Conference call summaries.

**Why it's smart:**
- The Web agent is currently the weakest leg — its output quality is bounded by the fetcher. Upgrading the fetcher upgrades every downstream agent that consumes its output.
- Firecrawl handles JS-heavy pages (most modern news sites) without a headless browser in your container.
- One env var (`FIRECRAWL_API_KEY` already in `~/.env` on Bryan's machine; needs to be added to Railway via `railway variables set FIRECRAWL_API_KEY=fc-...`).

**Cost guardrails:**
- Cap firecrawl calls per ticker per scan. ~5 calls per ticker × ~10 tickers per scan × 1 scan/day = 50 calls/day. Well within free tier.
- If a scan needs more, that's a signal the candidate is borderline — fall back to "no narrative context" and let Sonnet score on price/fundamentals alone.

## Integration 3 — Firecrawl in the Catalyst Agent (paywall fallback)

**Where:** `agents/catalyst.py` or wherever earnings/event detection lives.

**Use case:** when a catalyst is a paywalled article (FT scoop, Bloomberg news, WSJ analysis), the agent today probably can't read it. Pair `firecrawl scrape` with `archive-is-pp-cli` as a fallback chain:

```python
# Fallback chain for paywalled catalysts
text = firecrawl_scrape(url) or archive_is_lookup(url) or skip_with_reason("paywalled")
```

If both fail, the catalyst is logged with `narrative_quality: skipped_paywall` and Sonnet is told. Sonnet can decide whether to weight price/fundamentals more heavily on that candidate.

## Integration 4 — Optional: Wikipedia for entity grounding in Sonnet stage

**Skip in v1.** Sonnet is already good at entity disambiguation for tickers (which Apple, which Disney). Add only if eval shows ticker confusion errors at a measurable rate.

If invoked, query `wikipedia-pp-cli summary --title "<company> <ticker>"` and include the first 200 chars in the Sonnet prompt as grounding.

## What we are NOT doing (and why)

- **`coingecko-pp-cli`** — no crypto leg in SwingTrader's scope. Removed.
- **`kalshi-pp-cli`** — initially considered as macro overlay; deferred. Hard to validate signal without a multi-month backtest first; too expensive for v1.
- **Adding Go binaries to the Railway image** — production uses Python SDKs. CLIs are for Bryan's local research only.

## Implementation order

1. **Firecrawl in `agents/web_research.py`** — the highest-leverage move on the entire SwingTrader roadmap. One agent change, every downstream agent benefits.
2. **`firecrawl-py` to `requirements.txt`** + `FIRECRAWL_API_KEY` to Railway env vars.
3. **Catalyst paywall fallback chain** with archive.is as a second fallback. Use `requests` + the archive.is REST endpoint directly (no need to bake the CLI into the container).
4. **Smoke test** — pick 3 recent SwingTrader candidates, re-run the web agent with firecrawl on, compare narrative quality side-by-side.
5. **Local research workflow upgrade** — start using `yahoo-finance-pp-cli` locally for daily watchlist briefings.

## Cost math (deployment)

- Firecrawl: ~50 calls/day × $0.001-0.005 = $0.05-0.25/day = $1.50-7.50/month. Free tier likely covers most of this.
- archive.is: free, just be polite (≥5s between calls).
- yfinance: free.

## Files to touch

- `agents/web_research.py` — replace fetch layer with firecrawl client.
- `agents/catalyst.py` — add paywall fallback chain.
- `requirements.txt` — add `firecrawl-py`.
- `.env.example` — document `FIRECRAWL_API_KEY`.
- Railway: `railway variables set FIRECRAWL_API_KEY=fc-...`
- `tests/agents/test_web_research.py` — parity tests on a frozen set of URLs.

## Open-source readiness note

SwingTrader's release goal is open-source. Two implications:
- Document `FIRECRAWL_API_KEY` as required in README, with a "free tier covers typical use" note.
- Make the agents gracefully degrade when the key isn't set — fall back to plain `requests` + log a warning. Contributors without a Firecrawl account should still be able to run SwingTrader.
