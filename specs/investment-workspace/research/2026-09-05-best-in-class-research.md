# Best-in-class research for the SwingTrader Investment Workspace (specs K–Q)

**Date:** 2026-09-05 · **For:** Bryan Niyonzima · **Scope:** README §5 research slots, OSS survey,
datasets, methodology, agent tooling, brokers, plan critique.

## Method and an honest caveat about verification

Everything below is either **[V]** — verified by fetching the primary page in this session — or
**[S]** — taken from a search-engine extract of the primary page (the snippet quotes the page but I
could not open it) — or **[T]** — recalled from training and not verifiable here. This session's
egress proxy blocks most vendor marketing/pricing domains (`polygon.io`, `tiingo.com`, `eodhd.com`,
`sharadar.com`, `norgatedata.com`, `databento.com`, `site.financialmodelingprep.com`, `sec.gov`,
`claude.com`, `robinhood.com`, `gofastmcp.com`). GitHub, PyPI, `code.claude.com` and search were
reachable. **Any price marked [S] should be re-checked on the vendor page before you spend money.**

---

# Executive summary

## Slot recommendations

| Slot | Recommendation | Runner-up | Why (one line) |
|---|---|---|---|
| 1. Backtest / event-study engine | **Keep and extend `backtest/simulator.py`**; add `alphalens-reloaded`-style tearsheet only if you want charts | `vectorbt` OSS for cohort-wide vectorised return math | Your exit semantics (T+1 open, gap-through stop, pessimistic same-bar, half-at-T1) are the differentiator; no OSS engine reproduces them and Spec N §10 `test_policy_matches_simulator` demands bit-for-bit equality. Adopting an engine means *rewriting the thing you must not change*. |
| 2. PIT prices + corp actions + delisted | **Sharadar SEP + ACTIONS + TICKERS via Nasdaq Data Link** (~$69/mo full history [S]) | **Polygon/Massive Advanced $199/mo** [S] if you need 20yr + intraday; **Tiingo $30/mo** [S] as the cheap fallback | Sharadar is explicitly survivorship-bias-free (25k tickers, ~15k delisted) and flat-file, which suits a Railway cron. **Polygon does *not* provide dividend-adjusted prices** [S] — a real problem for total-return CAR. |
| 3. PIT fundamentals | **SEC XBRL `companyfacts` + `submissions` (free)** as the `clean_pit` source; **Sharadar SF1 `datekey`** as the convenience layer | FMP as-reported (exploratory tier only) | `companyfacts` carries `accn` + `filed` per fact; joining `accn` → `submissions.acceptanceDateTime` gives a *true* `known_at_utc`. That is exactly Spec O §2's rule, for $0. |
| 4. SEC filings + entity resolution | **`edgartools`** (MIT, v5.56.0, 2026-09-02) + raw EDGAR bulk data sets + **OpenFIGI** for CUSIP→ticker | `sec-api.io` Personal $49/mo [S] if parsing cost exceeds $49 of your time | edgartools does 13F, Form 4 with transaction codes, XBRL, full-text search, CIK/ticker lookup, rate-limit aware — free, no key. |
| 5. PIT macro | **Keep `fredapi`** (`get_series_as_of_date` / `get_series_all_releases`) but pin it and wrap it | `pyfredapi` (actively released 2025) if `fredapi` breaks | fredapi already in `requirements.txt` and already does ALFRED. Its maintenance is rated *Inactive* [S], so wrap it behind your own adapter. |
| 6. News with timestamps | **Alpaca News API (Benzinga, back to 2015, free with your existing Alpaca account)** | Tiingo news (bundled with the $30/mo Power plan) | You already have Alpaca credentials. Benzinga-sourced timestamps are publisher-grade; free tier is 200 req/min. |
| 7. Statistical rigor | **`arch` (bootstrap + SPA/StepM/MCS) + `statsmodels` (cluster-robust) + ~200 lines vendored** for deflated Sharpe / PBO / Wilson / empirical Bayes | `purgedcv` (MIT) or `ml4t-diagnostic` (MIT) to avoid writing DSR/PBO yourself | **`mlfinlab` is not open source** — all-rights-reserved, commercial licence required. Do not depend on it. `arch` has `optimal_block_length` (Politis–White) which Spec N §6.1 needs. |
| 8. Remote MCP transport / auth | **Official `mcp` SDK v2 (`MCPServer`) mounted in your FastAPI app**, streamable-HTTP, static bearer token | `fastmcp` 4.x if you want auth providers/OAuth out of the box | Both churn hard (fastmcp 2→3 Feb 2026 → 4 Aug 2026; official SDK 1.x→2.0 Aug 2026 renamed `FastMCP`→`MCPServer`). The official SDK is the smaller surface and you already depend on `mcp>=1.27.2` — **pin `mcp>=1.29,<2` today and migrate deliberately.** |

## Top 10 upgrades (ranked by impact ÷ effort)

1. **Replace the `AGENTS.md`/`CLAUDE.md` parity *test* with a one-line `@AGENTS.md` import.** (Spec K §4.3, Spec P §8) A test that asserts two hand-maintained files agree is a maintenance tax; the official Claude Code pattern is `CLAUDE.md` line 1 = `@AGENTS.md`. Parity becomes structural, not tested. Effort: 20 minutes.
2. **Tighten the balance threshold from |SMD| > 0.25 to > 0.10 for a warning, > 0.25 for a hard `unbalanced` label.** (Spec N §4.5) 0.25 is Rubin's old rule; modern matched-cohort practice is 0.1. At 0.25 you will ship cohorts that are visibly not comparable with no warning.
3. **Add a lookahead-detection harness borrowed from freqtrade** (`lookahead-analysis`, `recursive-analysis`). (Spec N §10) Re-run a cohort with the data truncated at the event date and assert the answer is identical. This catches the failure class Spec N §2 row 2 exists for, mechanically, on every cohort — not just in unit tests.
4. **Make macro `known_at_utc` day-resolution and say so.** (Spec O §4.1) ALFRED vintages are dated, not timestamped. A CPI print released 08:30 ET is only knowable to you as "that date". Either treat macro facts as known at the *close* of their vintage date, or your intraday event cohorts inherit up to 6.5 hours of lookahead. Currently the spec implies a timestamp it cannot get.
5. **Split the price-adjustment requirement into three stored series, not two.** (Spec N §4.3) Raw OHLC, split-adjusted, and total-return (split+dividend). Polygon supplies only the first two [S]; CAR against a *total-return* benchmark computed from *price-return* stock series is a systematic negative bias equal to the dividend yield difference. Store the split and dividend factors with ex-dates, not just the adjusted output.
6. **Add a "primary condition family" registry so the trial count is not gameable.** (Spec N §7) As written, `trials_against_this_pattern` counts variants "against this fact pattern" but nothing defines the equivalence class. Reuse the §6.4 shrinkage `family` slug as the trial-counting key — one concept, two uses.
7. **Add the Robinhood *Agentic account* reality to Spec L §5.1.** (Spec L) The official Robinhood MCP (`agent.robinhood.com/mcp/trading`, OAuth) can only *place* orders in a separately funded Agentic account; everything else is read-only [S]. Your ledger reads across accounts fine, but the propose→approve→execute path can only ever reach the Agentic account. That is a product decision, not an implementation detail, and it should be in the spec before Phase 6.
8. **Add tax-lot accounting and wash-sale *awareness* (not advice).** (Spec L §3 `tax_lots`, Spec M) The spec stores lots but computes nothing. A swing trader with a 61-day wash-sale window who trims and re-adds is generating disallowed losses invisibly. Borrow beancount's booking-method model (STRICT/FIFO/LIFO/AVERAGE per account) and just *flag* "this proposed buy is within 30 days of a realised loss in the same name". Zero tax advice, high value.
9. **Add a data-licensing register to the repo.** (README §3, new row) Tiingo and Polygon both prohibit redistribution of data *and derived works* [S]; yfinance access is against Yahoo's ToS [S]. Your repo is public and mirrors research to Markdown. One `docs/DATA_LICENSES.md` mapping each source → what may be committed → what may only live in Postgres, plus a CI check that `research/` contains no raw vendor price series.
10. **Deflated Sharpe / PBO belong in Spec Q, not Spec N.** (Spec N §7 vs Spec Q §10) Deflated Sharpe is defined over a *strategy's* Sharpe with N trials; a cohort's CAR is not a Sharpe. Spec N should report an empirical p-value adjusted by Romano–Wolf or a simple Šidák/Bonferroni on the logged trial count and say so; Spec Q should carry DSR/PBO/CPCV. Mixing them produces a number that looks rigorous and means nothing.

## Monthly data cost estimate

**Recommended stack (solo operator):**

| Item | Cost/mo | Note |
|---|---|---|
| Sharadar Core US Equities (full history bundle) | ~$69 [S] | SEP prices + SF1 fundamentals + ACTIONS + TICKERS + SP500 constituents |
| SEC EDGAR (filings, 13F, Form 4, XBRL, financial statement data sets) | $0 | free, key-less, 10 req/s |
| FRED/ALFRED | $0 | free key, 120 req/min |
| Alpaca news (Benzinga back to 2015) | $0 | with existing Alpaca account |
| OpenFIGI CUSIP→ticker | $0 | free key |
| Railway (workspace service + Postgres) | ~$10–20 [S] | Hobby $5 + usage; two services + PG |
| FMP (incumbent, keep during migration) | ~$22–29 [T] | drop once Sharadar lands |
| **Total** | **~$100–120/mo** | falling to ~$80–90 once FMP is dropped |

**Free-tier-only alternative (~$10–20/mo, Railway only):**
SEC XBRL companyfacts (PIT fundamentals) + SEC financial statement data sets + FRED/ALFRED +
Alpaca news + Alpaca/Tiingo free EOD prices + `fja05680/sp500` constituent history + FINRA short
interest + FRED `VIXCLS`. **What you lose:** delisting-complete prices before your own collection
start, and reliable delisted fundamentals. Consequence: every cohort older than your own data
history is capped at `archival_reconstructed` — which, given Spec N's tiering, is at least *honest*.
This is a genuinely viable v1: ship the engine on free data with the tier telling the truth, and buy
Sharadar when a cohort you actually care about gets refused.

---

# A. Slot-by-slot scoring

## Slot 1 — Backtest / event-study engine

Adoption criteria (README §5): must reproduce `backtest/simulator.py` exit semantics exactly, then
PIT correctness, speed, dependency weight.

### The criterion is decisive and rules out all five candidates

I read `backtest/simulator.py`. Its semantics are: entry at the **open of the bar after the signal**;
stop checked **before** targets on the same bar (pessimistic); a bar that **gaps through** the stop
fills at the worse open; **half the position** exits at T1 with the stop unchanged on the remainder;
T2 for the rest; time exit counted in **calendar** days from the fill bar; adverse slippage on
**every** leg including entry. It also returns MFE/MAE and a `rule_fired` taxonomy.

No general engine reproduces that combination without you writing the strategy code that implements
it — at which point the engine has bought you an execution loop you already have, plus a dependency.
And Spec N §10 requires `test_policy_matches_simulator` to hold **bit for bit**. Adopting an engine
would mean re-deriving the semantics inside it and then proving equality — strictly more work than
keeping 219 lines of pure-math Python with no network and no DB.

**Recommendation: keep and extend.** Extend it with (a) a vectorised path for cohort-scale replay
(pure NumPy over a 3-D bar array; the logic is a scan, it vectorises), (b) an explicit
`ExecutionPolicy` dataclass so Spec Q's named policies are data not arguments, (c) short-side and
fractional-share handling if you ever need them.

### Candidate scoring

| Candidate | License | Maintenance | Python | Cost | Verdict |
|---|---|---|---|---|---|
| **`backtest/simulator.py`** (incumbent) | yours | yours | any | $0 | **Adopt.** 219 lines, no deps, semantics already correct and documented. |
| **`vectorbt` (OSS)** | Apache-2.0 **with Commons Clause** [V] — *not* OSI open source; you may not sell a product that is primarily this software | Free edition is in **maintenance mode**: bug fixes and new-Python support only, new features are PRO-only [S] | 3.9+ [T] | $0 | **Runner-up, narrow use.** Excellent for vectorised forward-return matrices over a cohort. Do **not** use it for the policy-simulated leg. |
| **`vectorbt PRO`** | proprietary, **personal use only** [S] | active | — | ~$20/mo annual, ~$25/mo monthly [S] | **Reject.** Personal-use licence is awkward for a repo with live-money code; cost buys features you do not need. |
| **`nautilus_trader`** | **LGPL-3.0-only** [V] | Very active; v2 RC (`2.0.0rcN`), release 2026-09-02 [V] | 3.12–3.14 [V] | $0 | **Reject for this use.** 28.4k stars, Rust core, nanosecond order books — engineered for HFT/live venue simulation. Massive dependency weight for daily-bar swing events, and v1→v2 is a breaking migration in flight right now. |
| **`backtrader`** | GPL-3.0 [T] | **Inactive.** No PyPI release in 12 months; original repo effectively archive-mode; community fork `backtrader2` [S] | ≤3.9 comfortably [S] | $0 | **Reject.** Dead-ish, and you would be adding a dependency you then maintain. |
| **`zipline-reloaded`** | Apache-2.0 [V] | Active — 3.1.1 released 2025 alongside pyfolio 0.9.9 / alphalens 0.4.5 for Py3.13/NumPy2 [S] | 3.9+ [V] | $0 | **Reject as engine, consider as ideas source.** Its *Pipeline* API and bundle/PIT model are worth reading; the engine itself imposes a bundle ingestion pipeline you do not want on Railway. |
| **`bt`** | MIT [T] | low activity [T] | — | $0 | Reject: allocation/rebalancing framework, wrong shape for event replay. |
| **`pysystemtrade`** | GPL-3.0 [V] | Active, maintainer Andy Geach since 2024, org moved to `pst-group` [V] | — | $0 | Reject as engine (futures-first, IB-coupled). **Borrow the ideas** — see §B. |
| **QuantConnect Lean** | Apache-2.0 [V], 21.5k stars | Active [V] | — | free engine; cloud/data paid [V] | **Reject.** C#-first, cloud-coupled, and its data licensing is its own product. |
| **`eventstudy` (LemaireJean-Baptiste)** | MIT [T] | **Inactive**, last release 0.1a11, alpha [S] | — | $0 | **Reject.** Alpha, abandoned, and implements exactly the naive independent-events inference Spec N §6.2 exists to prevent. |
| **`alphalens-reloaded` / `pyfolio-reloaded`** | Apache-2.0 [S] | Active, 0.4.6 / 0.9.9 in 2025 [S] | 3.13 OK [S] | $0 | **Adopt optionally, for rendering only.** Never for inference — pyfolio's Sharpe/tearsheet stats assume i.i.d. returns. |
| **`quantstats`** | Apache-2.0 [T] | Original: sporadic (0.0.81 in 2026 [S]). Fork **`quantstats-lumi`** 1.1.5, Jun 2026 [S] | — | $0 | Use the fork if you use it at all. Reporting only. |

**What would change the recommendation:** if you ever need intraday fills or multi-leg
options, `nautilus_trader` becomes the serious answer and the whole calculus flips.

---

## Slot 2 — Point-in-time equity prices + corporate actions with delisted coverage

Criteria: adjustment provenance and delisted coverage first, cost second.

**The test that matters:** does the vendor (a) retain delisted tickers with full history, (b) return
**both** raw and adjusted series, (c) publish **split and dividend factors with ex-dates** so you can
reconstruct either from the other, and (d) supply a delisting return (or at least a delisting reason)
so you can apply the literature's terminal-return convention?

| Vendor | Delisted retained | Raw + adjusted | Split/div factors w/ ex-date | Delisting reason/return | Cost | Verdict |
|---|---|---|---|---|---|---|
| **Sharadar SEP + ACTIONS + TICKERS** (Nasdaq Data Link) | **Yes** — 25,000+ tickers, ~10k active + ~15k delisted, back to the 1990s, described as "nearly completely free of survivorship bias" [S] | SEP is adjusted; **ACTIONS** table carries the corporate-action events [S] | via ACTIONS [S] | TICKERS carries listing/delisting dates [S] | ~$69/mo full history, ~$29/mo 5-year [S] | **Recommend.** Flat-file/bulk, cron-friendly, survivorship-free by design, and it comes bundled with SF1 (slot 3) and S&P 500 constituent history (slot: PIT universe). One purchase closes three slots. |
| **Polygon / "Massive"** | **Yes** — "delisted tickers stay in the universe with their full history", tick data back to 2003, corporate actions back to 2008 [S] | Yes — `adjusted=false` on aggregates returns split-unadjusted [S] | splits and dividends endpoints with `ex_dividend_date`, declaration/record/pay dates [S] | not published as a return | Basic $0 (EOD), Starter $29 (**5 yr history**), Developer $79 (10 yr), Advanced $199 (20+ yr) [S] | **Runner-up.** Two problems: **(1) no dividend-adjusted series — "Polygon.io does not currently provide dividend-adjusted data"** [S], so total-return math is on you; **(2) history depth is paywalled by tier** — a 20-year cohort needs the $199 plan. |
| **Databento** | **Yes** — corporate actions dataset covers 310,000+ listed **and delisted** securities across 215 exchanges, continuous records through delist/relist, 60+ event types [S] | yes (raw ticks; you build bars) | yes, corporate actions + reference API [S] | listing/delisting events included [S] | usage-based + subscriptions; $125 free credit [S]; exact $/GB not verifiable here | **Consider only if you need microstructure.** Superb reference/corp-action data, but it is a tick-data product; you'd be paying for depth you don't use at a daily-bar horizon. |
| **Tiingo** | Partial [T] — 80,000+ assets, EOD back to 1962 [S]; delisted retention not explicitly documented in what I could reach | adjusted + `adjClose`/`adjOpen` + `divCash`/`splitFactor` per row [T] | yes, per-row factors [T] | no | Free tier: 500 unique symbols/mo, 50 req/hr, 1,000 req/day, 30+ yr price history [S]; Power $30/mo personal; Commercial $50/mo [S] | **Cheap fallback.** Per-row `splitFactor`/`divCash` is genuinely good adjustment provenance. The free tier's 500-symbols/month cap kills cohort work; $30/mo is the real entry. |
| **EODHD** | **Yes** — delisted symbols listable and queryable for EOD prices, fundamentals, dividends, splits, in **any** package; S&P 500 membership record continuous from **2012-04-04** [S] | yes [S] | yes [S] | delisting listed | ~$20–80/mo [S] (All-World ~$19.99 quoted from a third-party repo, not the vendor page) | **Solid cheap alternative to Sharadar.** The 2012 constituent-history start is the limitation for long-history PIT universe. |
| **Norgate Data** | **Yes** — complete delisted archive + point-in-time S&P 500 / Russell 3000 membership flags [S] | yes [T] | yes [T] | yes [T] | US Stocks Gold: **$198/6mo, $360/12mo** [S] | **REJECT for this architecture.** The `norgatedata` Python package **requires the Norgate Data Updater (NDU), a Windows-only desktop application, to be running** [S]. There is no Linux/cloud path except a Windows VM. That is disqualifying for a Railway-hosted service. |
| **FMP (incumbent)** | Delisted-companies endpoint exists [S] | adjusted + unadjusted endpoints [T] | yes [T] | delisting date/reason [S] | Free 250 calls/day [T]; Starter ~$22–29 [T]; Premium ~$69–79 [T] | **Keep as the incumbent through migration, then demote to exploratory tier.** FMP's historical revisions are not versioned; you cannot establish `known_at_utc`. |
| **Free sources with delisted names** | None reliable. `yfinance` **drops delisted tickers** and its access violates Yahoo's ToS [S]. Stooq/Alpha Vantage do not retain delistings [T]. | | | | $0 | **There is no free delisting-complete US EOD source.** This is the single thing worth paying for. |

**Recommendation:** Sharadar bundle. **Runner-up:** EODHD (cheaper) or Polygon Advanced (if you also
want intraday). **What would change it:** if Bryan only ever cares about the last 5 years and liquid
large caps, Tiingo Power at $30/mo plus SEC data is sufficient and Sharadar is over-buying.

---

## Slot 3 — Point-in-time fundamentals with availability timestamps

Criterion: `known_at_utc` present, or the source is exploratory-only.

### The free option is better than the spec assumes

The SEC's **XBRL `companyfacts` API** returns, for every reported fact:
`end`, `val`, **`accn`** (accession number), `fy`, `fp`, `form`, **`filed`**, and `frame` [S].
The **`submissions`** JSON carries **`acceptanceDateTime`** per filing [S]. Joining
`companyfacts.accn` → `submissions.acceptanceDateTime` yields a genuine
**`known_at_utc` to the second**, sourced from the regulator, for free, with no key.
Rate limit is 10 requests/second with a User-Agent identifying you [S].

This is strictly the highest-quality PIT fundamentals source available to an individual, and it is
free. It also handles restatements natively: a restated figure arrives as a *new fact with a new
`accn` and later `filed`*, so `known_at_utc <= t` filtering gives you the as-reported number
automatically. The **SEC Financial Statement Data Sets** (quarterly bulk ZIPs, `num.txt`/`sub.txt`)
are the bulk-load path for the same data [S].

**Limits:** XBRL coverage starts ~2009 for large filers (smaller filers phase in later); tags are
company-chosen, so normalisation (`Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`)
is real work. `edgartools` already does a lot of this normalisation [V].

| Candidate | PIT? | Cost | Verdict |
|---|---|---|---|
| **SEC XBRL companyfacts + submissions** | **Yes, to the second**, via `accn`→`acceptanceDateTime` [S] | $0 | **Recommend as the `clean_pit` source.** |
| **Sharadar SF1** | **Yes** — "point-in-time and as-reported, stamping every record with the actual filing date (`datekey`) alongside the reporting period, so an as-of query returns only what was public on that date"; retains delisted companies [S] | in the ~$69/mo bundle [S] | **Recommend as the convenience layer** — normalised, ~150 metrics, no XBRL wrangling. `datekey` is date-resolution, not timestamp; treat as end-of-day. |
| **S&P Compustat Point-in-Time** | Yes, the gold standard [T] | institutional (WRDS); not realistically available to an individual [T] | **Reject** — not purchasable at this scale. |
| **FMP** | **No.** As-reported endpoints exist but there is no availability timestamp and restatements overwrite [S/T] | $22–79/mo [T] | **Exploratory tier only** — exactly as Spec N §4.1 requires. |
| **EODHD fundamentals** | Partial; has `filing_date` on some statements [T] | $20–80/mo [S] | Middle option; verify `filing_date` coverage before relying on it. |
| **`edgartools` financials** | Inherits SEC PIT-ness | $0 | **Use it** as the client for the SEC path. |

**What would change the recommendation:** if you need pre-2009 fundamentals (XBRL doesn't exist),
Sharadar SF1 becomes mandatory rather than convenient.

---

## Slot 4 — SEC filings + entity resolution

| Candidate | License | Maintenance | Capabilities | Cost |
|---|---|---|---|---|
| **`edgartools`** | **MIT** [V] | **v5.56.0, released 2026-09-02** [V]; 2.7k stars [V] | 13F institutional holdings; **Form 4 insider transactions with standardized codes**; XBRL financials + CompanyFacts; 13D/G, Forms 3/5; 10-K/Q, 8-K, DEF 14A, N-CSR/N-PORT, Form D, Form 144; **full-text search**; ticker/CIK lookup; **rate-limit aware**; caching; ships its own MCP server [V] | $0, email identifier only [V] |
| Raw EDGAR (`companyfacts`, `frames`, `submissions`, `company_tickers.json`, full-text search, bulk data sets) | public domain | SEC | Everything; you write the parsing | $0 |
| **`sec-edgar-api`** (jadchaar) | MIT [T] | active [S] | thin wrapper over the JSON APIs [S] | $0 |
| **`sec-api.io`** | commercial | active | 13F since 1998 with CUSIP/ticker/value/shares; Forms 3/4/5; full-text search with boolean phrases; XBRL→JSON; real-time streaming [S] | **Personal $49/mo annual ($55 monthly); Business $199/mo; free tier is 100 calls *lifetime*** [S] |
| **Robinhood MCP `get_sec_filing_facts`** | n/a | n/a | Present on the *unofficial* server surface; the **official** Robinhood MCP tool list does not appear to include filings [S] | n/a |
| **OpenFIGI** (CUSIP→ticker) | free API | Bloomberg-run | **Accepts CUSIP as input**; 100 items/request with a key (5 without); "free to use without daily, weekly or monthly limitations"; bulk mapping quoted at 25,000 jobs/min [S]. **Caveat: OpenFIGI will not *return* CUSIP/ISIN/SEDOL** as output due to licensing — but you only need the reverse direction [S] | $0 |
| **HuggingFace / Kaggle 13F snapshots** | varies | community | e.g. `JamesFromAlphasmo/13f-institutional-holdings-sec-edgar`, 13k+ managers, mirrored on Kaggle [S] | $0 |
| **SEC Form 13F Data Sets / Insider Transactions Data Sets** | public domain | quarterly [S] | Flattened XML→TSV of the as-filed submissions; insider sets cover Forms 3/4/5 [S] | $0 |

**Recommendation: `edgartools` + raw EDGAR bulk data sets + OpenFIGI.** Runner-up `sec-api.io`
Personal at $49/mo — but only buy it if you find yourself spending more than an hour a month on
parser breakage. **What would change it:** heavy full-text search over 20 years of filing exhibits;
EDGAR FTS only covers 2001+ and is rate-limited, and sec-api.io's boolean phrase search is better.

**Entity-resolution gaps the spec should name explicitly:**
- `company_tickers.json` is a *current* snapshot. **CIK↔ticker history** must be reconstructed from
  the `submissions` JSON's `formerNames` array and the ticker/exchange history — plan for this; it
  is the "unglamorous part" Spec O §3.2 correctly identifies and is genuinely a week of work.
- 13F CUSIPs include share classes and ADRs that map to multiple FIGIs; Spec O's `test_unmapped_cusip_surfaced`
  is the right test but you also need `test_ambiguous_cusip_surfaced`.
- Form 4 transaction codes: **P** (open-market purchase) and **S** (sale) are the informative ones;
  **A** (award/grant), **M** (option exercise), **F** (tax withholding), **G** (gift) must never be
  pooled with them. Also flag `10b5-1` plan sales (a checkbox on the form since 2023). `edgartools`
  exposes the codes [V] — the *discipline* is yours.

---

## Slot 5 — Point-in-time macro

FRED/ALFRED is free, has an instant API key, and raises your rate limit from 30 to **120 requests
per minute** [S]. ALFRED's model is: every observation carries `date`, `realtime_start`,
`realtime_end`; you can request a `vintage_dates` list or a real-time period [S].

| Library | Maintenance | ALFRED support | Verdict |
|---|---|---|---|
| **`fredapi`** (mortada) | **Rated Inactive** by PyPI-cadence analysis [S], but still packaged (Anthropic/conda updated Sep 2025 [S]). README last substantively updated 2014 [V]. | **Yes:** `get_series_first_release()`, `get_series_latest_release()`, `get_series_as_of_date()`, `get_series_all_releases()` [V] | **Keep** — already in `requirements.txt`, does exactly what Spec O §4.1 needs. **Pin it and wrap it** behind `data/macro_data.py` so a break is a one-file fix. |
| **`pyfredapi`** (gw-moore) | Active — 0.10.2 uploaded Jul 2025 [S] | Covers **all** FRED endpoints incl. ALFRED, returns pandas or JSON [S] | **Runner-up.** Switch if `fredapi` breaks on a new pandas. |
| **`full_fred`** | Updated Jun 2025 [S] | Full endpoint coverage [S] | Third option. |
| **`fedfred`** | Active [S] | Modern client [S] | Unassessed; note as an option. |
| Treasury `FiscalData`/`Treasury.gov` par yield curve | official, free | daily, as-published | Use for the yield-curve regime input — it is not revised, so it is trivially PIT. |

**The gap the spec must acknowledge:** ALFRED vintages are **dated, not timestamped**. There is no
"CPI was released at 08:30:00 ET" in ALFRED. `fred/releases/dates` gives release dates, not times.
So `known_at_utc` for a macro fact is at best `vintage_date @ 23:59:59 UTC` unless you separately
scrape release schedules. **Recommendation:** define macro `known_at_utc` as the *close of the
vintage date*, mark it `precision='day'`, and add a `precision` field to `source_observations`.
Otherwise Spec O §4.1's `test_macro_vintage_absent_before_release` will pass while intraday cohorts
quietly inherit lookahead.

**Packaged regime datasets:** NBER recession dates are on FRED as `USREC`/`USRECD` (free, but
**announced with a long lag and revised** — using `USREC` as a PIT regime label is textbook
lookahead; use the ALFRED vintage of it or don't use it). `VIXCLS` on FRED, 1990-01-02 to present,
free [S]. There is no credible free "regime label" dataset; build your own deterministic one, which
is what Spec O §4.2 already says.

---

## Slot 6 — News with timestamps

| Source | Timestamps | History | Cost | Verdict |
|---|---|---|---|---|
| **Alpaca News API** | Benzinga-sourced publisher timestamps; ~130+ articles/day [S] | **back to 2015** [S] | **$0** with your existing Alpaca account; 200 req/min on the free plan [S] | **Recommend.** Free, already authenticated, real publisher timestamps, symbol-tagged. |
| **Tiingo News** | publisher timestamps + Tiingo crawl timestamp [T] | "70M+ articles over 20+ years" [S] | bundled with Power $30/mo [S] | **Runner-up**, and free if you're already buying Tiingo for prices. |
| **Benzinga direct** | native | long | enterprise pricing [T] | Reject — you get it via Alpaca for free. |
| **Polygon news** | publisher timestamps | shallower [T] | bundled with the price plan | Only if you're already on Polygon. |
| **Finnhub news** | publisher timestamps | free tier limited [T] | already in `requirements.txt` | Keep as a cross-check source for the earliest-timestamp rule. |
| **GDELT** | ingest timestamps (15-min updates), **not publisher timestamps** | 2015+ (GDELT 2.0) | $0, BigQuery | **Use only for volume/novelty context, never as `known_at_utc`.** GDELT stamps when *it* saw the article. |
| **FNSPID** (HuggingFace `Zihan1004/FNSPID`) | timestamped | **15.7M news + 29.7M prices, 4,775 S&P 500 companies, 1999–2023** [S] | $0 | **Excellent for backfilling the historical news plane.** Licence not stated on the pages I could reach — **check before committing anything derived from it to a public repo.** |
| **Incumbent Gemini-search + Firecrawl** | scrape time only | n/a | inference cost | **Demote.** Cannot establish publication time; per Spec O §5.1 these are `replay_eligible=false` by construction. Keep for live research, remove from any cohort path. |

**Dedup/clustering:** the right stack for a solo operator is **(1) exact URL + canonical-URL match,
(2) MinHash/LSH on title+lead shingles (`datasketch`, MIT) for near-duplicate wire pickups, (3)
optional embedding cosine for paraphrase clusters.** simhash and MinHash both work; MinHash-LSH scales
better and is deterministic, which matters because Spec O §5.2 wants novelty computed from structured
facts, not model judgement. Cluster `known_at_utc` = **min** publisher timestamp across members, which
is exactly what Spec O §5.1 says.

---

## Slot 7 — Statistical rigor libraries

### The important finding

**`mlfinlab` is not open source.** Its licence is all-rights-reserved; commercial use requires a
licence purchased from Hudson & Thames, and the company has moved to an open-core model [S]. Do not
put it in `requirements.txt`. The community response has been forks and reimplementations
(`mlfinpy`, `purgedcv`, `ml4t-diagnostic`) [S].

| Library | License | Maintenance | Provides | Verdict |
|---|---|---|---|---|
| **`arch`** (bashtage) | NCSA/BSD-ish [T]; 1.6k stars [V] | active [V] | **IID / Stationary / Circular-block / Moving-block bootstrap**; **`optimal_block_length`** implementing Politis–White (2004) with the Patton–Politis–White (2009) correction, returning `b_sb` and `b_cb` [S]; **SPA (Reality Check), StepM, Model Confidence Set**; unit-root tests; long-run covariance [V] | **Adopt.** This alone satisfies Spec N §6.1 *and* gives you a principled block length instead of a hand-picked one. Python 3.9+ [V]. |
| **`statsmodels`** | BSD-3 | active | `OLS(...).fit(cov_type='cluster', cov_kwds={'groups': ...})`; `get_robustcov_results` with `'cluster'`, `'hac-panel'`, `'hac-groupsum'` (Driscoll–Kraay); `sandwich_covariance.cov_cluster_2groups` for **two-way clustering** [S] | **Adopt.** Two-way clustering (event date × ticker) is exactly Spec N §6.2's requirement and it is a one-liner. |
| **`purgedcv`** (eslazarev) | **MIT** [V] | 120 commits, recent [V]; 31 stars | Purging, embargo (time/count/fraction), walk-forward CV, purged K-fold incl. group-aware and **combinatorial**, **Probabilistic Sharpe Ratio, Deflated Sharpe Ratio, PBO**; fully sklearn-compatible [V]; Py 3.10–3.14 [V] | **Adopt for Spec Q**, or vendor the ~200 lines. Small project — vendoring with attribution is defensible. |
| **`ml4t-diagnostic`** | **MIT** [V], 30 stars | active, CI-tested examples [V] | **Deflated Sharpe with correlation-adjusted K_eff**, PBO, **Benjamini–Hochberg FDR**, CPCV, **NYSE/CME calendar-aware splitting**, HAC-adjusted information coefficient [V]; Py 3.12–3.14 [V] | **Strong runner-up to `purgedcv`.** The correlation-adjusted effective-trials count is more honest than naive N for your `trials_against_this_pattern`. |
| **`skfolio`** | **BSD-3** [V], 2.4k stars, Py 3.10+ [V] | active [V] | Portfolio optimisation, CVaR/EVaR/CDaR, HRP, **Combinatorial Purged Cross-Validation**, entropy pooling, copula stress tests [V] | **Not needed for Spec N.** Relevant only if you later do portfolio construction. Good CPCV implementation if you want one with a real maintainer base. |
| **`mlfinlab`** | **all rights reserved, commercial licence required** [S] | commercial | — | **Reject.** |
| **`quantstats` / `quantstats-lumi`** | Apache-2.0 [T] | fork active (1.1.5, Jun 2026) [S] | tearsheets | Reporting only; its stats assume i.i.d. |
| **Event-study packages** (`eventstudy`, `myeventstudy`, `event-study-toolkit`, `paneleventstudy`) | mixed | `eventstudy` **inactive since 2019, alpha** [S]; others tiny | CAR/CAAR with naive t-tests | **Reject all.** None implements calendar-time portfolios or cross-sectional-correlation-robust inference — the two things Spec N §6.2 requires. Hand-roll; it is ~300 lines. |

**Recommendation:** `arch` + `statsmodels` + ~200 vendored lines (Wilson interval, empirical-Bayes
shrinkage, deflated significance, calendar-time portfolio regression). Runner-up: add `purgedcv` when
Spec Q needs CPCV. **What would change it:** if Spec Q grows into real ML factor modelling, adopt
`skfolio` + `purgedcv` properly rather than vendoring.

---

## Slot 8 — Remote MCP transport / auth

### The 2026 landscape, verified

- **Official `mcp` Python SDK:** latest **2.1.1, released 2026-08-25**; Python 3.10+; MIT; 24.2k
  stars. **v2 renamed the bundled `FastMCP` class to `MCPServer`** and is now the default install.
  v1.x lives on the `v1.x` branch with critical fixes only (latest 1.29.1, 2026-08-24), and the
  SDK's own guidance is to **pin `mcp>=1.28,<2` until you migrate** [V]. Supports stdio, **streamable
  HTTP (the recommended deployment transport)**, and SSE [V].
- **`fastmcp` (PrefectHQ):** **4.0.3 released 2026-09-05** (today); Apache-2.0; Python ≥3.10; 27.5k
  stars [V]. Timeline: 3.0 announced Jan 2026, GA Feb 2026 [S]; **4.0.0 GA 2026-08-31** aligned with
  SDK v2, removing server-initiated sampling and roots, deprecating `ctx.elicit()`, removing all v3
  deprecated APIs, moving background tasks to a separate `fastmcp-tasks` package, and switching MCP
  model fields to snake_case [V]. Auth: identity assertion (SEP-990), M2M client auth, scope step-up
  challenges, OAuth DCR `application_type`, multiple providers incl. Auth0; proxies strip cookies and
  connection-owned headers at trust boundaries [V].
- **Spec revision 2026-07-28** made the deprecation of HTTP+SSE official with a year-long offramp;
  **new servers should use streamable HTTP** [S].

### Client attachment reality (this is what actually decides it)

- **Claude Code CLI:** accepts a **static bearer token** —
  `claude mcp add my-server --transport http --header "Authorization: Bearer ${TOKEN}" https://.../mcp` [S].
  This is the simplest thing that works and matches Spec K §4.1's owner-token model exactly.
- **Codex CLI:** streamable HTTP with `url` + `bearer_token_env_var`, sent as `Authorization: Bearer <token>`;
  `codex mcp add name --url https://.../mcp --bearer-token-env-var TOKEN`; config in `~/.codex/config.toml`
  or `.codex/config.toml` [S].
- **claude.ai / Claude Desktop custom connectors:** historically **OAuth-only**, with static bearer /
  API-key headers arriving **in beta** where an admin enters the credential once [S]. There are open
  bug reports of the claude.ai connector completing OAuth and then not sending the bearer token,
  while Claude Code CLI works against the same server [S].

**Implication for Spec K §2 ("the same three questions answer identically from Claude Code, a cloud
session, and Codex"): a static bearer token gets you Claude Code + Codex today. A Claude *cloud
session* / claude.ai connector may require OAuth.** Plan for that: either accept
"bearer for CLI clients, connector added later", or implement OAuth from the start — which is the one
concrete argument for `fastmcp`, whose auth providers do this for you.

**Recommendation: official `mcp` SDK v2 (`MCPServer`) mounted into the existing FastAPI app,
streamable HTTP, static bearer with hashed+scoped tokens.** You already depend on `mcp>=1.27.2`, the
surface you need is small (15 tools, no sampling, no elicitation, no proxying), and one dependency is
one dependency. **Pin `mcp>=1.29,<2` now** and migrate to v2 in a dedicated PR with the rename.

**Runner-up: `fastmcp` 4.x** — take it if and only if you decide you need OAuth for claude.ai. Its
composition/proxying/OpenAPI features are worth nothing to you, and it has shipped **two major
versions in seven months**, which for a solo operator is a recurring tax.

**Railway deployment:** Railway publishes both a **FastMCP deploy template** and a
**"Build and Deploy Your Own MCP Server" guide**, with a reference architecture of a Python 3.13
uvicorn container + Postgres over the private network + Redis for stream resumability (which lets the
server close idle connections behind load balancers and lets the client resume) [S]. Hobby is $5/mo
plus usage; a single MCP service runs comfortably there [S]. **Take the Redis-for-resumability idea
seriously** — a `compare_setups` call that takes 30 seconds behind Railway's proxy is exactly the case
it exists for.

---

# B. Open-source projects to learn from

## Worth borrowing, ranked by what you'd actually take

| Project | Stars / License | Borrow this | Avoid this |
|---|---|---|---|
| **freqtrade** | 54k / GPL-3 [V] | **`lookahead-analysis` and `recursive-analysis` commands** — automated detection of look-ahead bias and self-referential indicator bugs by re-running with truncated data [V]. This is the single most transferable discipline in the entire survey and maps directly onto Spec N §2 failure modes 2 and 11. Also: dry-run as a first-class mode (= your shadow tier), and strategy/config separation. | Its hyperopt culture — thousands of parameter trials with weak multiple-testing control is the thing Spec N §7 is defending against. |
| **`edgartools`** | 2.7k / MIT [V] | The whole library; plus its typed-object model (a filing is an object with `.financials`, `.insider_transactions`, etc.) as a template for your `source_observations` producers. | Nothing significant. |
| **`arch`** | 1.6k / BSD [V] | `optimal_block_length`, SPA/StepM/MCS. | — |
| **beancount / fava** | 3k+ / GPL [S] | **The booking-method model**: booking is a per-account setting from `{STRICT, FIFO, LIFO, AVERAGE, NONE}`, with STRICT forcing explicit lot selection [S]. That is precisely the right shape for `tax_lots` in Spec L §3, and it gives you a vocabulary rather than a boolean. Also: plugins that assert invariants over the whole ledger after parsing — the same pattern as your import-graph tests. | Adopting beancount itself. You need lot tracking, not double-entry accounting. |
| **Ghostfolio** | 9.2k / AGPLv3 [V] | The **activity** model: one append-only table of typed events (buy/sell/dividend/fee/interest/liability) with date, quantity, unit price, currency, **and data source**, from which positions are *derived* rather than stored [V]. Compare with Spec L's `holdings` snapshot-by-append — the activity model answers "why do I hold this" for free. Consider storing both. | The stack (NestJS/Angular/Prisma) and the AGPL. Read it, don't import it. |
| **pysystemtrade** | 3.5k / GPL-3 [V] | Carver's discipline: **forecast scaling, volatility targeting, and capital management as separate, testable layers**; position size derived from risk not conviction. Spec L/Q have no position-sizing model at all (see §G3). | Its futures/IB coupling and its scale. |
| **Rotki** | 4k / AGPLv3 [V] | Customisable accounting settings for P&L reports; the self-hosted-privacy posture. | Crypto-first data model. |
| **Microsoft Qlib** | 48.3k / MIT [V] | Its **point-in-time database + expression caching engine** design — the idea that PIT-ness is a property of the storage layer, not of each query [V]. Directly relevant to `source_observations`. | The whole framework. Last tagged release is **v0.9.0, 2022-12-09** [V] despite ongoing commits — a dependency with a 4-year-old release is a liability. Also China-market-first. |
| **QuantConnect Lean** | 21.5k / Apache-2 [V] | Its universe-selection API (coarse → fine, evaluated per day from stored data) as the model for `universe_membership`. | C#, cloud coupling, data licensing. |
| **NautilusTrader** | 28.4k / LGPL-3 [V] | The **capability/venue-model separation** and deterministic event ordering. | Adopting it. LGPL + Rust toolchain + v1→v2 migration in flight. |
| **OpenBB** | 72.7k / **AGPLv3** [V] | The **provider-abstraction model** (one interface, many vendors, "connect once, consume everywhere") — validation for your `data/base_adapter.py`. And **`openbb-mcp-server`** (last release 2026-05-26 [S]) has a genuinely good idea: **progressive tool disclosure** — the server exposes discovery tools first and agents *activate* only the tool categories they need, per session, so the initial tool list stays small [S]. With 15 tools you don't need it yet; at 40 you will. | **The AGPLv3.** If you import `openbb` into a network-served workspace API, AGPL §13 arguably obliges you to offer the whole service's source. For a public personal repo that may be fine; know that you're choosing it. |
| **TradingAgents** (TauricResearch) | 102.6k / Apache-2 [V] | Its **honesty**, which is unusual: the README states results are non-deterministic, "two runs of the same ticker and date can differ", and "backtest results are not guaranteed to match any published figure"; v0.4.0 (Aug 2026) added **look-ahead filtering** and "company identity and price grounding … to prevent hallucination" [V]. The bull/bear researcher debate structure is the same idea as your `thesis-critic`. | The architecture. It is exactly the "custom agent framework" Spec P §2 correctly refuses to build, and its outputs are LLM-generated numbers — the thing Spec N §9 forbids. |
| **virattt/ai-hedge-fund** | 63.3k / MIT [V] | Nothing structural. Note it as the popular-but-shallow reference point: 63k stars, explicitly "educational … not intended for real trading", does not execute [V]. Useful as evidence that *stars ≠ rigour* when Bryan sees these in a feed. | Everything. |
| **FinRL / FinGPT** | large / MIT [T] | Nothing for this system. RL on financial time series with the sample sizes available to you is the multiple-testing problem in its most concentrated form. | Adopting either. |
| **Jesse, vnpy, `bt`** | — | Nothing not better covered above. | — |
| **`fja05680/sp500`** | MIT [V] | The dataset itself (see §C). | Treating it as authoritative — it's Wikipedia-derived and manually verified [V]. |
| **Obsidian investing vaults** | small | The **file-per-thesis with YAML front-matter** convention, which is what Spec M §5 already does. One 2026 vault explicitly pairs an Obsidian investment journal with Claude as "senior analyst", with earnings ingestion, thesis trackers, risk registers and decision journals [S] — small (13 stars) but confirms the shape is right. | Building a vault instead of a database. |
| **Academic event-study repos** (`eventstudy` py, R `EventStudy`, `crseEventStudy`) | mixed | The R `crseEventStudy` package implements **cross-sectional-correlation-robust** abnormal-return tests [S] — read its methodology, port the test. | The Python ones: unmaintained and statistically naive. |

## What best-in-class deliberately does NOT do

- **No serious system stores LLM output as a number.** Spec N §9 is correct and rare.
- **Neither Lean, zipline, nor freqtrade builds an agent orchestration layer.** Spec P §2 is right.
- **Nobody maintains two instruction files by hand and tests them for agreement** (see §G1 item 1).
- **Mature backtest engines do not ship "AI strategy generation".** The projects that do (ai-hedge-fund,
  FinGPT) are educational demos with disclaimers.

---

# C. Datasets

| Dataset | What it gives | Coverage | PIT? | Cost | Licence / URL |
|---|---|---|---|---|---|
| **Sharadar SEP** | Daily OHLCV, active **and delisted** | 25,000+ US tickers, 10k active / 15k delisted, from the 1990s [S] | prices are PIT by construction | in ~$69/mo bundle [S] | commercial, no redistribution — `data.nasdaq.com/databases/SEP` |
| **Sharadar SF1** | Fundamentals with **`datekey`** (filing date) | ~20 yrs, delisted retained [S] | **Yes** | bundle | commercial |
| **Sharadar ACTIONS / TICKERS / SP500** | Corporate actions; listing/delisting dates; **S&P 500 constituent history** | [S] | yes | bundle | commercial |
| **SEC XBRL `companyfacts` + `submissions`** | Every reported fact with `accn`, `filed`, `form`; **`acceptanceDateTime`** per filing | ~2009+ (XBRL phase-in), 10,000+ companies [S] | **Yes, to the second** | **$0**, no key, 10 req/s w/ User-Agent [S] | public domain — `sec.gov/search-filings/edgar-application-programming-interfaces` |
| **SEC Financial Statement Data Sets** | Quarterly bulk `sub.txt`/`num.txt` of as-filed XBRL | 2009+ | Yes (`filed`) | $0 | public domain |
| **SEC Form 13F Data Sets** | Flattened as-filed 13F information tables, quarterly [S] | 2013+ | Yes (acceptance) | $0 | public domain — `sec.gov/data-research/sec-markets-data/form-13f-data-sets` |
| **SEC Insider Transactions Data Sets** | Flattened Forms 3/4/5, quarterly [S] | 2006+ | Yes | $0 | public domain + `sec.gov/files/insider_transactions_readme.pdf` |
| **HF `JamesFromAlphasmo/13f-institutional-holdings-sec-edgar`** | Structured 13F for 13,000+ managers, mirrored to Kaggle [S] | — | derived | $0 | check dataset card |
| **FRED / ALFRED** | ~800k series + **all vintages** (`realtime_start`/`realtime_end`) | 1900s–present | **Yes, day-resolution** | $0; 120 req/min with key [S] | free for any use — `fred.stlouisfed.org/docs/api/fred/` |
| **FRED `VIXCLS`** | VIX daily close | 1990-01-02 → present [S] | yes | $0 | free |
| **CBOE historical VIX / volatility indices** | VIX + VIX term structure, VVIX etc. | 1990+ | yes | $0 | `cboe.com/tradable-products/vix/vix-historical-data` |
| **FINRA Equity Short Interest** | Bi-monthly short interest per security | 5 rolling years in the interactive grid, 1 year online + archive downloads [S] | reported with a settlement-date lag — **use the publication date as `known_at`** | $0 | `finra.org/finra-data/browse-catalog/equity-short-interest` |
| **FNSPID** (`Zihan1004/FNSPID`, arXiv 2402.06698) | **15.7M timestamped news + 29.7M price records**, 4,775 S&P 500 companies, **1999–2023** [S] | S&P 500 only | timestamps present; sourced from 4 news sites | $0 | **licence not stated on reachable pages — verify** |
| **Alpaca News (Benzinga)** | Symbol-tagged news with publisher timestamps, ~130+/day [S] | **2015 → present** [S] | yes | $0 with account [S] | vendor terms; **do not redistribute** |
| **`fja05680/sp500`** | S&P 500 historical components & changes CSV | **1996 → present**, updated every couple of months; Wikipedia-derived, originally from Clenow's *Trading Evolved* [V] | date-stamped membership | $0 | **MIT** [V] — `github.com/fja05680/sp500` |
| **`hanshof/sp500_constituents`** | Same idea, per-date constituent lists | 1996-01-02 → present [S] | yes | $0 | check repo |
| **EODHD index constituents** | S&P 500 membership changes | **continuous from 2012-04-04** [S] | yes | ~$20–80/mo [S] | commercial |
| **Earnings dates + surprises** | FMP `earnings-calendar` / `earnings-surprises`; EODHD earnings API; Sharadar `EVENTS` (8-K event types) [S] | varies | FMP: **no availability stamp** — the announced-date is usable, the *estimate* history is not PIT | see slot 2/3 | commercial |
| **Earnings-call transcripts** | FMP Ultimate tier; EODHD; HF community sets [S] | 2010s+ | transcript publication date ≈ known_at | FMP Ultimate [T] | commercial |
| **Analyst rating changes history** | FMP `upgrades-downgrades`; Benzinga ratings via Alpaca? (not confirmed) | varies | **the weakest PIT link in the whole stack** — ratings histories are routinely restated | — | **Treat as `archival_reconstructed` by default.** |
| **GICS sector history** | Not free. Sharadar/EODHD give *current* sector. | — | **No free PIT sector history exists.** | — | Mitigate: store the sector you observed at ingest with its `known_at`, and let old events keep the sector you recorded then. |
| **Options implied vol history** | OptionMetrics/IvyDB is the standard and is institutional; Option Strategist publishes **free weekly** HV/IV/IV-percentile per underlying [S]; CBOE Data Shop sells historical options [S] | — | weekly | $0 (weekly) / institutional | For regime purposes VIX + realized vol is enough; skip single-name IV. |
| **Regime labels** | NBER recession dates on FRED (`USREC`) — **announced with long lags and revised; use the ALFRED vintage or don't use it** | 1854+ | only via ALFRED | $0 | free |
| **Delisting returns** | No free source. Sharadar TICKERS gives delisting dates; the *return* convention comes from the literature (see §D). | — | — | — | Apply a configured terminal return by delisting reason. |

---

# D. Methodology — what best-in-class does for the comparable-setups question

## D.1 Event-study standards

- **MacKinlay (1997), "Event Studies in Economics and Finance", JEP 35(1)** — the canonical
  framework: estimation window → event window → abnormal return → aggregation (AAR/CAAR) → test. [T,
  universally cited]
- **Kothari & Warner (2007), "Econometrics of Event Studies", Handbook of Empirical Corporate Finance
  ch. 1** — the state-of-the-art review. Two findings matter to you:
  **"Short-horizon methods are quite reliable, while long-horizon methods have improved but serious
  limitations remain"** [S], and that event-study statistical properties **vary by calendar period and
  by firm characteristics such as volatility**, which is why they recommend **stratified samples** [S].
  → Spec N's regime stratification (§5.4) and vol-bucket matching (§4.5) are directly supported by
  this. Your 1–20 day horizons sit squarely in the "reliable" regime. Good news: the hardest
  econometrics (long-horizon BHAR) is a problem you mostly don't have.
  `papers.ssrn.com/sol3/papers.cfm?abstract_id=608601`
- **Fama (1998), "Market efficiency, long-term returns, and behavioral finance", JFE 49** —
  the **bad-model problem**: "Bad-model problems are most acute with long-term buy-and-hold abnormal
  returns (BHARs), which compound (multiply) an expected-return model's problems" [S]. Fama advocates
  the **calendar-time portfolio** approach. `sciencedirect.com/science/article/abs/pii/S0304405X98000269`
- **Mitchell & Stafford (2000), JB 73** — "conclude that the BHAR method should not be used in its
  conventional form"; BHAR's independence assumption is violated and **cross-sectional correlation
  significantly biases test statistics**; the calendar-time regression approach of Jaffe (1974) and
  Mandelker (1974) "provides more reliable inferences than long-run CARs or BHARs" [S].

**Implication for Spec N §5.2:** the spec currently says BHAR is reported *alongside* CAR beyond 20
sessions and "neither is quietly preferred." That is defensible as a display choice, but the
literature is not neutral — **BHAR is the worse estimator and the calendar-time portfolio is the
preferred one.** Recommend: keep showing all three, but state in `docs/` that when CAR, BHAR and the
calendar-time portfolio disagree, **the calendar-time portfolio is the tiebreak**, per Fama (1998) and
Mitchell & Stafford (2000). Spec N §6.2's "must agree in sign and rough magnitude, else `inconclusive`"
is a good rule; add the tiebreak so `inconclusive` isn't the only outcome.

## D.2 Overlapping windows and cross-sectional correlation

Two independent problems, and Spec N §6.1/§6.2 correctly requires two defences.

- **Overlapping windows (serial correlation in the aggregated series).** The stationary bootstrap of
  **Politis & Romano (1994)** resamples blocks of geometrically-distributed length, preserving
  dependence while keeping the resampled series stationary. Block length is not a free parameter:
  **Politis & White (2004), "Automatic Block-Length Selection for the Dependent Bootstrap",
  Econometric Reviews 23(1):53–70**, with the **Patton, Politis & White (2009) correction**, gives an
  automatic choice. `arch.bootstrap.optimal_block_length` implements exactly this, returning `b_sb`
  (stationary) and `b_cb` (circular) [S].
  → **Spec N §6.1 says "block length chosen from the horizon". Change it to "chosen by
  `arch.bootstrap.optimal_block_length` on the calendar-time abnormal-return series, with the horizon
  as a floor, and the chosen length printed in the response."** A block length picked from the horizon
  is an assumption; a block length estimated from the data is a measurement, and it costs one function
  call.
- **Cross-sectional clustering (30 semis on one day).** Two-way cluster-robust SEs (by event date and
  by ticker) via `statsmodels` `cov_cluster_2groups` [S], **plus** the calendar-time portfolio which
  absorbs it by construction. Spec N already requires both. Note that clustered SEs with **few
  clusters** (e.g. 15 distinct dates, your floor) are themselves unreliable — with <~30 clusters the
  asymptotics fail. **Recommend: when `n_distinct_dates < 30`, report the bootstrap CI only and label
  the clustered SE as unreliable rather than printing it.**

## D.3 Multiple testing and researcher degrees of freedom

- **Harvey, Liu & Zhu (2016), "…and the Cross-Section of Expected Returns", RFS 29(1):5–68** — with
  hundreds of published factors, "it doesn't make statistical sense to use the usual significance
  criteria of a t-ratio greater than 2.0"; they propose a **~3.0 hurdle** and a time series of
  historical cutoffs [S]. `papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314`
- **The counter-literature is real and worth knowing**: Chen & Zimmermann, *Publication Bias in Asset
  Pricing Research* (arXiv 2209.13623) and Chen, *Do t-Statistic Hurdles Need to be Raised?* (arXiv
  2204.10275) argue most claimed cross-sectional predictability findings are likely true and that
  hurdles need raising less than HLZ suggest [S]. → Don't hard-code t>3.0 as a truth; **report the
  trial count and the adjusted p-value and let the reader see both.** That is what Spec N §7 already
  does; just don't over-claim.
- **Bailey & López de Prado, "The Deflated Sharpe Ratio" (davidhbailey.com/dhbpapers/deflated-sharpe.pdf)**
  and **PBO / CPCV** — these are defined over *strategy Sharpe ratios* under N trials, not over a
  cohort's mean CAR. **See §G1 item 10: move them to Spec Q.** For Spec N, the correct instrument is
  **Romano–Wolf stepdown** (which controls FWER while accounting for the dependence between the
  variants you tried — and dependence is huge here, since setup variants overlap heavily) or, cheaply,
  **Šidák on the effective number of independent trials** (the `K_eff` idea in `ml4t-diagnostic` [V]).
  `arch`'s **StepM** is a Romano–Wolf stepwise procedure and is already in the dependency you're adding [V].

## D.4 Matching

- **Coarsened Exact Matching (Iacus, King & Porro)** bounds maximal imbalance *ex ante* by user
  choice, eliminates the need for a separate common-support step, is approximately invariant to
  measurement error, works with multi-category treatments, and is computationally cheap [S]. It is the
  right default for your covariates (sector, size bucket, liquidity bucket, vol bucket, regime), all of
  which are already categorical or naturally coarsened. Spec N §4.5 has this right.
- **Balance thresholds.** Modern practice: **|SMD| < 0.1 = negligible; > 0.2 flagged as notable
  imbalance** [S]. The 0.25 threshold in Spec N §4.5 is Rubin's older, looser rule.
  → **Recommend: warn at 0.10, hard-label `unbalanced` at 0.25.**
- **Propensity score matching** is *worse* here than CEM: it is model-dependent, and King & Nielsen's
  "Why Propensity Scores Should Not Be Used for Matching" shows PSM can *increase* imbalance. [T]
- Add **variance-ratio** alongside SMD — two groups can have identical means and very different
  spreads, and "your event is much more liquid" is about the tail as much as the mean.

## D.5 Small-sample honesty

Spec N §6.4 (added since I first read it) is right and matches the literature:
- **Wilson score intervals** dominate the normal approximation for proportions at small n and rates
  near 0/1 (Brown, Cai & DasGupta 2001). [T]
- **Empirical-Bayes / hierarchical shrinkage**: "a hierarchical model with empirical Bayes shrinkage
  provides a more robust estimate of the true effect by borrowing strength across units" [S]; the
  classic application is shrinking noisy batting averages toward the league mean [S] — exactly your
  "shrink this cohort's mean toward the setup family's pooled mean". The `n/(n+k)` weight is the
  standard beta-binomial / James–Stein form.
  **One caution:** shrinkage estimates are themselves "noisy and unstable, especially with small
  sample sizes" [S]. Fixing `k` in config and printing it (as the spec does) is the right mitigation.
- **Conformal prediction** would give distribution-free *predictive* intervals for a single next
  outcome, which is arguably what Bryan actually wants ("if I take this trade, what's the range?").
  It is a genuine addition rather than a replacement for the CI on the mean. **Low priority, real value.**

## D.6 PEAD as the canonical comparable-setups case

- PEAD is the most-documented "setup" anomaly; a standard study uses **SUE deciles, 60-day
  post-announcement windows, and thousands of events**. [T]
- **Has it decayed?** The evidence is genuinely mixed, and you should know both sides:
  - *For decay:* Richardson et al. (2010), Chordia, Subrahmanyam & Tong (2014), and **Martineau (2022)**
    find PEAD has weakened [S].
  - *Against:* **Meursault, Liang, Routledge & Scanlon (2023)**, "PEAD.txt", find **strong PEAD using a
    text-based earnings-surprise definition over 2008–2019** [S] (*JFQA*).
  - 2025: "Reviving PEAD with machine learning and historical earnings", *Finance Research Letters* [S].
  → **This is the empirical justification for Spec N §5.5's chronological split and `decayed` flag.**
  It is not a hypothetical concern; it is the best-studied case and it is contested. Cite it in the
  `docs/` page.
- **Practical n:** the literature uses thousands of events over decades. Spec N's floor of 30 matured
  events / 15 distinct dates is **generous, not conservative** — with 30 events and a 20-day horizon,
  a 1% abnormal return with 8% cross-sectional dispersion has a standard error of ~1.5%. You will
  correctly return `insufficient` a lot. That is the system working; make sure the docs page says so
  in advance so it doesn't read as a bug.

## D.7 Survivorship and delisting bias

- **Shumway (1997), "The Delisting Bias in CRSP Data", JF 52** — correct delisting returns are missing
  for most stocks delisted for negative reasons since 1962; the omitted returns are **large and
  negative** [S]. `tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf`
- **Shumway & Warther (1999), JF 54** — for Nasdaq the bias is **4.7× larger** than for NYSE/AMEX;
  they estimate that using a corrected **−55%** return for missing performance-related delisting
  returns corrects the bias; **when corrected, the Nasdaq size effect disappears** [S].
- → **Spec N §4.2's "delisting returns are applied per the standard practice for the delisting reason"
  should name the numbers**: approximately **−30% for NYSE/AMEX and −55% for Nasdaq** performance-related
  delistings, configurable, printed in the response, with the citation. An unnamed "standard practice"
  will get implemented as −100% or as 0% by whoever writes it. That an anomaly *disappears* under this
  correction is the strongest single argument for the whole `clean_pit` tier.

## D.8 Regime definition

- HMM regime detection is the popular ML answer and it has documented failure modes: **overfitting
  without regularisation, Gaussian states collapsing onto tiny variance regions, structural instability
  of learned regimes**, and "numerous short-lived regimes that are unintuitive and difficult to trade,
  frequently resulting in underperformance versus buy-and-hold" [S]. Practitioners respond by using
  **heuristic/static HMMs with predefined parameters that give deterministic outputs**, precisely
  because that is "highly suitable for trading strategies requiring consistency and backtesting
  reliability" [S].
- → **Spec O §4.2's deterministic, versioned, threshold-based classifier is the correct choice and is
  supported by the practitioner literature, not a simplification.** Say so in the spec so nobody
  "upgrades" it to an HMM later.
- **A regime label must not be a lookahead.** A classifier that uses a 200-day moving average is fine;
  one that uses "was this period later called a bear market" is not. Also: **a regime label computed
  from a *revised* macro series is lookahead even if the rule is deterministic** — which is exactly why
  Spec O §4.1 has to land before Spec N's regime stratification means anything. That dependency should
  be explicit in README §6 (Phase 4 partially blocks Phase 3's regime feature).
- Alternative worth one line in the docs: **statistical jump models** (arXiv 2402.05272,
  "Downside Risk Reduction Using Regime-Switching Signals: A Statistical Jump Model Approach") produce
  more persistent regimes than HMMs [S]. Still not worth the complexity here.

## D.9 Do analogs add information over covariate-matched cohorts?

Honest answer: **weakly, and only as a candidate generator — which is exactly how Spec N §3 already
uses `data/analog_ranker.py`.**

The pattern-matching / DTW literature is not encouraging. Findings I could verify: a pattern-matching
method "did not surpass the random walk model without preprocessing" and only beat it on RMSE/hit-ratio
*after* preprocessing [S]; another proposed technique "was more effective than the random walk model
but it does not statistically surpass" it [S]. Claims to the contrary come from vendor blogs, not
journals. Meanwhile the **covariate-matched cohort** is estimating a well-defined quantity (the mean
abnormal return of comparable events) with quantifiable uncertainty; a DTW/kNN analog set is estimating
a quantity nobody can define.

**Recommendation for Spec N:** keep `analog_ranker` as a candidate generator, and **add one test** —
`test_analog_generator_adds_no_bias` — asserting that the final cohort statistics are computed over the
*matched* set and are invariant to the analog ranker's ordering. Otherwise the similarity score becomes
a hidden selection-on-outcome channel. Also: `analog_ranker` currently ranks by a similarity score and
takes the top 30 (`scored[:30]`, `top[:10]`) — a fixed top-k over a score that includes outcome-adjacent
features is exactly failure mode 10. Confirm the score uses only pre-event features.

## D.10 Research notebook / thesis tracking / decision journal practice

- **Pre-mortems** ("assume this failed; explain why") and structured analytic techniques —
  checklists, pre-mortems, Delphi — "yield immediate improvements in forecast calibration" [S].
  Spec P's `thesis-critic` is a pre-mortem with a tool budget. Good.
- **Record the decision before the outcome is known** — thesis, assumptions, price, horizon [S].
  Spec M's `decision_journal` does this, including the decision to pass, which most practitioners omit
  and which is where selection bias in your own track record lives.
- **Calibration via Brier score.** Tetlock's Good Judgment Project superforecasters score **~0.20–0.25**
  Brier (0 perfect, 2 worst) [S]. That is your benchmark. Spec M §6's calibration table + Brier over
  resolved theses, reported only after **10** resolutions, is the right shape — though 10 is very few
  for a calibration *table* (you'll have 2–3 per bucket). Consider: report the **Brier score** at n=10
  and the **calibration table** at n=40, with the buckets coarsened to three (≤40%, 40–60%, ≥60%) below
  n=100.
- **Brier decomposition** (reliability, resolution, uncertainty) is worth more than the raw score: it
  separates "my 70%s happen 70% of the time" from "I only ever say 60%". One extra function.
- Keeping the *original* probability for scoring when it's revised (Spec M §6) is exactly right and is
  the discipline most journals lack.

---

# E. Agent layer and tooling (2026)

## E.1 Two instruction files: stop testing, start importing

Verified from the Claude Code docs and the ecosystem:
- **AGENTS.md** was released by OpenAI (Aug 2025), transferred to the **Linux Foundation's Agentic AI
  Foundation** in late 2025, and by May 2026 had 60,000+ repositories and 170+ AAIF member orgs [S].
  Read natively by Codex CLI, Copilot, Cursor, Windsurf, Amp, Devin, Aider, Zed, Jules, VS Code, Junie [S].
- **Claude Code's native file is `CLAUDE.md`.** Sources conflict on whether it also reads `AGENTS.md`
  directly; the **documented, unambiguous pattern is an import** — make line 1 of `CLAUDE.md` be
  `@AGENTS.md` (a symlink also works) [S].

→ **Replace `test_agents_md_parity` (Spec K §8, Spec P §8) with `@AGENTS.md`.** Shared non-negotiables
live in `AGENTS.md`; `CLAUDE.md` is a thin Claude-specific layer above the import. Parity becomes
impossible to break rather than tested-for. Keep a much smaller test: assert `CLAUDE.md` line 1 is the
import and that `AGENTS.md` contains the four non-negotiable strings.

## E.2 Subagents

The `.claude/agents/` format, verified from `code.claude.com/docs/en/sub-agents` [V]:
- Markdown + YAML frontmatter; `.claude/agents/` (project, checked in) or `~/.claude/agents/`;
  directories scanned **recursively**.
- Required: `name`, `description`. Optional and directly useful to Spec P §4:
  - **`tools`** (allowlist) and **`disallowedTools`** (denylist, applied first) — and both accept
    **MCP patterns**: `mcp__<server>`, `mcp__<server>__*`, and `mcp__*` (denylist only).
  - **`model`** — `sonnet`/`opus`/`haiku`/`fable`/full ID/`inherit`. This is how Spec P §6's
    "analyst-tier work runs on the cheaper model" becomes configuration rather than prose.
  - **`maxTurns`** — a hard bound per subagent. Spec P §6's "budget per session" gets a second,
    structural enforcement point for free.
  - **`permissionMode`**, **`skills`**, **`mcpServers`** (inline or by reference), **`hooks`**,
    **`memory`** (`user`/`project`/`local`), **`effort`**, **`isolation: worktree`**.
  - **`tools: Agent(worker, researcher)`** restricts which subagents a subagent may spawn; **omitting
    `Agent` entirely prevents spawning any subagent** — a clean way to guarantee `thesis-critic` cannot
    fan out.

→ **Concrete Spec P §4 upgrade.** Each subagent row should specify `tools`, `model`, and `maxTurns`
explicitly, and `thesis-critic`/`cohort-analyst` should omit `Agent`. And `test_subagent_tool_scopes`
should parse the actual frontmatter rather than asserting on prose. Note: **Codex has no equivalent
subagent primitive**, so "mirrored for Codex" in Spec P §4 means *prompt files a human or the lead
agent pastes*, not an executable equivalent. The spec should say that plainly rather than implying
parity.

## E.3 Tool output is data, not instruction

2026 consensus, verified: "User messages, retrieved documents, tool outputs, and email content —
none of it should be treated as instructions"; "every serious 2026 defense treats every retrieved
token as untrusted by default" [S]. The practical measures:
1. **Structural delimiters with random boundary markers** — wrapping untrusted content in
   randomly-generated markers and telling the model to treat the contents as data gives **95%+ defense
   rates vs ~60% for no defense** [S]. Random per-call nonces matter: static delimiters are defeated by
   "probabilistic delimiter injection", where an attacker guesses an inexact delimiter the model
   mis-parses [S] (arXiv 2607.05120).
2. **Spotlighting** (datamarking/encoding untrusted spans) and **CaMeL / dual-LLM** patterns for
   higher assurance [S]. `github.com/tldrsec/prompt-injection-defenses` is a good catalogue [S].
3. **The strongest defence you already have: capability restriction.** No agent-reachable path places
   an order (Spec L §6, import-graph tested). An injected instruction in a 10-K exhibit can, at worst,
   cause a bad `research_write`. Say this explicitly in Spec P §5 — it converts a scary open problem
   into a bounded one.

→ **Spec P §8 `test_untrusted_content_marked` should assert a *per-response random nonce*, not a fixed
tag.** And add: filing/news bodies are returned with `content_trust: "untrusted"` in the provenance
block, and `research_write` refuses to store a section whose sources are all untrusted-tier without a
human-authored flag.

## E.4 Cost controls

- Spec K §5 / P §6 are right that determinism is the cost control. Add two mechanical backstops:
  **`maxTurns` per subagent** (E.2), and a **per-token rate limit at the MCP layer** (Spec K §4.1
  already says "a runaway agent loop must cost time, not money" — make it a concrete
  requests-per-minute-per-token number, e.g. 60/min read, 10/min write).
- The `llm_call` ledger + Langfuse tagging is already the right design.

## E.5 Well-built finance MCP servers to study

- **`edgartools`' bundled MCP server** [V] — closest to what you're building, free, MIT.
- **`openbb-mcp-server`** (last release 2026-05-26) — **progressive tool disclosure**: discovery tools
  first, categories activated per session, per client, without cross-client interference [S]. The best
  single idea in the finance-MCP space.
- **Alpaca MCP** — data + trading in one surface [S]; useful as a counter-example of what your
  architecture deliberately refuses (Spec L §6).
- **Polygon/Massive official MCP** [S], SEC EDGAR MCP servers [S], `financial-datasets` MCP [T].

---

# F. Brokers

## F.1 Robinhood — and the finding that should change Spec L

**Robinhood shipped an official agentic-trading MCP server**, live at
**`https://agent.robinhood.com/mcp/trading`**, authenticating with **OAuth** so the agent never handles
credentials; described as introduced **May 2026** and generally available by **July 2026** [S].

Verified-by-search capabilities:
- **Read:** positions, balances, portfolio, order history across your accounts; live equity quotes;
  symbol search; watchlists [S].
- **Trade:** `review_equity_order` (simulates the order, reproduces Robinhood's price collar, returns
  pre-trade warnings, **places nothing**) → `place_equity_order` → `cancel_equity_order` [S].
- **Order types:** market / limit / stop / trailing, including `sell_short` on the surface [S].
- **The constraint that matters: order placement is confined to a dedicated, separately funded
  "Agentic" account. All other Robinhood accounts remain read-only to agents.** [S]

**Implications for Spec L, in order of importance:**
1. **§5.1 and §6 need a paragraph on the Agentic-account boundary.** Your ledger can read the whole
   book; your propose→approve→execute path can only ever *act* on the Agentic account. If Bryan's
   actual positions are in his primary account, the execution path in Phase 6 does nothing for them.
   This is a product decision (fund the Agentic account, or accept execution is manual) and it belongs
   in README §3, not discovered in Phase 6.
2. **`can_place_attached_stop` — the empirical question Spec L §5.1 correctly defers.** `stop` and
   `trailing` appear as order *types* [S], but I found **no evidence that the MCP exposes a *bracket* or
   an OCO stop attached to an entry**. A standalone stop order placed after a fill is *probably*
   sufficient for "a protective exit that outlives our process" — but it is a second order that can
   fail independently, so the capability probe must verify that the stop **exists at the broker** after
   entry, not that the call returned 200. Spec L §5.1's "determine empirically … record with evidence"
   is exactly right; add the specific assertion.
3. **Confirm which server you are actually connected to.** The tools named in Spec L §5.1 and Spec O
   §3.4 (`get_sec_filing_facts`, `get_equity_news`, equity tax lots, realized P&L) do **not** appear in
   descriptions of the *official* server; they do appear in **unofficial** community servers (e.g.
   `kevin1chun/robinhood-for-agents`, 50 tools, browser-login-based, explicitly "not affiliated with …
   Robinhood" [V]; `Open-Agent-Tools/open-stocks-mcp` [S]). An unofficial server that logs in via a
   browser session is a materially different security and reliability posture from an OAuth-scoped
   first-party endpoint. **This should be resolved before Phase 1 and recorded in
   `docs/ROBINHOOD_INTEGRATION_PLAN.md`.**
4. The `review_equity_order` → show human → `place_equity_order` pattern is Robinhood's own
   recommended safety flow [S] and maps one-to-one onto Spec L §6. Use their review call as the
   pre-trade risk snapshot rather than reimplementing the price collar.

## F.2 Schwab — the deferral is correct, and here's the evidence

(README §3 now defers Schwab entirely; this section supports that decision rather than reopening it.)
- Individual developers register an app on the Schwab Developer portal; approval takes
  **1–3 business days** and passes through an "Approved — Pending" state [S].
- **The killer: a Trader API refresh token is valid for 7 days.** On expiry you must redo the
  full `authorization_code` flow interactively [S]. `schwab-py` auto-refreshes access tokens (30 min)
  but **cannot** renew the 7-day refresh token without a human in a browser [S].
- → For an unattended Railway service, that is a **weekly manual re-auth** forever. Deferring is
  correct, and this is the sentence to put in the spec: *"Schwab's 7-day refresh-token expiry makes
  unattended operation impossible without a weekly human browser login; revisit only if Schwab changes
  it."*

## F.3 IBKR

- IBKR Web API uses **OAuth 2.0 with `private_key_jwt` client authentication** (signed JWT
  `client_assertion` validated against a registered public key) [S] — but **"OAuth protocol direct
  connection is available only to institutional users"** [S]. Retail individuals go through OAuth 1.0a
  or the Client Portal Gateway, which is a local process you must keep alive.
- `ib_async` (the maintained successor to `ib_insync`) is at **2.1.0** with published API docs [S].
- → Spec L §7's deferral stands. IBKR is the best *broker*, with the worst *unattended session model*
  for this architecture.

## F.4 Alpaca

Unchanged and correct as the paper venue. Two things it also gives you for free that the specs don't
currently claim: **news back to 2015 via Benzinga** (slot 6) and free EOD/IEX bars. Worth using.

---

# G. Critique of the plan

## G.1 Top 10 concrete upgrades (repeated from the summary, with the evidence)

| # | Upgrade | Spec section | Evidence | Impact/effort |
|---|---|---|---|---|
| 1 | Replace `test_agents_md_parity` with `CLAUDE.md` line 1 = `@AGENTS.md` | K §4.3, K §8, P §8 | Official Claude Code pattern; AGENTS.md is a Linux Foundation standard with 60k repos [S] | High / trivial |
| 2 | SMD warn at 0.10, hard-label at 0.25; add variance ratio | N §4.5 | Modern matched-cohort practice is <0.1 negligible, >0.2 notable [S]; 0.25 is Rubin's older rule | High / trivial |
| 3 | Automated lookahead harness: re-run each cohort with data truncated at the event date and assert identical output | N §10, Q §16 | freqtrade ships `lookahead-analysis` and `recursive-analysis` for exactly this [V] | High / medium |
| 4 | Macro `known_at_utc` is **day-resolution**; add `precision` to `source_observations` | O §4.1, Q §8 | ALFRED vintages are dated, not timestamped [S]; FRED has no release-time field | High / low |
| 5 | Store **three** price series (raw, split-adjusted, total-return) + split & dividend factors with ex-dates | N §4.3, slot 2 | Polygon provides no dividend-adjusted data [S]; CAR of a price-return stock vs a total-return benchmark is biased by the yield gap | High / medium |
| 6 | Make the shrinkage `family` slug the key for `trials_against_this_pattern` | N §6.4, N §7 | "this fact pattern" is currently undefined and therefore gameable | Medium / trivial |
| 7 | Document the Robinhood **Agentic-account** placement boundary and resolve which MCP server is connected | L §5.1, L §6, README §3 | Official RH MCP confines placement to a separately funded Agentic account [S]; the tool names in the spec match an *unofficial* server [V] | High / low |
| 8 | Tax lots with a booking method + wash-sale **awareness** flag | L §3, M | beancount's `{STRICT,FIFO,LIFO,AVERAGE,NONE}` per-account model [S]; 61-day window [S] | High / medium |
| 9 | `docs/DATA_LICENSES.md` + CI check that `research/` holds no raw vendor series | README §3, K §3.3 | Tiingo and Polygon both prohibit redistribution **and derived works**; yfinance access violates Yahoo ToS [S] | Medium / low |
| 10 | Move deflated Sharpe / PBO / CPCV to Spec Q; use **Romano–Wolf StepM** (in `arch`) for Spec N's trial adjustment | N §7, Q §10 | DSR is defined over Sharpe under N trials, not over a mean CAR [S]; `arch` ships StepM/SPA/MCS [V] | Medium / low |

Two more that just miss the top 10 but are cheap:
11. **Name the delisting-return convention** (−30% NYSE/AMEX, −55% Nasdaq, per Shumway & Warther) in
    Spec N §4.2 rather than "standard practice" [S].
12. **Use `arch.bootstrap.optimal_block_length`** instead of "block length chosen from the horizon",
    and print the chosen length (N §6.1) [S].

## G.2 Over-engineered, or things best-in-class deliberately does not do

- **`test_agents_md_parity`.** Already covered. Nobody does this; they import.
- **The Schwab stub** — already correctly removed by the README update. Good call; a stub nobody
  exercises rots.
- **"The two methods must agree in sign and rough magnitude, else `inconclusive`" (N §6.2)** — the
  rule is good but *symmetric*, which is wrong. The calendar-time portfolio is the preferred estimator
  in the literature (Fama 1998, Mitchell & Stafford 2000) [S]. Give it the tiebreak instead of
  discarding both.
- **BHAR beyond 20 sessions "shown, neither quietly preferred" (N §5.2)** — see above. Also: your
  horizons are 1–20 days. Kothari & Warner: short-horizon methods are reliable, long-horizon ones are
  not [S]. **You may not need BHAR at all.** Consider dropping it until you actually run a >20-day
  horizon, and save the implementation.
- **Four outcome measures × 5 horizons × N regimes × 2 chronological halves × bootstrap × placebo ×
  random-cohort × pre-event window, all mandatory, all non-optional fields.** This is the most likely
  place the plan stalls. A `CohortAnswer` with ~12 mandatory nested blocks is a lot of code before the
  first real answer. **Recommend a `depth` parameter**: `quick` (raw + CAR + n + CI + tier) for
  iteration, `full` (everything) for anything that gets written into a thesis or a Spec Q promotion —
  with `full` mandatory for any `journal_append` or `research_write` reference. Same rigour where it
  matters, 10× faster feedback loop where it doesn't.
- **`portfolio_snapshots` computing beta, realized vol, and pairwise correlation among largest
  positions** (added in the L update) — good idea, but note this is a *risk model*, and a 60/250-day
  sample correlation matrix over ~10 positions is extremely noisy. Print n and the window (the spec
  says to), and consider Ledoit–Wolf shrinkage on the correlation matrix, or just don't call it a
  correlation *matrix* — report the max pairwise correlation and the average, which is what the
  "six names are one position" insight actually needs.
- **Sync every 15 minutes during market hours** (L §4). For a swing-trading horizon with a 20-minute
  freshness budget, hourly plus on-demand is enough and is 4× less API pressure. Not wrong, just
  unnecessary.

## G.3 Missing entirely

1. **Position sizing.** Nothing in K–Q says how big a position should be. `propose_order` carries a
   quantity from nowhere. This is the single largest gap: sizing dominates selection in realized P&L.
   Minimum viable: risk-per-trade as a fraction of equity, size = risk$ / (entry − stop), capped by
   concentration limits. Kelly is the wrong tool at your sample sizes (it requires an edge estimate
   you have just spent Spec N proving you don't reliably have) — **fractional/half-Kelly at most, and
   only after Spec N returns a `clean_pit` non-`insufficient` answer.** Borrow pysystemtrade's
   volatility-targeting framing [V].
2. **Tax lots and wash sales.** §G1 item 8.
3. **Factor/risk exposure.** `exposure_tags` (narrative) and sector weights are good, but there is no
   beta, no size/value/momentum tilt, no "this whole book is one factor". The L update adds beta and
   correlation — extend to a 3-factor regression of portfolio returns on market/size/momentum proxies
   (free from Ken French's data library, which is genuinely free and PIT-clean).
4. **Options.** Explicitly out of scope in Q §3. Fine — but *say so in K–P too*, because "portfolio
   ledger across brokers" implies completeness and Robinhood accounts hold options. **At minimum, the
   ledger must not silently omit option positions from exposure math.** An account with short puts and
   no visible position is the worst possible failure mode of a portfolio view.
5. **Calibration scoring of theses.** The M update added stated probabilities and Brier — good. Add
   the **Brier decomposition** (reliability/resolution/uncertainty) and a coarser bucket scheme at
   low n (§D.10).
6. **Data licensing / redistribution.** §G1 item 9.
7. **Cost ceiling.** Model spend is budgeted (P §6). **Data spend is not.** Add a row to README §3:
   a monthly data budget and the rule that adding a paid source requires retiring one or an explicit
   owner decision. Otherwise slot-filling ratchets.
8. **A "what if the vendor dies" story.** Spec K §8 tests the API being down. Nothing tests a *data
   vendor* going away or changing schema. Since every fact lands in `source_observations`, you are
   mostly safe — but add `test_adapter_schema_change_fails_loudly`: an adapter returning an unexpected
   shape must raise, not write nulls.
9. **Backfill provenance.** When you buy Sharadar and backfill 20 years, those rows have a
   `known_at_utc` reconstructed from `datekey` — which is legitimate but is *not* the same as a fact
   you observed live. Add a third dimension to the tier: `observed_live` vs `vendor_pit` vs
   `archival_reconstructed`. The middle tier is what most of your history will be, and conflating it
   with either neighbour is a lie in a different direction each time.
10. **Timezone and session discipline.** `simulate_trade` uses calendar days for the time exit
    (correct, mirrors `order_monitor`) while horizons in Spec N are in *sessions*. Both are defensible;
    mixing them silently is not. Make the unit explicit in `SetupSpec.horizons_days` (rename to
    `horizons_sessions`) and in the response.

## G.4 Cost summary

Recommended: **~$100–120/mo** falling to **~$80–90/mo** after FMP is retired.
Free-tier-only: **~$10–20/mo** (Railway only), with the honest consequence that pre-collection history
is capped at `archival_reconstructed`.
Model spend is not included and is, by the architecture's own design, the smaller number.

---

## Appendix: sources

Verified by fetch [V]: github.com/polakowo/vectorbt · github.com/nautechsystems/nautilus_trader ·
github.com/stefan-jansen/zipline-reloaded · github.com/dgunning/edgartools · pypi.org/project/edgartools ·
github.com/jlowin/fastmcp · github.com/PrefectHQ/fastmcp/releases · github.com/modelcontextprotocol/python-sdk ·
pypi.org/project/mcp · pypi.org/project/fastmcp · github.com/bashtage/arch · github.com/skfolio/skfolio ·
github.com/eslazarev/purged-cross-validation · github.com/ml4t/diagnostic · github.com/OpenBB-finance/OpenBB ·
github.com/microsoft/qlib · github.com/virattt/ai-hedge-fund · github.com/TauricResearch/TradingAgents ·
github.com/ghostfolio/ghostfolio · github.com/robcarver17/pysystemtrade · github.com/rotki/rotki ·
github.com/freqtrade/freqtrade · github.com/QuantConnect/Lean · github.com/fja05680/sp500 ·
github.com/kevin1chun/robinhood-for-agents · github.com/mortada/fredapi · code.claude.com/docs/en/sub-agents

Key search-extracted [S]: sharadar.com/prices · data.nasdaq.com/databases/SEP · polygon.io pricing &
knowledge-base (delisted tickers; split/dividend adjustment) · eodhd.com delisted & S&P constituents ·
norgatedata.com NDU Windows requirement · tiingo.com/about/pricing · databento.com/corporate-actions ·
sec.gov EDGAR APIs · sec.gov Form 13F / Insider Transactions Data Sets · fred.stlouisfed.org API docs ·
docs.alpaca.markets historical news · sec-api.io/pricing · openfigi.com FAQ · finra.org equity short
interest · huggingface.co/datasets/Zihan1004/FNSPID · arxiv.org/abs/2402.06698 ·
papers.ssrn.com/sol3/papers.cfm?abstract_id=608601 (Kothari & Warner) ·
sciencedirect.com/science/article/abs/pii/S0304405X98000269 (Fama 1998) ·
tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf · papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314
(Harvey–Liu–Zhu) · arxiv.org/pdf/2204.10275 & 2209.13623 (Chen; Chen & Zimmermann) ·
davidhbailey.com/dhbpapers/deflated-sharpe.pdf · bashtage.github.io/arch optimal_block_length ·
statsmodels.org sandwich_covariance · railway.com/deploy/fastmcp & docs.railway.com/guides/mcp-server ·
github.com/tldrsec/prompt-injection-defenses · arxiv.org/html/2607.05120 · robinhood.com agentic-trading
overview · cran.r-project.org/web/packages/schwabr · interactivebrokers.com/docs/web-api/introduction
