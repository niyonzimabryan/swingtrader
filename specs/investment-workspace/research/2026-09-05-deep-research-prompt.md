# Deep-research prompt — second pass (for ChatGPT Deep Research)

**Purpose:** the first research pass (`2026-09-05-best-in-class-research.md`) ran behind
a proxy that blocked most vendor domains, so many of its claims are search-extract tier.
This prompt asks a browsing-capable agent to verify those claims against primary pages,
fill the gaps the first pass named, and stress-test the plan. Paste everything below the
rule into ChatGPT Deep Research as one message. Findings land in README §5 and §8.

---

I am a solo technical founder building a personal investment-research workspace on top of an existing Python swing-trading codebase. I need best-possible research before building. Be rigorous: verify claims against primary sources (vendor docs and pricing pages, GitHub repos, SEC pages, papers), quote the exact sentence you relied on, give the URL, and state the date you accessed it. Where you cannot verify something, say "unverified" rather than guessing. Where a source contradicts a claim I list below, say "CONTRADICTED" and show both. I would rather have a visible "not sure" than confident wrongness. Today is 2026-09-05; prefer material from 2025–2026 and flag anything older.

## What the system is (context, not a question)

A cloud-hosted workspace (FastAPI + Postgres on Railway) that any agent session can attach to over remote MCP (Claude Code, Codex, a claude.ai session). It holds: a portfolio ledger synced from Robinhood; a research workspace (dossiers, theses with falsifiable invalidators, a decision journal with Brier-scored probabilities); a comparable-setups engine that answers "given this setup, how have setups genuinely like it performed" with point-in-time integrity, benchmark subtraction, block-bootstrap uncertainty, regime stratification, null tests, and an explicit `insufficient` refusal below a sample floor; and evidence planes for SEC filings (13F, 13D/G, Form 4, 8-K), vintage-correct macro (FRED/ALFRED), and timestamped news. Fixed constraints I will not revisit: agents never place orders (a human approves, code executes); no LLM output is ever a statistic; every fact carries a `known_at_utc`; scheduled jobs are deterministic Python; the repo is public. US equities only, daily bars, 1–20 session horizons, long-only, one person.

Decisions already made from the first pass: keep our own exit simulator rather than adopt a backtest engine; `arch` + `statsmodels` for inference; `edgartools` + SEC bulk data sets + OpenFIGI for filings; `fredapi` wrapped for ALFRED; Alpaca News API as the primary timestamped news source; official `mcp` Python SDK over `fastmcp`; free-tier data first with Sharadar as the named upgrade; Robinhood is the broker and live orders will go through Robinhood's official agentic MCP into a dedicated Agentic account; Schwab and IBKR deferred.

## Part 1 — Verify these specific claims (each needs URL + quoted text + verified / contradicted / unverified)

**Robinhood official agentic MCP** (highest priority — a product decision rests on it):
1. It exists at `agent.robinhood.com/mcp/trading` (or wherever it actually is), authenticates via OAuth, and became generally available in 2026. Give the official docs URL and the exact tool list.
2. Order placement is restricted to a separately funded "Agentic" account and all other accounts are read-only to agents. Quote Robinhood's own wording. How is the Agentic account funded and is there a cap?
3. Which order types are supported (market, limit, stop, stop-limit, trailing stop), and — critically — whether a protective stop can be **attached to an entry** (bracket / OCO / OTO) or only placed as a separate order after the fill. Does a standalone stop persist at the broker indefinitely (GTC)?
4. Does the surface expose tax lots, realized P&L, options positions, dividends, and account history? Are option positions readable even if not tradeable?
5. Rate limits, token lifetime, refresh behaviour, and whether an unattended server can keep a session alive without a human re-login.
6. Are there unofficial "Robinhood MCP" servers that log in with a username/password or browser session (e.g. community repos), and what tool names do they expose (I want to tell which one a prior integration used — look for tools named like `get_sec_filing_facts`, `get_equity_news`, equity tax-lot tools)?

**Data vendors** (verify on the vendor's own pricing and docs pages):
7. Sharadar via Nasdaq Data Link: current price for the Core US Equities bundle (SEP + SF1 + ACTIONS + TICKERS + SP500) for an individual; whether SEP retains delisted tickers with full history; whether TICKERS carries a delisting date and a delisting *reason*; whether SF1 `datekey` is the filing date; redistribution / derived-works terms; whether bulk flat-file download is included.
8. Polygon (now "Massive"?): confirm or refute "does not provide dividend-adjusted prices"; history depth per plan tier and current prices; whether delisted tickers remain queryable; redistribution terms.
9. Tiingo: does the EOD endpoint retain delisted tickers; per-row `splitFactor` / `divCash` fields; free-tier limits; Power plan price; redistribution and derived-works clause.
10. EODHD: delisted coverage, `filing_date` availability on fundamentals, index constituent history start date, current prices.
11. Alpaca News API: it is Benzinga-sourced, history goes back to 2015, it is free on the basic plan, rate limit — and **what the terms say about storing articles and redistributing derived data**.
12. FNSPID dataset (HuggingFace `Zihan1004/FNSPID`, arXiv 2402.06698): its licence, and whether derived aggregates may be committed to a public repo.
13. SEC XBRL `companyfacts` API: confirm each fact carries `accn`, `filed`, `form`, `fy`, `fp`, `frame`; confirm `submissions` JSON carries `acceptanceDateTime` per filing; confirm rate limit (10 req/s) and User-Agent requirement; the earliest year of reliable coverage for large filers vs smaller reporting companies.
14. `edgartools`: current version and release date, licence, whether Form 4 parsing exposes transaction codes and the 10b5-1 checkbox, whether it handles 13F-HR/A amendments, and its rate-limit handling.
15. OpenFIGI: confirms CUSIP is accepted as an input identifier, the request limits with and without an API key, and the terms for storing the returned mapping.
16. `mlfinlab` licence status in 2026 (confirm it is not OSI open source) and the maintenance state of `purgedcv` and `ml4t-diagnostic`.
17. `arch`: confirm `arch.bootstrap.optimal_block_length` exists and implements Politis & White (2004) with the Patton–Politis–White (2009) correction; confirm StepM / SPA / MCS are present.
18. Official `mcp` Python SDK: current stable version, whether v2 renamed `FastMCP` to `MCPServer`, the recommended pin for staying on 1.x, and streamable-HTTP status vs SSE deprecation. `fastmcp` (PrefectHQ): current major version and release cadence in 2026.
19. How claude.ai custom connectors, Claude Code CLI, and Codex CLI each authenticate to a remote MCP server today: static bearer header supported or OAuth required? Cite the official docs for each.
20. Schwab Trader API: refresh-token lifetime (the claim is 7 days with mandatory interactive re-auth) and whether anything changed in 2026. IBKR: whether OAuth 2.0 direct connection is institutional-only for retail accounts.
21. Railway: current Hobby/Pro pricing, Postgres pricing, and whether Railway publishes an MCP-server deployment guide recommending Redis for stream resumability.

## Part 2 — Fill these gaps (the first pass could not answer them)

22. **Point-in-time universe.** What is the best available source for historical index membership (S&P 500 / 400 / 600, Russell 1000/2000/3000) with exact change dates, for an individual? Evaluate `fja05680/sp500` (GitHub, MIT), `hanshof/sp500_constituents`, EODHD, Sharadar SP500, Norgate, and anything else. Is there any source for historical GICS sector *history* (not current sector) short of institutional S&P/MSCI licences?
23. **Point-in-time analyst estimates.** For an earnings-surprise setup the consensus estimate *as it stood before the print* is the fact that matters. Which sources (FMP, EODHD, Benzinga, Zacks, Refinitiv/LSEG retail products, Wall Street Horizon, Estimize archives, Alpha Vantage) give an as-of estimate history rather than a restated one? Which give earnings *announcement timestamps* (pre-market vs post-close) reliably back to ~2010?
24. **Delisting returns.** Beyond Shumway (1997) and Shumway & Warther (1999), is there more recent work on appropriate terminal returns for performance-related delistings (NYSE vs Nasdaq), and how do CRSP-alternative vendors (Sharadar, EODHD) represent the final price of a delisted name?
25. **Post-earnings-announcement drift in 2020–2026.** Summarize the most recent peer-reviewed or SSRN evidence on whether PEAD persists, its magnitude at 5/10/20-day horizons, and how the effect varies by market cap and by surprise definition (SUE vs text-based). I need this to set expectations for what a comparable-setups engine will find.
26. **Event-study inference with daily data at short horizons.** For a panel of clustered events (earnings season), what is current best practice for standard errors and confidence intervals: two-way clustering, calendar-time portfolio regression, stationary block bootstrap, or the `crseEventStudy` cross-sectionally-robust approach? Cite methodological papers from 2015–2026 if any, and any Python implementations that are maintained.
27. **Empirical-Bayes shrinkage for small cohorts.** For shrinking a small cohort's mean return toward a family pooled mean with weight `n/(n+k)`, what is the principled way to choose `k` (method of moments on the family variance? fixed?), and is there a reference implementation?
28. **Deterministic regime classifiers** used by practitioners (trend + realized vol + VIX + yield-curve rules): any published, reproducible rule sets with thresholds, and any evidence comparing them with HMM or statistical jump models on out-of-sample stability?
29. **Wash-sale awareness.** The exact rule (30 days before and after, substantially identical securities, across accounts including IRAs) as stated by the IRS, so a purely informational flag can be implemented correctly. No tax advice needed — the rule text and its source.
30. **Position sizing for a discretionary swing trader with uncertain edge.** Best references on risk-per-trade sizing (fixed fractional, volatility targeting à la Carver) versus Kelly variants, specifically the argument for or against Kelly when the edge estimate itself has a wide confidence interval.
31. **Prompt-injection defences for tool outputs in 2026.** The strongest current evidence on delimiter/nonce wrapping, spotlighting, and CaMeL-style designs, and any measured defence rates; plus the Claude Code and Codex documentation on how subagents can be restricted by tool allowlists.
32. **Personal research-workspace precedents.** Find any public examples (blog posts, repos, talks, 2024–2026) of an individual or small team building an agent-attached investment research system with a thesis tracker, decision journal, and base-rate engine. What did they learn, and what did they abandon?

## Part 3 — Attack the plan

33. Given everything above, what are the ten most likely ways this plan fails or produces confidently wrong numbers that the design does not already guard against? Be specific and cite evidence.
34. What would an experienced quant researcher say is over-engineered here for one person with 1–20 day horizons, and what would they say is missing?
35. Give a total monthly cost for the recommended data stack with verified prices, and the cheapest stack that still yields delisting-complete daily prices.

## Deliverable format

A report with: (1) a verification table for Part 1 — one row per numbered claim: verdict, quoted text, URL, access date; (2) answers to Part 2 in numbered order with sources; (3) Part 3 as a ranked list; (4) a final one-page "what to change before building" summary. Mark every claim you could not verify from a primary source. Do not pad; I am technical and will read the sources.
