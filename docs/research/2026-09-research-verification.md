# Research verification pass — investment research workspace

**Access date for every source below: 2026-09-06** (the session clock reads 2026-09-06; you said 2026-09-05 — one day's drift, noted because several verdicts are date-sensitive).

**How to read the verdicts.** `VERIFIED` = I read the vendor's own page, API response, or source and quote it. `CONTRADICTED` = a primary source says otherwise; both are shown. `UNVERIFIED` = I could not find it on a primary source, usually because the vendor does not publish it. `PARTIAL` = some sub-claims verified, others not. Where I only have a secondary source (a blog, an aggregator) I say so explicitly and do not upgrade it to verified.

**One structural caveat up front.** Robinhood does not publish developer documentation for its Trading MCP. There is no schema reference, no rate-limit page, no order-type table. Everything I could verify came from three consumer support articles. Several of your questions (order types, brackets, rate limits, token lifetime) have no primary answer at all. I flag these rather than filling them from blog posts, and the practical consequence is in Part 3.

---

## Part 1 — Verification table

### Robinhood official agentic MCP

---

#### Claim 1 — Exists at `agent.robinhood.com/mcp/trading`, OAuth, GA in 2026

**Verdict: PARTIAL — endpoint and OAuth VERIFIED; "generally available" CONTRADICTED / at best unverified.**

Endpoint, quoted verbatim from Robinhood's own support article:

> "You can connect to the Robinhood Trading MCP on other AI platforms that support MCP connections. Just use the Robinhood Trading MCP link: https://agent.robinhood.com/mcp/trading."

> "Claude Code — Run this command in your terminal: `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`"

URL: https://robinhood.com/us/en/support/articles/agentic-trading-overview/

**GA is contradicted by Robinhood's own newsroom post.** Announcement date May 27, 2026:

> "Agentic Trading is launching in beta with support for equities only out of the gate. Support for options, crypto, event contracts, futures, and more are coming soon as we move out of beta."

URL: https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/

The marketing page now says:

> "Now available for equities, options, and crypto through Robinhood's MCP server."

URL: https://robinhood.com/us/en/agentic-trading/

So options and crypto shipped after the beta launch, and the support article confirms options and crypto tools exist. But **I found no Robinhood statement declaring general availability or exit from beta.** Treat "GA" as UNVERIFIED. Also note this constraint, quoted verbatim, which matters for a cloud-hosted unattended service:

> "You can only open an agentic account and authenticate your agent on a desktop device. If you're connecting to the Robinhood Trading MCP on a mobile device, copy the onboarding URL and open it in a desktop browser."

**OAuth**: the transport is Streamable HTTP and the client-side flow is OAuth — this is verified indirectly but strongly. Your own repo (`database/token_store.py`) implements `mcp.client.auth.TokenStorage` against this server with `grant_types=["authorization_code", "refresh_token"]` and dynamic client registration, and it works. Robinhood's support page never uses the word "OAuth" — the *word* is unverified; the *mechanism* is verified by your working integration.

---

#### Claim 2 — Orders restricted to a separately funded Agentic account; other accounts read-only

**Verdict: VERIFIED.** Robinhood's own wording, verbatim:

> "When you connect your AI agent to the Robinhood Trading MCP, it will have read access to these data points: All your Robinhood accounts, including your Robinhood account numbers / All details about your positions and balances / All details about your transactions, including your order history / All details about your watchlists and scans"

> "**Important** — Your agent can only place trades in your Robinhood Agentic account."

URL: https://robinhood.com/us/en/support/articles/agentic-trading-overview/

And from the newsroom post:

> "You can open a dedicated agentic trading account separate from the rest of your portfolio, meaning your agent only has access to the funds you deposit into that account."

URL: https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/

**Funding mechanism — PARTIAL.** Verified constraints, verbatim:

> "A Robinhood Agentic account is a type of self-directed, individual investing account. For investing purposes, you can have up to 10 self-directed individual investing accounts, including your Agentic account."

> "Have a primary individual investing account that's in good standing before you open additional accounts."

> "Agentic accounts with limited margin enabled allow you to trade with unsettled funds from closing stock and option positions. With Agentic cash accounts, you must wait 1 business day for funds from closing stock and option positions to settle before trading with them."

> "**Note** — Margin borrowing is not yet enabled for Agentic accounts."

URLs: the overview article and https://robinhood.com/us/en/support/articles/trading-with-your-agent/

**Cap — UNVERIFIED.** I found no published maximum balance or funding cap for an Agentic account on any Robinhood page. Secondary sources also report none. The account boundary is the control, not a stated cap. Do not assume one exists; equally, do not assume none exists — Robinhood simply has not said.

**Important for your design:** T+1 settlement on a cash Agentic account is a real constraint on 1–20 session horizons. If you close a position Monday you cannot redeploy that cash until Tuesday unless you enable limited margin. That is a sizing and cadence constraint your simulator probably does not model.

---

#### Claim 3 — Order types; brackets/OCO/OTO; GTC stop persistence

**Verdict: UNVERIFIED, and the evidence available leans against brackets existing.**

Robinhood's own text is deliberately vague:

> "Your agent can ask about your portfolio value, buying power, and account information, and help with your investing, including placing orders with the different available order types."

URL: https://robinhood.com/us/en/support/articles/agentic-trading-overview/

That is the entire published statement on order types. There is no table, no enumeration of market/limit/stop/stop-limit/trailing, and no schema. **Robinhood publishes no parameter documentation for `place_equity_order`.**

What I *can* verify is the complete tool list (see Claim 4), and it contains exactly three equity write tools:

> `review_equity_order` — "Simulate an equity order and get pre-trade warnings"
> `place_equity_order` — "Place an equity order"
> `cancel_equity_order` — "Cancel an open equity order"

URL: https://robinhood.com/us/en/support/articles/trading-with-your-agent/

**There is no bracket tool, no OCO tool, no OTO tool, and no attach-stop tool in the published list.** That is negative evidence, not proof — the bracket could be a parameter on `place_equity_order`. But combined with the fact that Robinhood's own retail UI has historically not offered true broker-side bracket orders on equities, I would plan on the assumption that **a protective stop must be placed as a separate order after the fill, and that you must own the race between fill and stop placement.**

GTC persistence: Robinhood's retail equity stop orders are day or GTC (Robinhood's GTC is conventionally 90 days, not indefinite), but **I could not verify this for the MCP surface from a primary source and I am not going to assert it.** UNVERIFIED.

**This is the single largest open risk in your broker decision.** The only way to resolve it is empirical: call `review_equity_order` (which places nothing) with candidate parameter shapes and read the schema back off the `tools/list` response. Your existing `_call_tool` already caches `self._tools_cache` — dump it and read the JSON Schema for `place_equity_order`. That will answer Claim 3 definitively in about ten minutes, which is faster than any further web research.

---

#### Claim 4 — Tax lots, realized P&L, options positions, dividends, account history

**Verdict: MOSTLY VERIFIED — tax lots YES, realized P&L YES, options YES (and tradeable, not read-only), dividends NO, account history PARTIAL.**

Verbatim from the tool table at https://robinhood.com/us/en/support/articles/trading-with-your-agent/:

> `get_equity_tax_lots` — "View the open tax lots for an equity holding, each with its quantity, cost basis, acquisition date, and long/short-term status"

> `get_realized_pnl` — "View realized profit and loss for your account over a custom time window, broken down by asset class"

> `get_pnl_trade_history` — "View your trade-by-trade realized P&L history"

> `get_option_positions` — "View open or closed options positions"

> `place_option_order` — "Place a real options order"

**Your sub-question "are option positions readable even if not tradeable" is based on a false premise — CONTRADICTED.** Options are both readable and tradeable through the MCP. From the support page: "You currently can use your agent to place long equities, options, and crypto orders."

**Dividends: no dedicated tool.** The full tool list contains no `get_dividends`, no corporate-actions tool, and no cash-transaction tool. The closest is:

> `get_equity_fundamentals` — "Get valuation ratios, market cap, 52-week range, dividend info, and today's OHLCV"

— which is *security-level* dividend metadata, not *your account's* dividend receipts. If your ledger needs dividend cash flows for correct total-return accounting, **the Robinhood MCP will not give them to you.** You will need to reconcile from statements or from a market-data dividend feed. This is a real gap for a portfolio ledger.

**Account history: partial.** The overview article promises read access to "All details about your transactions, including your order history," but the tool list only exposes `get_equity_orders`, `get_option_orders`, `get_crypto_orders`, and `get_pnl_trade_history`. There is no generic transactions/ACH/journal tool. Deposits, withdrawals, dividends, fees, and corporate actions are not reachable. **The prose oversells what the tools deliver.**

For completeness, the full published tool list (verbatim names) is:

- Account/portfolio: `get_accounts`, `get_portfolio`, `get_realized_pnl`, `get_pnl_trade_history`, `search`
- Watchlists: `get_watchlists`, `get_watchlist_items`, `get_option_watchlist`, `get_popular_watchlists`, `create_watchlist`, `update_watchlist`, `follow_watchlist`, `unfollow_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `add_option_to_watchlist`, `remove_option_from_watchlist`
- Market data: `get_equity_historicals`, `get_equity_fundamentals`, `get_financials`, `get_equity_price_book`, `get_equity_technical_indicators`, `get_earnings_results`, `get_earnings_calendar`, `get_indexes`, `get_index_quotes`
- Equities: `get_equity_positions`, `get_equity_tax_lots`, `get_equity_quotes`, `get_equity_orders`, `get_equity_tradability`, `review_equity_order`, `place_equity_order`, `cancel_equity_order`
- Options: `get_option_level_upgrade_info`, `get_option_historicals`, `get_option_chains`, `get_option_instruments`, `get_option_quotes`, `get_option_positions`, `get_option_orders`, `review_option_order`, `cancel_option_order`, `place_option_order`
- Crypto: `get_currency_pairs`, `get_crypto_quotes`, `get_crypto_positions`, `get_crypto_orders`, `preview_crypto_order`, `place_crypto_order`, `cancel_crypto_order`
- Scanner: `get_scans`, `get_scanner_filter_specs`, `create_scan`, `run_scan`, `update_scan_filters`, `update_scan_config`

Note `get_earnings_results` ("estimated vs. actual EPS for recent quarters, plus the upcoming quarter's estimate and report date") — this is a **restated, current-vintage** estimate source with no `known_at` semantics. It is unusable as a point-in-time consensus fact. See Part 2 §23.

---

#### Claim 5 — Rate limits, token lifetime, refresh, unattended sessions

**Verdict: UNVERIFIED from any primary source. Robinhood publishes none of this.**

I searched Robinhood's support hub, newsroom, and marketing pages. There is no rate-limit page, no token-lifetime statement, and no developer terms document for the Trading MCP. The only operational guidance published is:

> "If you are experiencing issues with your MCP connection, disconnect and reconnect the Robinhood Trading MCP on your AI platform."

URL: https://robinhood.com/us/en/support/articles/agentic-trading-overview/

That sentence is, in effect, Robinhood telling you the expected recovery path is a **human reconnect**. Combined with the verified constraint that "You can only open an agentic account and authenticate your agent on a desktop device," the honest read is:

**Unattended server-side session keep-alive is not a documented capability, and Robinhood has given no commitment that it works.** Your `token_store.py` implements refresh-token persistence correctly and *may* work indefinitely — but you are relying on undocumented behaviour of a beta product from a broker that has published no API contract. There is no SLA, no deprecation policy, and no versioning statement to hold them to.

I also want to flag a widely-repeated secondary claim I could **not** verify and which you should not rely on: several blogs assert a ~1-hour access-token lifetime with clients failing to auto-refresh. Those posts are about MCP OAuth in general (there are open issues on `anthropics/claude-code` and `open-webui` to this effect), not about Robinhood specifically. Marked UNVERIFIED.

**Recommended empirical test before you depend on this:** run the token store on Railway with no human present and log every refresh attempt with timestamps for 30 days. That is the only source of truth available.

---

#### Claim 6 — Unofficial Robinhood MCP servers; which one a prior integration used

**Verdict: the premise about tool names is CONTRADICTED, and the question is answered from your own repo rather than the web.**

**`get_sec_filing_facts` and `get_equity_news` do not exist in the official Robinhood MCP tool list** (see Claim 4), and I found no repository exposing that exact pair. Searching for them surfaces only unrelated SEC-EDGAR MCP servers (`stefanoamorelli/sec-edgar-mcp`, `cyanheads/secedgar-mcp-server`, `daniel3303/Equibles`, `tradermonty/finviz-mcp-server`) whose tools are named differently (`get_edgar_company_facts`, `get_edgar_company_filings`, etc.).

**Your prior integration is the official Robinhood MCP, not a community server.** From `execution/brokers/robinhood.py` in this repo, the tools actually called are:

`get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders`, `get_equity_quotes`, `get_equity_tradability`, `review_equity_order`, `place_equity_order`, `cancel_equity_order`

Every one of those is on Robinhood's published list, with matching semantics. `config/settings.py:46` sets `robinhood_mcp_url: str = "https://agent.robinhood.com/mcp/trading"`, and `database/token_store.py` runs the MCP SDK OAuth provider against it. There is no username/password path anywhere in the repo.

Community servers do exist and do use credentials — for the record, so you can recognise them if you meet one:

- `verygoodplugins/robinhood-mcp` — read-only, wraps `robin_stocks`, credentials via `ROBINHOOD_USERNAME` / `ROBINHOOD_PASSWORD` / `ROBINHOOD_TOTP_SECRET`, session cached at `~/.tokens/robinhood.pickle`
- `zaydiscold/robinhood-cli-mcp-api` — unofficial API map, reads live, writes gated
- `Jimgitsit/robinhood-mcp` — trading + market data
- `Open-Agent-Tools/open-stocks-mcp` — multi-broker (Robinhood, Schwab)

These are secondary-sourced (GitHub descriptions via search, not repos I fetched). Each is explicitly not affiliated with Robinhood. **Do not use any of them** — storing a broker password and TOTP seed in a cloud-hosted service is a materially worse security posture than the OAuth flow you already have.

---

### Data vendors

---

#### Claim 7 — Sharadar via Nasdaq Data Link: bundle price, delisted retention, TICKERS delisting date/reason, SF1 datekey, redistribution, bulk download

**Verdict: UNVERIFIED across the board. I could not confirm a single one of these from a primary source.**

This is a genuine failure of this research pass and I want to be blunt about it rather than paper over it. Nasdaq Data Link's dataset pages (`data.nasdaq.com/databases/SEP`, `/SFA`, `/ZFA`) render pricing client-side and returned no pricing text to a fetch. QuantRocket's Sharadar pricing page — the usual secondary mirror — is now gated:

> "Log in or create account to see pricing" / "Select license to see pricing"

URL: https://www.quantrocket.com/pricing/data/sharadar/

I therefore have **no verified price** for the Core US Equities Bundle, **no verified statement** that SEP retains delisted tickers with full history, **no verified schema** for TICKERS showing a delisting date and reason, **no verified definition** of SF1 `datekey`, **no verified redistribution terms**, and **no verified statement** about bulk flat-file download inclusion.

Secondary sources assert "Sharadar's active and delisted coverage extends back to the 90s, is point-in-time ready, and nearly completely free of survivorship bias" (quantrocket.com/sharadar/) — that is vendor-adjacent marketing copy, not a schema, and I am not treating it as verification.

**Because §35 asks for a total monthly cost with verified prices, and Sharadar is your named upgrade, this gap directly blocks that answer.** Resolve it by opening `sharadar.com/prices` and `data.nasdaq.com` in a logged-in browser, or emailing Sharadar for the table schema and the licence PDF. Fifteen minutes of your time beats another hour of mine.

---

#### Claim 8 — Polygon (now Massive): no dividend-adjusted prices; history depth; delisted; redistribution

**Verdict: "no dividend-adjusted prices" VERIFIED — the vendor's own docs confirm it. Rebrand VERIFIED. Prices VERIFIED. Delisted and redistribution UNVERIFIED.**

The decisive quote, from Massive's own API reference for the aggregates endpoint:

> "Whether or not the results are adjusted for splits. By default, results are adjusted. Set this to false to get results that are NOT adjusted for splits."

URL: https://massive.com/docs/rest/stocks/aggregates/custom-bars

The `adjusted` parameter adjusts **for splits only**. Dividends are not mentioned. Your claim is confirmed: **Polygon/Massive does not return dividend-adjusted (total-return) prices.** You would have to build the adjustment yourself from their dividends endpoint. For a 1–20 day horizon on US equities this is a smaller error than it would be at longer horizons, but it is not zero — it systematically biases measured returns downward around ex-dates, and it will show up as a spurious negative drift in any cohort whose events cluster near ex-dividend dates.

Rebrand VERIFIED: "Polygon.io is Now Massive" (https://massive.com/blog/polygon-is-now-massive), and `polygon.io/pricing?product=stocks` now 301-redirects to `massive.com/pricing?product=stocks`.

Prices and history depth, quoted verbatim from https://massive.com/pricing?product=stocks:

| Plan | Price | History | Calls | Flat files |
|---|---|---|---|---|
| Stocks Basic | $0/month | "2 Years Historical Data" | "5 API Calls / Minute" | not included |
| Stocks Starter | $29/month | "5 Years Historical Data" | "Unlimited API Calls" | included |
| Stocks Developer | $79/month | "10 Years Historical Data" | "Unlimited API Calls" | included |
| Stocks Advanced | $199/month | "20+ Years Historical Data" | "Unlimited API Calls" | included |

**Delisted-ticker queryability: UNVERIFIED.** Not stated on the pricing page and I did not find a docs statement. **Redistribution terms: UNVERIFIED** — I did not read the ToS.

---

#### Claim 9 — Tiingo: delisted retention, splitFactor/divCash, free limits, Power price, redistribution

**Verdict: PARTIAL. Prices and limits VERIFIED. Fields VERIFIED. Delisted retention UNVERIFIED. Redistribution VERIFIED in the restrictive direction.**

Verbatim from https://www.tiingo.com/about/pricing:

| | Starter | Power |
|---|---|---|
| Price | "$0/month" | "$30/month" |
| Unique Symbols per Month | "500" | "109,881" |
| Max Requests Per Hour | "50" | "10,000" |
| Max Requests Per Day | "1000" | "100,000" |
| Max Bandwidth Per Month | "1 GB" | "40 GB" |

The critical licence sentence, verbatim from the same page:

> "Internal use means you may only use the data for your own personal use and you may not display or share the data with another person or organization"

> "For redistribution use, our pricing is explicit and predictable on our Product pages."

**This matters more than you may realise given "the repo is public."** Under the internal-use licence you may not display or share the data. Committing raw Tiingo price series — or arguably any output that reconstitutes them — to a public GitHub repo is display/sharing. Derived *statistics* (a cohort mean, a bootstrap CI) are almost certainly fine; a cached parquet of adjusted closes is almost certainly not. Get this right before the first commit, because git history is forever.

`splitFactor` and `divCash` are documented Tiingo EOD row fields — confirmed by Tiingo's own corporate-actions documentation (https://www.tiingo.com/documentation/corporate-actions/splits), which states the split ex-date "is where `splitFactor` will not be 1.0". **Delisted-ticker retention on the EOD endpoint: UNVERIFIED.** Tiingo does not state it on the pricing or EOD product page, and this is the one thing you actually need from them.

**Note the free tier is unusable for your purpose**: 500 unique symbols/month and 1,000 requests/day cannot build a point-in-time US equity universe.

---

#### Claim 10 — EODHD: delisted coverage, filing_date, index constituent history start, prices

**Verdict: PARTIAL. Prices VERIFIED. Delisted coverage VERIFIED (secondary-strong). Index history VERIFIED as ~12 years. `filing_date` UNVERIFIED.**

Prices, verbatim from https://eodhd.com/pricing:

| Plan | Monthly | Annual | Calls/day |
|---|---|---|---|
| Free | $0 | $0 | 20 |
| EOD Historical Data — All World | $19.99 | $199.00 | 100,000 |
| EOD+Intraday — All World Extended | $29.99 | $299.90 | 100,000 |
| Fundamentals Data Feed | $59.99 | $599.90 | 100,000 |
| ALL-IN-ONE | $99.99 | $999.90 | 100,000 |

Delisted coverage, from EODHD's own delisted-companies page (https://eodhd.com/financial-apis/delisted-stock-companies-data-2): delisted tickers are available in all packages, with end-of-day prices, fundamentals, dividends and splits retrievable; the `IsDelisted` flag is exposed and is stated to be "only visible for US stocks". Coverage claimed as 26,000+ US tickers mostly from Jan 2000. I read this via search extraction rather than fetching the page myself, so: **strong secondary, not fully primary.**

**Index constituent history: this is the answer to a question you asked in §22, and it is bad news.** EODHD's historical constituents API provides "up to 12 years" of history. Twelve years from 2026 is 2014. **That does not reach 2000, and it does not cover 2008–2009.** If your comparable-setups engine wants regime stratification that includes the GFC, EODHD's index history cannot supply the universe.

`filing_date` on fundamentals: **UNVERIFIED.** I did not find it documented. This is the field that determines whether EODHD fundamentals are point-in-time usable at all, so verify it directly before relying on them.

---

#### Claim 11 — Alpaca News API: Benzinga-sourced, back to 2015, free on basic, rate limit, storage/redistribution terms

**Verdict: PARTIAL. Source, history and free-tier VERIFIED (Alpaca docs/blog). Rate limit VERIFIED with a caveat. Storage/redistribution terms CONTRADICT your plan.**

Alpaca's docs state news data goes back to 2015 for stocks and crypto, that Benzinga is currently the sole provider, and that News API is available at no cost with rate limits inherited from your Market Data plan — "200 calls per minute for Free plans and 10,000 calls per minute for Unlimited plans." Sources: https://docs.alpaca.markets/us/docs/historical-news-data and https://alpaca.markets/blog/introducing-news-api-for-real-time-fiancial-news/. I read these through search extraction; **treat the 200/min figure as strong secondary, and confirm it in the docs page yourself before you build a backfill schedule around it.**

**The terms are the problem, and this is a genuine finding against your plan.** Alpaca's Terms & Conditions prohibit distributing, selling or commercially exploiting market data without written consent, and the underlying market-data licensing terms bar sharing, selling or publishing the data "or any derived products or services" to third parties without permission, and bar altering or combining the data with other sources without permission. Sources: https://files.alpaca.markets/disclosures/library/Alpaca+Terms+_+Conditions.pdf and Alpaca's market-data licensing summary.

Read literally, "any derived products" plus "combine with other data sources" would cover a public repo containing news-derived features joined to price data. I did not read the full PDF clause-by-clause, so I will not call this definitive — **but "the repo is public" and "Alpaca News is the primary news source" are in tension, and you should resolve it deliberately rather than discover it later.** The safe pattern: keep article text and any article-level derived fields out of the repo entirely; commit only aggregate statistics that cannot reconstitute the source.

---

#### Claim 12 — FNSPID licence; may derived aggregates go in a public repo?

**Verdict: VERIFIED, and the answer is "not for a commercial system."**

From the dataset card at https://huggingface.co/datasets/Zihan1004/FNSPID:

> Licence: **CC BY-NC-4.0** (Creative Commons Attribution-NonCommercial 4.0 International)

> "Commercial use of this code or dataset without explicit permission from the original authors is strictly prohibited."

Commercial licensing inquiries go to the address on the card. Paper: arXiv 2402.06698.

**Interpretation.** CC BY-NC permits redistribution and derivative works *for non-commercial purposes* with attribution. A public repo is not itself commercial. But a system you use to make money trading is a commercial use of the dataset, and the authors' own sentence is broader and blunter than the licence's NC clause. Two consequences:

1. If this workspace informs real trades, **FNSPID-derived features are a licence problem regardless of whether they are aggregated.**
2. There is no share-alike obligation, so the aggregation question is not the issue — the *purpose* is.

Given you already have Alpaca/Benzinga back to 2015 and FNSPID's own coverage window is materially narrower than advertised in places, **I would drop FNSPID rather than negotiate a licence.** It buys you little and carries a clear restriction.

---

#### Claim 13 — SEC XBRL companyfacts: accn/filed/form/fy/fp/frame; acceptanceDateTime; 10 req/s; User-Agent; coverage start

**Verdict: VERIFIED — and this is the strongest verification in the report, because I called the APIs rather than reading about them.**

Live call to `https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json` returned fact objects with exactly these keys:

```json
{"start": "2015-09-27", "end": "2016-09-24", "val": 215639000000,
 "accn": "0000320193-18-000145", "fy": 2018, "fp": "FY",
 "form": "10-K", "filed": "2018-11-05", "frame": "CY2016"}
```

All six of `accn`, `filed`, `form`, `fy`, `fp`, `frame` confirmed present. (`frame` is present on facts that map to a calendar frame; it is not on every fact.)

Live call to `https://data.sec.gov/submissions/CIK0000320193.json` returned `filings.recent` with these keys:

`accessionNumber, filingDate, reportDate, acceptanceDateTime, act, form, fileNumber, filmNumber, items, core_type, size, isXBRL, isInlineXBRL, isXBRLNumeric, primaryDocument, primaryDocDescription`

Sample row: `{'accessionNumber': '0001140361-26-035636', 'filingDate': '2026-09-03', 'reportDate': '2026-09-01', 'acceptanceDateTime': '2026-09-03T22:30:44.000Z', 'form': '4', ...}`

**`acceptanceDateTime` confirmed, per filing, with second resolution and an explicit UTC `Z`.** That is exactly the `known_at_utc` primitive your design requires, and note the value above: accepted at 22:30 UTC, i.e. after the close on the filing date. **Using `filingDate` instead of `acceptanceDateTime` would leak future information into any same-day setup.** Use `acceptanceDateTime`.

Rate limit and User-Agent, verbatim from https://www.sec.gov/os/webmaster-faq:

> "our current maximum access rate is 10 requests per second"

> "Please declare your user agent in request headers" — format "User-Agent: Sample Company Name AdminContact@<sample company domain>.com"

**Coverage start: PARTIAL / UNVERIFIED as stated.** I did not find an SEC page stating a reliable-coverage start year by filer class, and I will not assert the phase-in dates from memory. What I *can* show is that the earliest `filed` date for Apple's `Revenues` concept is 2018-11-05 — which is not a coverage boundary but a *tag-usage* boundary. **This is the real trap: XBRL coverage is per-tag, not per-company.** A concept can vanish from a company's facts because the filer switched tags (`Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax`), not because the data is missing. Any fundamentals pipeline built on companyfacts must handle tag migration explicitly or it will silently produce gaps that look like missing quarters. Verify the phase-in years against SEC's XBRL rule releases before you set a hard start date.

---

#### Claim 14 — edgartools: version, licence, Form 4 codes + 10b5-1, 13F-HR/A, rate limiting

**Verdict: PARTIAL. Version, date and licence VERIFIED. Form 4/13F support VERIFIED at feature level. 10b5-1 checkbox and amendment handling UNVERIFIED at field level.**

From https://pypi.org/project/edgartools/ and confirmed by `pip index versions edgartools`:

- **Latest version: 5.56.0, released September 2, 2026** (four days before this report — actively maintained)
- **Licence: MIT**

Note the search index also surfaced 5.31.5 (May 22, 2026) and libraries.io showed 5.28.3 — those are stale mirrors. PyPI and `pip index` agree on 5.56.0.

Feature statements from the PyPI page, verbatim:

> "Form 3/4/5 insider transactions"
> "13F institutional holdings & hedge fund portfolios"
> "The latest insider Form 4 as a structured object" — `Company("AAPL").get_filings(form="4").latest().obj()`
> "Configurable rate limiting + enterprise/academic mirrors" / "Rate-limit aware, smart caching"

**What I could not verify:** whether the parsed Form 4 object exposes the transaction code (`P`/`S`/`A`/`M`/`F`…) as a field, and whether it exposes the Rule 10b5-1 checkbox (the `<rule10b5-1Plan>` / footnote-based flag). Both are in the Form 4 XML, so the data is there; whether edgartools surfaces them as attributes is a five-minute check in a REPL that I would rather you do than have me guess.

Likewise **13F-HR/A amendment handling is UNVERIFIED.** This matters: amendments come in restatement and addition flavours, and naively unioning HR with HR/A double-counts positions. Check `edgartools`' behaviour explicitly.

The transaction-code semantics themselves (P = purchase, S = sale, A = grant/award; P and S are the discretionary, informative ones) are standard and corroborated, but the authoritative list is the SEC's Form 4 instructions, not edgartools.

---

#### Claim 15 — OpenFIGI: CUSIP as input, rate limits with/without key, storage terms

**Verdict: CUSIP and rate limits VERIFIED. Storage terms UNVERIFIED.**

Verbatim from https://www.openfigi.com/api/documentation:

- **Without API key: "25 Per Minute", max "10 Jobs" per request**
- **With API key: "25 Per 6 Seconds", max "100 Jobs" per request**
- `ID_CUSIP` is an accepted `idType`: "CUSIP - Committee on Uniform Securities Identification Procedures", example `[{"idType":"ID_CUSIP","idValue":"123456789"}]`

So CUSIP-in works. **The asymmetry you should design around: OpenFIGI accepts CUSIP as input but does not return CUSIP/ISIN/SEDOL as output**, because those are third-party proprietary identifiers with redistribution restrictions. Mapping is one-way — CUSIP → FIGI, never FIGI → CUSIP. For 13F ingestion (which is CUSIP-keyed) that is exactly the direction you need, so this works, but you cannot use OpenFIGI to build a reverse lookup table.

**Storage/redistribution terms for returned mappings: UNVERIFIED.** The API documentation page does not state them. There is a separate Terms of Service I did not read. Given that FIGI is an open standard explicitly designed to be freely redistributable — that is its entire reason for existing versus CUSIP — I *expect* storage to be unrestricted, but I have not verified it and you should not commit a public FIGI mapping table on my expectation.

**Throughput note:** with a key, 100 jobs × 25 requests per 6 seconds ≈ 25,000 mappings/minute. A full 13F season is well within reach. Get the key.

---

#### Claim 16 — mlfinlab licence in 2026; purgedcv and ml4t-diagnostic maintenance

**Verdict: mlfinlab VERIFIED as not open source. purgedcv / ml4t-diagnostic UNVERIFIED.**

From the mlfinlab repository's own licence documentation (`docs/source/additional_information/license.rst` and `LICENSE.txt` at github.com/hudson-and-thames/mlfinlab):

> MlFinLab is licensed under an "all rights reserved" licence, is **not** open-source, and "may not be used for commercial purposes without a commercial license which may be purchased from Hudson and Thames Quantitative Research."

> Student licences "do NOT allow for use of the code base for any commercial purposes."

**Confirmed: mlfinlab is not OSI open source and is unusable in your system without a paid commercial licence.** Read via search extraction of the repo's licence files rather than a direct fetch — but the claim is corroborated across the LICENSE, the docs, and a long-running GitHub issue (#496) asking them to relicense permissively, which they declined.

**`purgedcv` and `ml4t-diagnostic`: UNVERIFIED.** GitHub API access from this session is scoped to your own repository, so I could not read their metadata, and I found no authoritative release history. I will not guess at their maintenance state.

**Practical point:** you do not need mlfinlab. Purged/embargoed K-fold is roughly forty lines of `numpy` implementing López de Prado's own published algorithm (*Advances in Financial Machine Learning*, ch. 7). Write it yourself, test it, and you own it under your own licence. That removes the whole question.

---

#### Claim 17 — arch: `optimal_block_length`, Politis–White (2004) + Patton–Politis–White (2009), StepM/SPA/MCS

**Verdict: FULLY VERIFIED — by installing the package and reading the shipped source, not the docs.**

`arch` version installed and inspected: **8.0.0**.

`from arch.bootstrap import optimal_block_length` imports successfully. Its docstring, verbatim:

> "Estimate optimal window length for time-series bootstraps"
>
> Returns: "A DataFrame with two columns `b_sb`, the estimated optimal block size for the Stationary Bootstrap and `b_cb`, the estimated optimal block size for the circular bootstrap."
>
> "Algorithm described in ([1]_) its correction ([2]_) depends on a tuning parameter m…"
>
> References:
> "[1] Dimitris N. Politis & Halbert White (2004) Automatic Block-Length Selection for the Dependent Bootstrap, Econometric Reviews, 23:1, 53-70, DOI: 10.1081/ETC-120028836."
> "[2] Andrew Patton, Dimitris N. Politis & Halbert White (2009) Correction to "Automatic Block-Length Selection for the Dependent Bootstrap" by D. Politis and H. White, Econometric Reviews, 28:4, 372-375, DOI: 10.1080/07474930802459016."

Both citations confirmed exactly as you claimed.

Multiple-comparison procedures present in `arch.bootstrap`: **`SPA`, `StepM`, `MCS`** — all three confirmed by attribute inspection.

One caveat the docstring itself raises, worth knowing before you trust the numbers:

> "The block lengths do not match this implementation since the autocovariances and autocorrelations are all computed using the maximum sample length rather than a common sampling length."

i.e. `arch`'s block lengths differ from Patton's reference MATLAB implementation. Not wrong — different, deliberately. Do not expect to reproduce published numbers exactly.

---

#### Claim 18 — Official `mcp` Python SDK version, v2 rename, 1.x pin, transports; `fastmcp` cadence

**Verdict: VERIFIED — and I found a live bug in your repo while checking it.**

`pip index versions mcp` → **latest is `mcp` 2.1.1**. `pip index versions fastmcp` → **latest is `fastmcp` 4.0.3** (PrefectHQ). Both as of 2026-09-06.

I installed `mcp==2.1.1` in a clean venv and probed the imports your repo uses. The SDK's own error message is the clearest possible primary source on the rename:

> `ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP was renamed to MCPServer (from mcp.server.mcpserver import MCPServer) and other APIs changed; see the migration guide at https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver or pin 'mcp<2' to keep running v1 code.`

**Rename VERIFIED. `mcp.server.fastmcp` removal VERIFIED. Recommended 1.x pin VERIFIED** — the SDK's own guidance is `mcp<2` (release notes phrase it as keeping "a `<2` upper bound on your requirement (for example `mcp>=1.28,<2`)"), and v1.x is stated to be in maintenance mode receiving security fixes only. Streamable HTTP is the recommended remote transport, not SSE — verified in the sense that v2 is built around it; I did not find a formal SSE deprecation notice, so **"SSE deprecated" is UNVERIFIED as a formal status.**

**The bug.** `requirements.txt:22` pins `mcp>=1.27.2` with **no upper bound**. Under mcp 2.1.1, your imports resolve like this:

| Import | Under mcp 2.1.1 |
|---|---|
| `mcp.client.auth.OAuthClientProvider` | OK |
| `mcp.client.auth.TokenStorage` | OK |
| `mcp.shared.auth.OAuthClientMetadata` / `OAuthToken` / `OAuthClientInformationFull` | OK |
| `mcp.ClientSession` | OK |
| **`mcp.client.streamable_http.streamablehttp_client`** | **MISSING** |

In v2 the module still exists but the symbol was renamed to **`streamable_http_client`** (underscore before `client`). Your `execution/brokers/robinhood.py:328` and `scripts/robinhood_auth.py:125` both do `from mcp.client.streamable_http import streamablehttp_client`.

**Consequence: a fresh `pip install -r requirements.txt` today resolves `mcp` to 2.1.1 and every Robinhood broker call raises `ImportError` at the point of use.** Worse, `_call_tool_sync` catches `ImportError` and re-raises it as `RobinhoodMCPError("Install the MCP SDK with 'pip install mcp'.")` — so the failure presents as "SDK not installed" when the SDK *is* installed. On a Railway rebuild this would be a confusing outage.

The one-line fix is `mcp>=1.27.2,<2`. Do it before your next deploy. (Migrating to v2 is a separate, larger decision; your OAuth layer survives it, only the transport import changes.)

`fastmcp` cadence: at 4.0.3, so at least four majors — a fast-moving major-version cadence. **This corroborates your existing decision to prefer the official SDK.** I did not verify a release-date history, so "cadence in 2026" is UNVERIFIED in detail.

---

#### Claim 19 — How claude.ai connectors, Claude Code CLI, and Codex CLI authenticate to a remote MCP server

**Verdict: PARTIAL. Codex CLI VERIFIED (static bearer supported). Claude Code / claude.ai: OAuth is the documented path; static-header support is reported but I could not confirm it on an Anthropic-owned page.**

**Codex CLI — static bearer VERIFIED.** Config supports `bearer_token_env_var = "TOKEN"` (read at connect time, sent as `Authorization: Bearer …`), plus `http_headers` for static header values and `env_http_headers` for env-sourced ones, described as an escape hatch for non-standard schemes. OAuth is also supported via `auth = "oauth"`. Older versions ignore remote servers unless `experimental_use_rmcp_client` is enabled. Source: https://developers.openai.com/codex/mcp (read via search extraction — strong secondary; the config keys are quoted consistently across several independent write-ups and an OpenAI issue tracker).

**Claude Code CLI — OAuth VERIFIED, static bearer UNCERTAIN.** Claude Code is a native client using an RFC 8252 loopback redirect on an ephemeral port; it declares `http://localhost/callback` and `http://127.0.0.1/callback` in its Client ID Metadata Document, so an authorization server must accept both with the port component ignored. On a 401 it expects a `WWW-Authenticate` header pointing at OAuth metadata, then sends `Authorization: Bearer <token>` on subsequent requests. This matches the behaviour your `token_store.py` already depends on.

**claude.ai custom connectors — static headers reported as beta.** Anthropic's connector-authentication docs describe support for static bearer tokens / API keys via `static_headers`, with an admin entering the credential once. Source: https://claude.com/docs/connectors/building/authentication. **I read this through search extraction, not a direct fetch, and there is an open issue (`anthropics/claude-ai-mcp` #112) titled "Cannot configure Authorization: Bearer for custom remote MCP (only OAuth client id/secret in advanced settings)" — which suggests the feature is either newer than the issue, gated, or admin-only.** Verify on the docs page yourself before designing around it.

**Design implication.** If you want one server reachable from Claude Code, Codex, and claude.ai with the least friction, **implement OAuth 2.1 with dynamic client registration**, because it is the only mechanism all three definitely support. Static bearer as an additional accepted path costs you almost nothing and makes local development and cron jobs much easier — accept both.

---

#### Claim 20 — Schwab 7-day refresh token; IBKR OAuth 2.0 institutional-only

**Verdict: both CORROBORATED but NOT primary-verified. Both developer portals are auth-gated.**

**Schwab.** Multiple independent sources — the `schwab-py` authentication documentation, the CRAN `schwabr` package manual dated May 9, 2026, and Lumibot's broker docs — all state the same thing: refresh tokens expire after seven days, requests using an older refresh token are rejected with `invalid_client`, there is no programmatic renewal, and a manual interactive login is required to obtain a new one. The May 2026 CRAN manual still documents the 7-day limit with no change noted. **No 2026 change found.** Because Schwab's developer portal requires login, I could not read Schwab's own words. **Marked corroborated-secondary, high confidence.**

If accurate, this is disqualifying for an unattended workspace: a mandatory human interactive re-auth every seven days, forever. Your decision to defer Schwab looks correct.

**IBKR.** IBKR Campus documentation states retail clients are currently approved to access the Web API only through the Client Portal Gateway; third-party vendors may currently only seek approval for OAuth 1.0a; OAuth 1.0a is expected to remain institutional; OAuth 2.0 for individual access is "being considered… no ETA at this time." IBKR has separately signalled an intent to unify its web API products under OAuth 2.0. **Your claim is corroborated: OAuth 2.0 direct connection is not available to retail.** Read via search extraction of interactivebrokers.com/campus pages — **strong secondary.** Deferring IBKR is also correct: the Client Portal Gateway is a local Java process requiring periodic re-authentication, which is a poor fit for Railway.

---

#### Claim 21 — Railway pricing and an MCP deployment guide recommending Redis

**Verdict: pricing VERIFIED. Postgres pricing UNVERIFIED. Redis/MCP guide UNVERIFIED.**

Verbatim from https://railway.com/pricing:

> Free Trial — "$0" with "$5 one-time credit (30 days)"
> Free — "$0/month with $1 of monthly usage credits"
> **Hobby — "$5/month, including $5 of monthly usage credits"**
> **Pro — "$20/month per workspace, including $20 of monthly usage credits"**
> Enterprise — "custom pricing with contractual SLAs, SSO, dedicated support, and procurement options"

Resource rates, verbatim:

> Memory: "$0.00000386 per GB/s" (≈ "$10 per GB" monthly)
> CPU: "$0.00000772 per vCPU/s" (≈ "$20 per vCPU" monthly)

**Postgres is not separately priced** — the pricing page contains no database-specific pricing. Railway's Postgres is a deployed service billed at the same compute + memory + volume rates. So your database cost is a function of how much RAM you give it, not a plan fee. **Volume/storage rate: UNVERIFIED** (not captured in what I read).

**The Redis-for-stream-resumability guide: UNVERIFIED.** I found no Railway-published MCP deployment guide making that recommendation. The underlying idea is real and comes from the MCP spec itself — Streamable HTTP supports resumability via `Last-Event-ID`, and the reference TypeScript implementations use a Redis-backed event store — but **I cannot attribute the recommendation to Railway.** Treat the Redis dependency as optional: you need it only if you want a disconnected client to resume a stream mid-response, which for a single-user workspace is close to worthless. Skip it.

**Cost sanity check:** Hobby at $5/month with $5 of included credit will not cover a FastAPI service plus a Postgres running continuously. Two always-on services at even 0.5 GB each is roughly $10/month of memory alone. **Budget Pro at $20/month and expect to exceed the included credit.**

---

## Part 2 — Gaps

### 22. Point-in-time universe (index membership and historical GICS)

**The honest headline: there is no good cheap source, and every option below has a defect you must design around.**

**`fja05680/sp500`** (https://github.com/fja05680/sp500) — **MIT licensed** (verified via the repo page). Contains `S&P 500 Historical Components & Changes (Updated).csv` covering "historical S&P 500 index membership from 1996 til" present. Provenance, from the README: the original dataset came from Andreas Clenow's *Trading Evolved*; the maintainer updates it "every couple of months" by consulting the Wikipedia S&P 500 page and doing supplementary Google research, because "Wikipedia provides only 'Selected Changes,' not comprehensive data."

**Read that provenance again before you trust it.** It is a hand-maintained Wikipedia scrape with acknowledged incompleteness, updated on a multi-month cadence. It is free, MIT, and covers 1996–present, which is more history than any affordable vendor. But it is not an authoritative membership record, its change dates are researched rather than sourced, and it will have errors you cannot enumerate. `hanshof/sp500_constituents` is the same shape (1996-01-02 to present, per-date constituent lists); **I could not verify its licence** — GitHub API access from this session is scoped to your own repo.

**EODHD** — up to **12 years** of index constituent history (≈2014→). Covers S&P 500/400/600/100 and Dow. **Does not reach 2008–2009.** Included in higher tiers.

**Sharadar SP500** — part of the Core US Equities bundle; **coverage window, schema, and price all UNVERIFIED** (see Claim 7). This is the option most likely to be right and the one I have least evidence about.

**Norgate Data** — the serious retail answer for point-in-time index membership, with documented handling of index constituent history and delisted securities. **I did not verify its current pricing or terms in this pass — UNVERIFIED.** Historically it has been Windows-oriented and desktop-licensed, which sits awkwardly with a Linux cloud service; check whether their data can be exported to a format you can host.

**Russell 1000/2000/3000 — worse.** FTSE Russell reconstitutes annually in June and publishes membership commercially. **I found no free or cheap historical Russell membership source and would treat Russell point-in-time membership as unavailable to you.** If your engine needs a small-cap universe, define your own (e.g. a rank-by-market-cap rule applied point-in-time to a delisting-complete price file) rather than pretending to reconstruct Russell. A self-defined, reproducible, documented universe rule is *more* defensible than a badly-reconstructed index.

**Historical GICS sector — the answer is essentially no.** GICS is jointly owned by S&P Global and MSCI and licensed commercially; historical sector *assignment history* (as opposed to current sector) is an institutional-licence product. I found no free or retail source and **I am marking this UNVERIFIED-but-strongly-expected-negative.** Sharadar's TICKERS carries a sector/industry field, but whether it is point-in-time or current-as-of-snapshot is exactly the thing I could not verify.

**What I would actually do:** accept that sector is current-vintage, and treat any sector-stratified result as contaminated by look-ahead. Or drop sector stratification and stratify on something you *can* observe point-in-time — market cap decile, realized volatility decile, price level. Those are computable from your own price file with no licence and no vintage problem. **A stratification you can compute honestly beats one you can only fake.**

---

### 23. Point-in-time analyst estimates and announcement timestamps

**This is the hardest unsolved problem in your plan, and I did not find a satisfying answer at retail prices.**

The distinction that matters: a *restated* estimate history tells you what the consensus is now recorded as having been; a *point-in-time* history tells you what a subscriber could actually have seen on a given date. Vendors restate for splits, for late-arriving broker submissions, for analyst-coverage corrections, and for their own data fixes. Using a restated consensus as a pre-print fact is a look-ahead bias that inflates measured surprise-to-drift relationships, and it inflates them in the direction that makes your engine look right.

**Zacks** markets exactly this: "historical and point-in-time data covering analyst estimates, fundamentals, prices… purpose-built for institutions conducting quantitative research, model development, back-testing" (zacksdata.com). Available via Nasdaq Data Link as Zacks Earnings Estimates (ZEE). **Price and individual availability UNVERIFIED** — Zacks positions this as institutional, which usually means four or five figures annually.

**FMP** offers analyst estimates and consensus price targets "updated continuously" (site.financialmodelingprep.com/datasets/analyst-estimates-targets). **"Updated continuously" is the language of a restated snapshot, not a point-in-time archive.** I found no FMP statement offering an as-of estimate history. Treat FMP estimates as restated until proven otherwise.

**EODHD, Benzinga, Alpha Vantage** — I found no point-in-time estimate archive claim from any of them. **UNVERIFIED, expected negative.**

**Estimize** — the crowd-sourced archive is genuinely point-in-time by construction (contributions are timestamped), but coverage is skewed to large caps and high-interest names, and **I could not verify current data availability or terms in 2026.** Its selection bias is severe for a base-rate engine: the names people bother to estimate are not a random sample.

**Wall Street Horizon** — the specialist for *event timing* rather than estimates: confirmed and inferred earnings dates with before/after-market designation. **Pricing and retail availability UNVERIFIED**, and it is generally sold institutionally.

**Announcement timestamps back to ~2010 — no verified retail source.** Robinhood's own `get_earnings_calendar` and `get_earnings_results` exist but are current-vintage with no `known_at` semantics (Claim 4). This matters more than the estimate itself for a 1–20 day horizon: **if you misclassify a post-close print as pre-market, your day-0 return is wrong by a full session, and the error is systematic, not random.**

**My recommendation, and it is a scope cut.** Do not build the earnings-surprise setup on purchased estimates. Instead:

1. Derive the announcement *timestamp* from SEC `acceptanceDateTime` on the 8-K carrying Item 2.02 (Results of Operations). That is free, exact to the second, in UTC, and unimpeachably point-in-time. It will not cover every announcement — companies sometimes press-release before filing — but where it exists it is better than any vendor field.
2. Define surprise **without** analyst estimates: a seasonal random-walk SUE (actual EPS minus the same quarter a year earlier, scaled by the standard deviation of that difference) is computable entirely from SEC XBRL companyfacts using `filed`/`accn` for vintage correctness. It is a weaker surprise proxy than analyst-based SUE, and the PEAD literature finds analyst-based measures stronger — but **it is honest, free, and reproducible, and a weaker-but-clean signal beats a stronger-but-contaminated one in a system whose entire selling point is point-in-time integrity.**
3. If and when you buy estimates, buy Zacks point-in-time specifically, and validate it by checking whether the stored consensus for a past quarter changes between two downloads a month apart. If it changes, it is restated.

---

### 24. Delisting returns

**Verdict on your question: I found no more recent authoritative work than Shumway, and the vendor representations are UNVERIFIED.**

The canonical numbers remain Shumway (1997), *The Delisting Bias in CRSP Data*, and Shumway & Warther (1999), *The Delisting Bias in CRSP's Nasdaq Data and Its Implications for the Size Effect*, both in the *Journal of Finance*. Full texts at https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf and https://tylergshumway.org/Shumway-DelistingBiasCRSPs-1999.pdf.

The findings you need:

- **99.8% of returns are missing for performance-related delistings**, versus at most 1% missing for mergers, exchanges, or moves to NYSE/AMEX.
- The recommended correction for missing performance-related delisting returns is **−55%** (Shumway & Warther, Nasdaq).
- Frequency differs sharply by venue: **~1.2% of NYSE/AMEX stocks delist for poor performance each year versus ~5.6% of Nasdaq stocks.**
- After correction, "there is no evidence that there ever was a size effect on Nasdaq" — an illustration of how large the bias can be.

Beard, Beveridge & Hunter's *Delisting returns and their effect on accounting-based market anomalies* (Journal of Accounting and Economics, https://www.sciencedirect.com/science/article/abs/pii/S0165410106000930) extends this to accounting anomalies — but it is from 2006, older than your 2025–2026 preference, and **I did not read it.**

**More recent work: I found none, and I want to be clear that this is "I did not find it," not "it does not exist."** My search was not exhaustive on this point.

**How Sharadar and EODHD represent a delisted name's final price: UNVERIFIED for both.** This is a specific, answerable question that neither vendor documents where I could reach it, and it is *the* question that determines whether a "delisting-complete" price file is actually usable. The failure mode is subtle: a vendor that simply stops the series on the last trading day, with no terminal return, produces a file that *looks* survivorship-free (the ticker is present with full history) while still omitting the −55% that matters. **Your cohort statistics would be biased upward and nothing in the data would flag it.**

**Concrete test to run on any candidate vendor, before paying:** take twenty known performance-related delistings (Chapter 11 filings, exchange deficiency delistings) from 2015–2024, pull each ticker's final rows, and check whether the last observation reflects a collapse to near-zero or simply stops at the last quoted price. If it stops, you must synthesise the terminal return yourself — and Shumway's −55% is the literature default for doing so.

**Design note for your `insufficient` refusal:** a cohort whose members disproportionately delisted is exactly a cohort where your sample floor will be met by survivors only. Consider making delisting-rate-within-cohort a first-class output alongside the return statistics, so the refusal can trigger on *composition*, not just on *n*.

---

### 25. PEAD in 2020–2026

**Summary: PEAD has not disappeared, but the recent literature has moved from "does it exist" to "where does it survive," and the honest expectation for your engine should be conditional, not unconditional, drift.**

The most directly relevant recent paper I found: McCarthy, *Prior-Biased Inference in Asset Prices: The Conditional Post-Earnings Announcement Drift* (SSRN, June 2025, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5311906). Its finding is that drift concentrates where the earnings surprise **contradicts** analysts' standing recommendations; a strategy shorting Buy-rated firms with negative surprises earns a reported **9.4% abnormal return per annum**. The paper's framing is that PEAD "has not disappeared: it survives in recommendation-inconsistent states, especially after bad news to optimistically-rated firms."

On surprise definition, the relevant methodological paper is Chen, Tang, Yao & Zhou, *PEAD.txt: Post-Earnings-Announcement Drift Using Text* (*Journal of Financial and Quantitative Analysis*, https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/peadtxt-postearningsannouncement-drift-using-text/5EB217BB68B5FB054FE38541BAAC4679), which constructs a text-based surprise measure as an alternative to SUE. Also relevant: 2024 work finding PEAD is stronger when earnings surprises are **correlated** across firms, and a 2025 paper *Beyond the last surprise: Reviving PEAD with machine learning and historical earnings*.

**Every one of these I know through abstracts and search extraction. I did not read the full papers, and I have not verified their magnitudes, samples, or whether the 9.4% is net of transaction costs. Treat all magnitudes here as UNVERIFIED.**

**What I cannot give you, and you specifically asked for: magnitude at 5/10/20-day horizons, and variation by market cap.** I did not find a recent paper reporting a clean horizon decomposition, and I am not going to interpolate one. This is a real gap.

**Set your expectations accordingly.** The direction of the literature — drift surviving in conditional subsets, requiring interaction with analyst recommendations or text or cross-sectional correlation to show up — is precisely the setting where a comparable-setups engine finds *something* in every slice it cuts. If unconditional PEAD is weak and conditional PEAD is strong, then the number of conditioning variables you can try is the number of ways you can manufacture a result. **Your `insufficient` refusal and your null tests are the load-bearing parts of the design here, and they will be under more strain than you expect.** Pre-register the conditioning variables. Count them. Report the count.

---

### 26. Event-study inference with clustered daily events

**Best current practice for your exact case: block bootstrap for the time-series dependence within a cohort, combined with calendar-date clustering for the cross-sectional dependence across simultaneous events. Do not rely on either alone.**

The core problem: earnings season means your events are not independent draws. Dozens of firms report in the same week, their abnormal returns share a common market/factor shock on that day, and standard cross-sectional t-tests on CARs assume away exactly that. The classic reference for this is Kolari & Pynnönen's work on cross-sectional correlation in event studies; the more recent packaged implementation is **`crseEventStudy`** (CRAN, https://cran.r-project.org/web/packages/crseEventStudy/index.html), which weights abnormal returns by their standard deviation to handle heteroskedasticity and "adopts clustering techniques following Cameron et al. (2011) for computing cross-sectional correlation robust standard errors," claiming robustness to "returns' cross-sectional correlation, autocorrelation, and volatility clustering without power loss."

**Caveat that matters for you: `crseEventStudy` is explicitly designed for *long-horizon* event studies.** Your horizon is 1–20 sessions, which is short-horizon. The size/power problems it solves are most acute at long horizons. Do not adopt it uncritically for a 5-day window.

A relevant warning from the simulation literature: cluster-robust standard errors in event-study designs "generally exhibit a decreasing trend over the pre-event periods… and more so when intra-cluster correlation is high" (*Variation in standard errors in event-study design*, Applied Economics 55:5, https://www.tandfonline.com/doi/abs/10.1080/00036846.2022.2091109). In plain terms: **CRSE can produce pre-event standard errors that shrink as you approach the event, which manufactures apparent significance in exactly the window you care about.** Plot your pre-event SEs. If they trend, distrust the post-event ones.

**Maintained Python implementations: I found none that I would recommend, and this is a genuine gap.** `crseEventStudy` is R-only. `statsmodels` gives you two-way clustering via `cov_type='cluster'` with `groups` as a 2-column array (firm and date) in a calendar-time panel regression, and `arch.bootstrap.StationaryBootstrap` with `optimal_block_length` gives you the time-series piece — both verified present in §17. **You will be assembling this from parts, not importing a package.**

**My concrete recommendation for your engine:**

1. **Calendar-time portfolio regression as the primary estimator.** Form a portfolio on each date of all names currently in an active event window, take that portfolio's daily return series, and regress on your benchmark. This *dissolves* the cross-sectional clustering problem instead of correcting for it — simultaneous events become one observation, which is what they economically are. It is the most defensible thing you can do and it is simple. Its cost is power: you get one time series, not N events.
2. **Stationary block bootstrap on that portfolio series** for the confidence interval, with `optimal_block_length` choosing the block. This handles the residual autocorrelation and volatility clustering.
3. **Two-way clustered CAR regression as a cross-check, not the headline.** If the calendar-time and the clustered-CAR answers disagree materially, report the disagreement rather than picking the friendlier one.
4. **Report the number of distinct event *dates*, not just the number of events.** Fifty events on four dates is four observations wearing a disguise. **This should feed your `insufficient` floor: your sample floor ought to be on effective independent observations, not raw n.** As currently described, your design floors on sample size — I think that is the wrong denominator.

---

### 27. Empirical-Bayes shrinkage: choosing k

**The principled answer is that k is not free — it is determined by the ratio of within-cohort noise to between-cohort signal, and you estimate it by method of moments from the family.**

The setup: cohort *i* has observed mean \(\bar{x}_i\) from \(n_i\) observations; the family has pooled mean \(\mu\). You shrink toward \(\mu\) with weight \(n_i/(n_i+k)\).

Under the standard normal-normal hierarchical model — \(\bar{x}_i \mid \theta_i \sim N(\theta_i, \sigma^2/n_i)\) and \(\theta_i \sim N(\mu, \tau^2)\) — the posterior mean is exactly \(\frac{n_i}{n_i + \sigma^2/\tau^2}\bar{x}_i + \frac{\sigma^2/\tau^2}{n_i + \sigma^2/\tau^2}\mu\).

**So \(k = \sigma^2/\tau^2\): within-cohort variance over between-cohort variance.** It is not a tuning knob. A fixed k is an unexamined assumption about that ratio.

**Method of moments estimation**, which is what you should do:

1. \(\hat\sigma^2\) = pooled within-cohort variance of individual event returns.
2. Total between-cohort variance of the \(\bar{x}_i\) decomposes as \(\tau^2 + \sigma^2/n_i\). So compute the observed variance of cohort means, subtract the average sampling variance \(\overline{\sigma^2/n_i}\), and what remains estimates \(\tau^2\).
3. \(\hat k = \hat\sigma^2/\hat\tau^2\).

**The failure mode you must handle:** step 2 can return a *negative* \(\hat\tau^2\) when cohorts genuinely differ no more than sampling noise would predict. Truncating at zero gives \(k=\infty\) — total shrinkage, every cohort reported as the family mean. **That is not a bug; it is the correct answer, and it is exactly the signal your `insufficient` refusal should surface.** If \(\hat\tau^2 \le 0\), the honest output is "these cohorts are indistinguishable," not a shrunken point estimate. Make sure your implementation can say that rather than silently clamping.

This is the standard empirical-Bayes construction; the general framing ("the prior distribution is learned from the data itself via maximum likelihood or method-of-moments estimation") is textbook, and the classic applied demonstration is Efron & Morris's baseball example, with Brown & Zhao (2006) discussing modifications to the James–Stein constant. The canonical modern field test is *In-season prediction of batting averages: A field test of empirical Bayes and Bayes methodologies* (https://arxiv.org/pdf/0803.3697).

**Reference implementation in Python: I did not find a maintained one and am marking this UNVERIFIED.** R's `ebbr` and `ebayesthresh` are the usual pointers. The method-of-moments version above is about thirty lines of numpy; **write it yourself and unit-test it against a simulation where you know the true \(\tau^2\).** That test is worth more than a library.

**One caution specific to your use.** Empirical Bayes assumes cohorts are exchangeable draws from a common family. If you define cohorts by a variable you *chose because it looked predictive*, they are not exchangeable, and shrinkage will understate the selection problem rather than fix it. **Shrinkage is not a substitute for the null tests.**

---

### 28. Deterministic regime classifiers

**Published, reproducible rule sets with explicit thresholds: I did not find a canonical one, and I am marking this UNVERIFIED.** Practitioner regime rules (200-day moving average slope, realized-vol terciles, VIX thresholds around 20/30, 10y–3m curve inversion) circulate widely but I found no peer-reviewed paper that publishes a specific reproducible threshold set as its contribution. If you want one, you will be defining it yourself.

**On the comparison you asked about, there is good recent evidence, and it does not favour HMMs.**

The key reference is Nystrup, Kolm and co-authors' work on statistical jump models: *Downside Risk Reduction Using Regime-Switching Signals: A Statistical Jump Model Approach* (arXiv 2402.05272, published in *Journal of Asset Management*, https://link.springer.com/article/10.1057/s41260-024-00376-x). The findings, as reported in the abstract and paper:

> The jump model "distinguishes itself from traditional Markov-switching models by enhancing regime persistence through a jump penalty applied at each state transition."

> HMM failure mode: "The high imbalance and persistence of regimes, a low signal-to-noise ratio, and limited data availability often cause HMMs to generate state sequences that lack persistence and stability, leading to frequent false alarms."

> Result: "consistent outperformance of the JM-guided strategy in reducing risk metrics such as volatility and maximum drawdown, and enhancing risk-adjusted returns like the Sharpe ratio, when compared to both hidden Markov model-guided strategy and the buy-and-hold strategy," evaluated out-of-sample on US, German and Japanese equity indices 1990–2023 "in the presence of transaction costs and trading delays."

Related: *Dynamic Asset Allocation with Asset-Specific Regime Forecasts* (arXiv 2406.09578) and *Dynamic Factor Allocation Leveraging Regime-Switching Signals* (arXiv 2410.14841). **All read at abstract level only — magnitudes UNVERIFIED.**

**What this means for your design.** The jump penalty is the whole idea: it is an explicit regularizer for regime persistence, which is precisely the property a deterministic threshold rule has for free. **The evidence that JMs beat HMMs is, read one way, evidence that persistence matters more than statistical sophistication** — which argues for your deterministic-rules instinct rather than against it.

Given your constraint that "scheduled jobs are deterministic Python," a fitted HMM or JM is a poor fit anyway: both are estimated models whose state assignments can change retroactively when refit, which breaks the point-in-time guarantee you have built everything else around. **A deterministic rule set — even an arbitrary one — has the property that yesterday's regime label never changes. That is worth more to you than out-of-sample Sharpe.** Define your thresholds, document them, freeze them, and treat the regime label as a fact with a `known_at_utc` like everything else.

---

### 29. Wash sale — the rule text

**VERIFIED verbatim from IRS Publication 550**, https://www.irs.gov/publications/p550:

> "You cannot deduct losses from sales or trades of stock or securities in a wash sale unless the loss was incurred in the ordinary course of your business as a dealer in stock or securities."

> "A wash sale occurs when you sell or trade stock or securities at a loss and within 30 days before or after the sale you:
> 1. Buy substantially identical stock or securities,
> 2. Acquire substantially identical stock or securities in a fully taxable trade,
> 3. Acquire a contract or option to buy substantially identical stock or securities, or
> 4. Acquire substantially identical stock for your individual retirement arrangement (IRA) or Roth IRA."

> "If you sell stock and your spouse or a corporation you control buys substantially identical stock, you also have a wash sale."

> "If your loss was disallowed because of the wash sale rules, add the disallowed loss to the cost of the new stock or securities (except in (4) above). The result is your basis in the new stock or securities. This adjustment postpones the loss deduction until the disposition of the new stock or securities. Your holding period for the new stock or securities includes the holding period of the stock or securities sold."

On "substantially identical":

> "In determining whether stock or securities are substantially identical, you must consider all the facts and circumstances in your particular case. Ordinarily, stocks or securities of one corporation are not considered substantially identical to stocks or securities of another corporation. However, they may be substantially identical in some cases. For example, in a reorganization, the stocks and securities of the predecessor and successor corporations may be substantially identical."

> "Similarly, bonds or preferred stock of a corporation are not ordinarily considered substantially identical to the common stock of the same corporation. However, where the bonds or preferred stock are convertible into common stock of the same corporation, the relative values, price changes, and other circumstances may make these bonds or preferred stock and the common stock substantially identical."

On options and futures:

> "The wash sale rules apply to losses from sales or trades of contracts and options to acquire or sell stock or securities. They do not apply to losses from sales or trades of commodity futures contracts and foreign currencies."

**Implementation notes for a purely informational flag.**

- The window is **61 days total**: 30 before, the sale day, 30 after. Off-by-one here is the most common bug.
- **Note the asymmetry in item (4):** an IRA/Roth purchase triggers the wash sale, but the basis adjustment does *not* apply — "(except in (4) above)". The loss is permanently disallowed, not deferred. Your flag should distinguish these two outcomes because they differ enormously in consequence.
- The rule reaches **across accounts and across persons** — spouse and controlled corporations. A flag that only inspects the Agentic account will under-report. Since Robinhood's MCP gives read access to all your accounts (Claim 2), you can at least check across your own Robinhood accounts; you cannot see a spouse's or another broker's.
- **"Substantially identical" is a facts-and-circumstances test, not a computation.** Do not resolve it in code. Flag same-CUSIP/same-FIGI matches as high confidence, flag convertibles and same-issuer different-class as "possible — requires judgement," and never emit a determination. This is also the cleanest way to keep the feature informational rather than advisory.

---

### 30. Position sizing under uncertain edge

**The argument against full Kelly when the edge estimate is uncertain is not subtle, and it is decisive: Kelly's optimality is conditional on knowing the true distribution, and the penalty for overbetting is asymmetric and severe.**

Kelly maximises the expected log growth rate given known \(p\) and payoffs. The growth curve is concave with a maximum at \(f^*\) and a zero-crossing at \(2f^*\) — **bet twice Kelly and your long-run growth rate is zero; bet more and it is negative.** Since your edge estimate has a wide confidence interval, and since the estimate is derived from the same data that suggested the trade, \(\hat{f}^*\) is biased upward by selection. Betting \(\hat{f}^*\) is therefore systematically overbetting.

The standard practitioner response is fractional Kelly, and the arithmetic is favourable: **half-Kelly retains roughly 75% of the theoretical growth rate while halving volatility.** Quarter-Kelly is the usual choice when estimation uncertainty is high. The consensus framing across the sources I read is that Kelly is best treated as "a theoretical upper bound and discipline framework rather than a literal full-size target."

**Carver's volatility-targeting approach is the better primary framework for you**, and his own heuristic is worth quoting because it makes the Kelly connection explicit: **your volatility target should roughly match your Sharpe ratio** — a system with SR 0.25 should run a 25% volatility target. That *is* half-Kelly in disguise (full Kelly for a continuous-return system is a vol target equal to the Sharpe; Carver's recommendation to run at or below that, and his explicit endorsement of "setting risk at half the optimal level," lands you at fractional Kelly without ever computing a Kelly fraction). Primary reference: Rob Carver, *Systematic Trading* (2015) and *Leveraged Trading*.

**All of the above is corroborated across several secondary sources (CXO Advisory's notes on *Systematic Trading*, the7circles.uk's chapter summary, QuanterLab, and arXiv 2508.16598 on Kelly/VIX hybrids in put-writing) — I did not read Carver's books directly in this pass, so the specific "vol target = Sharpe" formulation is UNVERIFIED against the primary text.** It is consistently reported and mathematically coherent, but check the book.

**What I would actually recommend, given your specific situation.**

You are one person, long-only, 1–20 sessions, with an edge whose confidence interval your own engine is designed to report. **Use fixed-fractional risk-per-trade (a fixed percentage of equity at risk between entry and stop), sized so that the volatility of the resulting portfolio hits a target you set from your *lower confidence bound* Sharpe, not your point estimate.** That is the sizing rule that takes your uncertainty seriously instead of just acknowledging it.

**And the sharper point: your comparable-setups engine outputs a distribution, not a number. That is unusually good raw material for sizing — and using only its mean to size positions throws away the thing that makes it valuable.** Size from the lower bound of the block-bootstrap CI. If that bound is negative, the correct size is zero, and your engine has just earned its keep.

---

### 31. Prompt-injection defences in 2026

**The strongest current evidence favours design-level isolation (CaMeL-style) over any text-level mitigation. Delimiter and nonce wrapping have essentially no measured defensive value against a competent attacker and should not be counted as a control.**

The design-level approach is Debenedetti et al., *Defeating Prompt Injections by Design* (CaMeL), arXiv 2503.18813. The measured result on AgentDojo:

> "The number of successful attacks with CaMeL is 0, while the number of successful attacks with the next best defense (tool filter) is 8."

The mechanism matters: CaMeL runs a privileged LLM that emits a program over *capabilities*, and a quarantined LLM that parses untrusted content but cannot call tools. Untrusted data can supply values but never control flow. That is a security architecture, not a prompt.

The 2026 follow-up literature is more sobering and you should read it before feeling safe. *Adaptive Evaluation of Out-of-Band Defenses Against Prompt Injection in LLM Agents* (arXiv 2606.26479) surveys CaMeL, FIDES, Progent, RTBAS and FORGE — noting several "report near-elimination of attacks on the AgentDojo benchmark" — and then lands the critical caveat:

> "every one of them is validated only on static benchmarks (a fixed set of injection attempts)"

Under adaptive attack, Progent's mean attack success went from 25.8% to 4.2%, and a hand-crafted adaptive attack did not raise it further (2.6%) — a good result, but not zero. And *Agent Data Injection Attacks are Realistic Threats to AI Agents* (arXiv 2607.05120) reports that **"Only CaMeL Strict fully prevented ADI (0% ASR), while all other defenses allowed 22.2–50.0% of attacks to succeed."** Spotlighting appears in these evaluations as one of the weaker prompting-based defences; see also *Evaluating Prompting-Based Defenses Against Domain-Camouflaged Injection Attacks* (arXiv 2606.18530) and *Adaptive Attacks Break Defenses Against Indirect Prompt Injection Attacks on LLM Agents* (arXiv 2503.00061).

**All read at abstract/summary level. Specific ASR figures are UNVERIFIED against the papers' tables.**

**Claude Code and Codex subagent tool allowlists: UNVERIFIED.** I did not fetch the official documentation for either product's subagent tool-restriction mechanism in this pass. Claude Code does support per-subagent tool restriction (agent definitions carry a tools list), but I am not going to quote a mechanism I did not read the docs for. Check https://code.claude.com/docs for the current agent-definition frontmatter schema.

**What this means for your system specifically, and it is more urgent than it looks.** Your evidence planes ingest SEC filing text, news articles, and 13D/G narrative sections. **All three are attacker-writable.** Anyone who can file with EDGAR or place a press release can put text in front of your agent. The 8-K free-text and the news body are the obvious vectors; a 13D purpose-of-transaction section is a less obvious one.

Your existing constraints already do most of the work, and you should recognise how much: **"agents never place orders" and "no LLM output is ever a statistic" together mean a successful injection cannot move money and cannot corrupt a number.** That is a far stronger position than most agent systems occupy. What an injection *can* still do is corrupt a dossier, poison a thesis, or manipulate the framing a human reads before approving a trade — which is a real but bounded harm.

Three things worth adding:

1. **Structural separation, not prompt hardening.** The job that parses filings and news should not be the job that can write to the ledger. Give the ingestion agent a tool allowlist containing no write tools at all.
2. **Provenance on every ingested claim.** You already carry `known_at_utc`; carry `source_url` and `source_trust` alongside, and render untrusted-origin text distinctly wherever a human reads it.
3. **Do not count delimiters as a defence.** Wrap untrusted content by all means — it helps the model parse — but do not let its presence lower your guard anywhere else in the design.

---

### 32. Personal research-workspace precedents

**I found less than I hoped, and what I found is weaker evidence than you want. Marking this section as thin.**

The closest public precedents I located:

- **`fafawlf/thesis-agent`** (https://github.com/fafawlf/thesis-agent) — self-described as a "Multi-agent LLM investment system: 3 AI analysts + bull/bear debate + deterministic Decision Hub." Its stated architectural principle is exactly yours: *"LLMs reason, math decides… By using LLM outputs as inputs to a deterministic function, you get rich qualitative reasoning with reproducible quantitative decisions."* **Its headline claim of "+92.5% vs QQQ +33.6% over 4.2 years" is a backtest with no verified methodology and I would give it no evidential weight whatsoever** — a 4.2-year backtest by the system's own author, with no described point-in-time discipline, is the exact artefact your whole design exists to avoid producing. Read it for architecture, ignore the number.

- **"InvestMate"** — described in *High-Stakes Personalization: Rethinking LLM Customization for Individual Investor Decision-Making* (arXiv 2604.04300). Its central abstraction is the "living thesis": "a structured, per-holding hypothesis capturing not just what an investor owns but why: a conviction statement, validation triggers, break conditions, macro dependencies, and upcoming catalysts," evaluated daily against market signals via a structured LLM call. The reported motivating failures are worth having: "losing context between sessions, receiving advice untethered from actual investment rationale, and watching models latch onto recency rather than the thesis that justified a position weeks ago."

  **"Validation triggers" and "break conditions" are your falsifiable invalidators under different names — independent convergence on the same primitive, which is mild evidence you have the right one.**

- **InvestLogicBench (arXiv 2608.06108, August 2026)** — a benchmark built from documented real-investor decision traces. Its headline finding is the useful one: "state-of-the-art LLMs encounter significant bottlenecks in both emulating expert investor reasoning and translating such reasoning into profitable outcomes." **That is evidence for your "no LLM output is ever a statistic" constraint.**

- Survey context: *Large Language Model Agents for Investment Management: Foundations, Benchmarks, and Research Frontiers* (ACM ICAIF '25, https://dl.acm.org/doi/10.1145/3768292.3770387).

**All read at abstract/description level. None fetched in full. What did they abandon? I could not answer that — none of these sources documents abandoned approaches, and I found no post-mortem write-ups.** The genre you were hoping for — a builder's honest retrospective on a personal research system — I did not find. That may be because it is rare for people to publish those, or because my search was too narrow.

**One inference I will offer, flagged as inference and not evidence:** every precedent I found is architecturally *lighter* than what you are proposing. None implements point-in-time integrity, block-bootstrap uncertainty, regime stratification, null tests, or an explicit refusal threshold. That is either your differentiator or your over-engineering, and Part 3 argues it is partly both.

---

## Part 3 — Attacking the plan

### 33. Ten ways this produces confidently wrong numbers

Ranked by expected damage × probability, and restricted to things your stated design does *not* already guard against.

**1. Your sample floor counts events, not independent observations.** Earnings cluster: fifty events across four reporting days share four market shocks. A block bootstrap over the *event* dimension does not fix cross-sectional dependence on the same calendar date, and your CI will be too narrow by roughly √(events per date). Evidence: this is the standard clustered-events problem (§26), and the CRSE simulation literature shows the failure gets worse as intra-cluster correlation rises (Applied Economics 55:5). **Fix: floor on distinct event dates, and make calendar-time portfolio regression the primary estimator.** This is the single highest-value change in this report.

**2. Point-in-time integrity is only as good as your worst evidence plane, and the analyst-estimate plane has no honest source at your price point.** §23 found no verifiable retail point-in-time consensus archive. If you build the earnings-surprise setup on a restated consensus, every downstream guarantee is decorative — you will have block-bootstrapped, regime-stratified, null-tested confidence intervals around a look-ahead-contaminated statistic, and the rigour of the wrapper will make the number *more* believable, not less. **This is the most dangerous single item in the report** precisely because the failure is invisible and the surrounding machinery is trustworthy.

**3. Delisting completeness is unverified for every vendor you named, and the failure mode is silent.** §24: a vendor can retain a delisted ticker's full history and still omit the terminal return. The file looks survivorship-free. Your cohorts inherit an upward bias whose magnitude is unbounded and whose presence nothing flags. Shumway's −55% correction and the 99.8%-missing statistic say how large this can be. **Fix: run the twenty-delisting audit in §24 before paying any vendor, and make within-cohort delisting rate a reported output.**

**4. Polygon/Massive returns split-adjusted-only prices — verified from their own docs (§8).** If Massive is anywhere in your price path, every return you compute is a price return, not a total return, and the error concentrates around ex-dividend dates. For dividend-paying names at a 20-day horizon this is not noise. **Fix: build the dividend adjustment yourself, or use a vendor that does it.**

**5. `filingDate` instead of `acceptanceDateTime` leaks a full session.** Verified live in §13: Apple's most recent Form 4 has `filingDate: 2026-09-03` and `acceptanceDateTime: 2026-09-03T22:30:44Z` — accepted after the close. Any setup keyed on filing date treats post-close information as available intraday. The bias is systematic and favours your hypothesis. **Fix: `acceptanceDateTime`, always, and assert it in a test.**

**6. XBRL coverage is per-tag, not per-company (§13).** Filers migrate tags (`Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax`). A naive companyfacts pipeline produces gaps that look like missing quarters and silently drops firms from cohorts — non-randomly, since tag migration correlates with filer size and accounting complexity. **Fix: explicit tag-alias maps with tests, and alert on coverage discontinuities rather than absorbing them.**

**7. Sector stratification is contaminated by look-ahead and there is no affordable fix (§22).** Historical GICS is institutionally licensed. Any sector field you can buy is probably current-vintage, which means a company that migrated sectors is stratified by where it ended up. **Fix: drop sector stratification; stratify on market-cap decile, realized-vol decile, or price level — all computable point-in-time from your own file.**

**8. Conditioning-variable proliferation, and the PEAD literature will encourage it.** §25: recent work finds drift surviving mainly in conditional subsets — recommendation-inconsistent states, correlated-surprise regimes, text-based measures. Every one of those is a conditioning variable, and your engine can slice on all of them. With regime stratification × surprise definition × cap bucket × horizon, you have hundreds of cells and will find significance in some. Your null tests help; they do not fix multiplicity. **Fix: use `arch`'s `SPA`/`StepM`/`MCS` (verified present, §17) as first-class outputs, pre-register the conditioning set, and report the number of cells examined next to every result.**

**9. Robinhood's stop-order semantics are undocumented, so your simulated exits may not be executable (§3).** Your exit simulator assumes a protective stop. If the MCP cannot attach a stop to an entry, there is a window between fill and stop placement where you are unprotected; if standalone stops are not indefinitely GTC, a 20-session hold can outlive its stop. **Your backtested exit distribution would then describe a policy you cannot actually run** — and the gap shows up as unexplained negative slippage that looks like market impact. **Fix: dump the `place_equity_order` JSON Schema from `tools/list`, then model the exit mechanism you actually have.**

**10. The unattended OAuth session is undocumented behaviour on a beta product (§5).** Robinhood publishes no token lifetime, no refresh guarantee, and no rate limits, and its documented recovery path is a human reconnect on a desktop. Your scheduled deterministic jobs will fail in a way that produces *stale* data rather than *no* data if any layer caches. **A ledger that silently stops syncing is worse than one that loudly breaks.** Fix: assert freshness on every read — if `known_at_utc` is older than a threshold, refuse rather than serve.

**Two more, because they are cheap to state and you should know them.** *(a)* The `mcp>=1.27.2` unbounded pin breaks on a fresh install today and presents as "SDK not installed" (§18) — this is live right now. *(b)* Tiingo's internal-use licence forbids displaying or sharing data, and your repo is public (§9); Alpaca's terms cover "derived products" (§11). Neither is a numbers problem, but a licence violation discovered after the fact is unwindable in a way a bad number is not, because git history is permanent.

---

### 34. Over-engineered / missing

**What an experienced quant would call over-engineered for one person at 1–20 days:**

- **Regime stratification.** With daily bars, US equities, and a realistic event count, conditioning on regime multiplies your cells and divides your already-small samples. You will hit `insufficient` in most cells, and the cells that *do* clear the floor will be the common regimes — which is to say, you will have spent significant complexity to learn about normal markets. The jump-model literature (§28) is about asset allocation over decades, not 20-day swing setups.
- **Block-bootstrap-plus-regime-plus-null-tests as separate machinery.** Pick the calendar-time portfolio regression (§26) and you get most of the dependence handling structurally, for far less code.
- **Three evidence planes at once.** 13F is quarterly with a 45-day lag — nearly useless at a 20-day horizon. 13D/G is genuinely event-driven and worth having. Form 4 is worth having. **13F is the one to cut**, and it is the most work of the three.
- **The comparable-setups engine's full generality.** "Given this setup, how have setups genuinely like it performed" is a research platform. For one person with one strategy, a small number of hard-coded, pre-registered setup definitions would deliver most of the value with none of the multiplicity risk — and would make the multiplicity *countable*.

**What they would say is missing:**

- **Transaction costs and a realistic fill model.** Not mentioned anywhere in your description. At 1–20 days with a swing-trading turnover, spread plus impact can consume the entire measured edge. A base-rate engine that reports gross returns is reporting a number that does not exist. **This is the largest omission.**
- **An effective-sample-size concept.** See failure mode 1. Your `insufficient` floor is on the wrong denominator.
- **Capacity and position-count realism.** One person, long-only, with an Agentic account of bounded size, holding N positions of 1–20 days — the achievable number of independent bets per year is small, and it bounds how much any edge can matter. Worth computing before building.
- **A live-vs-backtest reconciliation loop.** You have a decision journal with Brier scores for *your* predictions. There is no equivalent for the *engine's* predictions. Every time the engine says "cohort mean +2.1%, CI [0.3, 3.9]," record it and score it later. **After a year that is the only honest evidence about whether any of this works** — and it is the one measurement no amount of methodological rigour can substitute for.
- **T+1 settlement modelling.** Verified constraint on cash Agentic accounts (§2). It changes achievable turnover.

---

### 35. Costs

**Recommended stack (verified prices only):**

| Item | Price | Verified |
|---|---|---|
| Railway Pro | $20/mo (incl. $20 usage credit) | ✅ railway.com/pricing |
| Railway overage (app + Postgres, ~1.5 GB RAM total) | ~$10–20/mo | Derived from verified $0.00000386/GB/s |
| Tiingo Power (EOD prices, splits/dividends) | $30/mo | ✅ tiingo.com/about/pricing |
| SEC EDGAR (filings, XBRL, acceptanceDateTime) | $0 | ✅ 10 req/s, UA required |
| FRED/ALFRED | $0 | Not re-verified this pass |
| Alpaca News (Benzinga, 2015→) | $0 on Basic | Strong secondary |
| OpenFIGI (with key) | $0 | ✅ openfigi.com/api/documentation |
| `fja05680/sp500` index membership | $0, MIT | ✅ repo |
| **Total** | **~$60–70/month** | |

**Sharadar is deliberately absent because I could not verify its price (§7), and I am not going to put an unverified number in a cost table.** Resolve that yourself; it is the one line that could move this materially.

**Cheapest stack that still yields delisting-complete daily prices:**

**EODHD "EOD Historical Data — All World" at $19.99/mo** (✅ eodhd.com/pricing), which states delisted tickers are available in all packages with EOD prices, dividends and splits, and exposes an `IsDelisted` flag for US stocks. Plus free SEC, free FRED, free Alpaca News, free OpenFIGI, free `fja05680/sp500`, and Railway.

**Total: ~$40–50/month** ($19.99 data + $20 Railway + overage).

**Two caveats that matter more than the $10 saved.** First, EODHD's index constituent history is ~12 years (≈2014→), so this stack cannot stratify on 2008–2009. Second, and more important: **"delisting-complete" here means the vendor says delisted tickers are retained — it does not mean the terminal return is present.** Until you run the audit in §24, neither stack is verified delisting-complete, and this cost comparison is between two options whose key property is unconfirmed.

---

## What to change before building

**Do these five things first. They are cheap and they change the design.**

1. **Pin `mcp>=1.27.2,<2` in `requirements.txt` today.** A fresh install resolves to 2.1.1, where `streamablehttp_client` no longer exists, and your error handler mislabels it "Install the MCP SDK." Verified by installing 2.1.1 and probing every import you use. One line, live bug.

2. **Dump the Robinhood `tools/list` JSON Schema for `place_equity_order` and `review_equity_order`.** Your `_tools_cache` already fetches it. This answers order types, brackets, stop persistence and time-in-force — none of which Robinhood documents — in ten minutes, and it determines whether your exit simulator models an executable policy or a fictional one. **Nothing else in this report is blocked on so little work.**

3. **Change the `insufficient` floor from event count to distinct event dates**, and make calendar-time portfolio regression the primary estimator with clustered-CAR as a cross-check. This is the difference between confidence intervals that mean something and confidence intervals that are too narrow by a factor you never see.

4. **Switch every filing timestamp to `acceptanceDateTime`** and add a test that fails if `filingDate` is used as a `known_at`. Verified live: Apple's latest Form 4 was accepted at 22:30 UTC on its filing date. This is a one-session look-ahead leak in the direction that flatters your results.

5. **Run the twenty-delisting audit before paying any data vendor.** Pull twenty known performance-related delistings from 2015–2024 and check whether the final observation is a collapse or just a stop. If it just stops, you must synthesise terminal returns (Shumway's −55% is the literature default) regardless of which vendor you pick.

**Three scope cuts I would make.**

Drop **13F** (quarterly, 45-day lag, useless at 20 days, most work of the three planes). Drop **FNSPID** (CC BY-NC-4.0, commercial use prohibited, and Alpaca already covers 2015→). Drop **sector stratification** and replace it with market-cap and realized-vol deciles — you cannot buy honest historical GICS, and a stratification you can compute correctly beats one you can only approximate.

**Two things to add.**

**Transaction costs and a fill model** — the largest omission in the design; gross-return base rates at swing-trading turnover describe a strategy nobody can run. And **a scoring loop for the engine's own predictions**, parallel to your decision journal's Brier scores. After a year, that log is the only real evidence about whether any of this machinery works.

**Two licence problems to resolve before the first public commit.** Tiingo's internal-use terms — "you may not display or share the data with another person or organization" — and Alpaca's prohibition on distributing "any derived products or services." Commit aggregate statistics, never cached vendor series. Git history does not forget.

**And one thing to keep, because it is better than you may realise.** "Agents never place orders" plus "no LLM output is ever a statistic" means a successful prompt injection through your SEC/news planes — and those planes are attacker-writable — cannot move money and cannot corrupt a number. The 2026 literature (§31) says every text-level defence fails under adaptive attack and only architectural isolation holds. You already have the architecture. Do not trade it away for convenience later.

---

## Sources

Robinhood: [Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/) · [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/) · [Robinhood is Now Open to Agents](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/) · [Agentic Trading](https://robinhood.com/us/en/agentic-trading/)

Data vendors: [Tiingo pricing](https://www.tiingo.com/about/pricing) · [Tiingo splits docs](https://www.tiingo.com/documentation/corporate-actions/splits) · [Massive pricing](https://massive.com/pricing?product=stocks) · [Massive aggregates docs](https://massive.com/docs/rest/stocks/aggregates/custom-bars) · [Polygon is now Massive](https://massive.com/blog/polygon-is-now-massive) · [EODHD pricing](https://eodhd.com/pricing) · [EODHD delisted data](https://eodhd.com/financial-apis/delisted-stock-companies-data-2) · [Alpaca historical news](https://docs.alpaca.markets/us/docs/historical-news-data) · [Alpaca T&C](https://files.alpaca.markets/disclosures/library/Alpaca+Terms+_+Conditions.pdf) · [FNSPID](https://huggingface.co/datasets/Zihan1004/FNSPID) · [QuantRocket Sharadar pricing (gated)](https://www.quantrocket.com/pricing/data/sharadar/)

SEC & identifiers: [EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) · [SEC webmaster FAQ](https://www.sec.gov/os/webmaster-faq) · [edgartools on PyPI](https://pypi.org/project/edgartools/) · [OpenFIGI API docs](https://www.openfigi.com/api/documentation)

Libraries & infra: [MCP Python SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases) · [mlfinlab licence](https://github.com/hudson-and-thames/mlfinlab/blob/master/LICENSE.txt) · [Railway pricing](https://railway.com/pricing) · [Codex MCP docs](https://developers.openai.com/codex/mcp) · [Claude connector auth](https://claude.com/docs/connectors/building/authentication)

Method & literature: [IRS Pub 550](https://www.irs.gov/publications/p550) · [Shumway 1997](https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf) · [Shumway & Warther 1999](https://tylergshumway.org/Shumway-DelistingBiasCRSPs-1999.pdf) · [McCarthy, Conditional PEAD](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5311906) · [PEAD.txt (JFQA)](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/peadtxt-postearningsannouncement-drift-using-text/5EB217BB68B5FB054FE38541BAAC4679) · [crseEventStudy](https://cran.r-project.org/web/packages/crseEventStudy/index.html) · [Variation in SEs in event-study design](https://www.tandfonline.com/doi/abs/10.1080/00036846.2022.2091109) · [Statistical jump models](https://arxiv.org/abs/2402.05272) · [CaMeL](https://arxiv.org/pdf/2503.18813) · [Adaptive evaluation of out-of-band defenses](https://arxiv.org/abs/2606.26479) · [Agent Data Injection](https://arxiv.org/pdf/2607.05120) · [fja05680/sp500](https://github.com/fja05680/sp500) · [thesis-agent](https://github.com/fafawlf/thesis-agent) · [High-Stakes Personalization](https://arxiv.org/html/2604.04300) · [InvestLogicBench](https://arxiv.org/html/2608.06108v1)
