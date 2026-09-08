# SwingTrader Investment Workspace — spec series K–Q

**Status:** Draft v0.4 — post-review; owner decisions closed 2026-09-08 (§3); three owner actions remain (§9)
**Date:** 2026-09-05
**Owner:** Bryan Niyonzima
**Target repository:** `niyonzimabryan/swingtrader`
**Predecessors:** `specs/audit-2026-07-04/` (A–J, shipped as PRs #21–#37),
`specs/investment-workspace/strategy-lab/strategy-lab-architecture.md` (recovered
Draft v0.1, 2026-08-21 — the experiment plane, unchanged and folded in here as Spec Q).

---

## 1. What we are building, in one paragraph

SwingTrader stops being "a Telegram bot that scans and scores tickers" and becomes
**a cloud-hosted investment workspace that any agent session can attach to** — from
Claude on the web, Claude Code local or cloud, Codex local or cloud, or Cursor. The workspace
holds four things no chat transcript can hold: the **portfolio** (what Bryan actually
owns, across brokers), the **research** (dossiers, theses, and what would invalidate
them), the **evidence** (point-in-time facts, comparable historical setups, filings,
macro, news), and the **experiments** (versioned strategies measured against each
other). An agent session is the *interface*, not the *system of record*. Deterministic
work — syncing, pricing, cohort statistics, backtests, reconciliation — is ordinary
Python on a schedule and costs no inference. Model calls are reserved for synthesis,
critique, and conversation.

The single sharpest capability, the one everything else serves:

> **Given this setup, how have setups genuinely like it performed — measured
> honestly, with the sample size, the benchmark subtracted, and the uncertainty shown?**

That question is Spec N. Every other spec exists to make its answer trustworthy or to
make it reachable from wherever Bryan is.

---

## 2. The specs

| Spec | Name | Answers |
|---|---|---|
| **K** | [Workspace core & tool surface](K-workspace-core-and-tool-surface.md) | How do I reach the same state from laptop, cloud, Codex, phone? |
| **L** | [Portfolio ledger & broker adapters](L-portfolio-ledger-and-brokers.md) | What do I own, what is it worth, what is exposed, what is pending? |
| **M** | [Research workspace](M-research-workspace.md) | What do we believe about this company, why, and what would kill it? |
| **N** | [Comparable-setups engine](N-comparable-setups-engine.md) | **How have setups like this actually performed?** |
| **O** | [Evidence planes: filings, macro, news](O-evidence-planes.md) | What are strong investors doing? What is the macro state? What is new? |
| **P** | [Agent layer](P-agent-layer.md) | How does the lead agent work, delegate, and stay cheap and safe? |
| **Q** | [Strategy Lab](strategy-lab/strategy-lab-architecture.md) | Which versioned strategies beat which, on evidence? |

Read order for an implementer: K → L → M → N → O → P, with Q running in parallel from
its own delivery plan. Read order for Bryan: this README, then N, then P.

---

## 3. Owner decisions (recommended defaults, in force until changed)

| Decision | Default | Why |
|---|---|---|
| Broker of record, today | **Robinhood** via the connected MCP server — **confirmed by owner 2026-09-05** | It is the account and interface that actually exists and is authenticated. |
| Second broker | **Schwab, deferred.** Owner: "RH is fine, might do Schwab later." Phase 1 ships the capability interface and contract tests only; no Schwab adapter file, no auth flow doc, until the owner asks. | Schwab absorbed TD Ameritrade; its Trader API is the plausible second venue. Building a stub nobody will exercise for months is maintenance without information. IBKR is deferred (Spec L §7). |
| Trade execution | **Agent proposes; Bryan approves; code executes.** No agent, local or cloud, may place an order. | Non-negotiable. Encoded in Spec L §6 and Spec P §5, not in prompts. |
| State of record | **Postgres on Railway**, with narrative research as **Markdown in the repo** | SQLite on a Railway volume is single-host and unreachable from a cloud session. This is the change that unlocks "accessible everywhere." |
| Hosting | Existing Railway project `e556a6d9-2023-4c81-a031-e32e160a33be`, one new web service for the workspace API/MCP | Reuses the deploy path already documented in the root `CLAUDE.md`. |
| Model spend | Deterministic-by-default. Every scheduled job is Python. LLM calls only on a human turn or an explicit synthesis step, each one budgeted and ledgered. | Bryan has already hit credit exhaustion once (BRY-301); the architecture should make that a nuisance, not an outage. |
| Live capital | Unchanged from Spec Q: one live champion, entry-by-entry approval, hard caps, kill switch | The Strategy Lab safety model is already correct and is not reopened here. |
| Repo mode | **Production.** Surgical, flagged, backward-compatible. | There is live-money code in this repo. Every spec below ships behind a flag defaulting off. |
| Robinhood execution scope — **decided 2026-09-05** | **Use the Agentic account.** Live entries under Phase 6 are placed there through the official Robinhood Trading MCP; the primary account is read-only to the workspace and holds whatever Bryan trades by hand. | **Verified 2026-09-06 from Robinhood's own pages** (`docs/research/2026-09-research-verification.md` §1–§6): "Your agent can only place trades in your Robinhood Agentic account." The repo already uses the official server. Caveats that survived verification: the product is **beta, not GA**; there is **no developer documentation** (order types, brackets, GTC, rate limits, token lifetime all unpublished); a cash Agentic account settles **T+1** unless limited margin is enabled; **no tool exposes dividends received or cash movements**. The ledger reads both accounts; risk caps span the combined book. Whether a protective stop is attachable is decided by dumping the `place_equity_order` schema (`scripts/dump_robinhood_tool_schemas.py`, owner action §9). **Account type, decided 2026-09-08: cash, no margin, funded with a budget Bryan loads.** T+1 settlement is therefore modelled everywhere turnover matters (Spec L §5.1, Spec N §5.3). |
| Data stack — **decided 2026-09-08** | Free tier for everything that has a free primary source: SEC XBRL + EDGAR bulk sets (fundamentals, insiders, holdings, events), FRED/ALFRED, Alpaca news (Postgres-only), `fja05680/sp500`, existing FMP history for the warmed event library. **The one paid feed is Sharadar "Prices" at $9/mo "from"** (owner screenshot of sharadar.com, 2026-09-08): tickers & metadata with listing/delisting dates, corporate actions, S&P 500 constituent history, stock prices, price-based metrics. Fundamentals ($19) and Investors ($19) tiers are not bought because EDGAR gives the same facts with regulator timestamps; the $29 bundle is the upgrade if XBRL normalisation proves expensive. Before paying: confirm what "from" gates (history depth or request volume), and run the twenty-delisting audit (Spec N §4.2). | Sharadar Prices is cheaper than EODHD ($19.99) and Tiingo ($30, internal-use-only licence) and is the only one of the three whose delisted coverage and constituent history are the product's stated purpose. There is no free delisting-complete US price source; scrappy reconstructions (Form 25 delisting dates plus a dead ticker's prices from somewhere) fail on the second half. At $9 the scrappy path costs more in tokens than the subscription. |
| Data budget | **$100/month ceiling** on data subscriptions. Adding a paid source retires one or is an explicit owner decision. | Model spend was already budgeted (Spec P §6); data spend was not, and slot-filling ratchets. Verified stack: Railway Pro $20 + ~$10–20 overage + Sharadar Prices $9, everything else $0 — **~$40–50/month**. |
| Data licensing | `docs/DATA_LICENSES.md` + CI check that `research/` holds no raw vendor series and nothing news-derived (Spec K §3.3). Runs **before the first public commit** of any data-bearing file. | Verified: Tiingo — "you may not display or share the data"; Alpaca — no publishing of "any derived products or services." FNSPID is CC BY-NC and dropped. Git history is permanent. |
| Position sizing — **decided 2026-09-08: advisory, not a gate** | Agents never choose a quantity. `propose_order` carries a `risk_fraction`; code sizes from entry and stop under the caps. **Two budgets.** An *evidenced* budget for proposals citing a positive `full` answer, scaled by the CI lower bound; and a separate *discretionary* budget with its own per-trade cap and daily notional for everything else — uncited proposals, and proposals whose cited evidence is `insufficient` or has a non-positive lower bound. Those are re-labelled `discretionary` with the evidence printed on the card; **nothing sizes to zero on evidence alone.** `EVIDENCE_GATE_MODE=advisory` is the config; `strict` restores zero-sizing later without code changes (Spec L §6.6). | Owner: "I want this to be a tool, not too much of a gate yet." The system will say `insufficient` for months; discretionary trades draw from their own budget so the evidence path and the judgment path are visible separately in the journal, which is what makes the honesty metrics (Spec M §6) meaningful. |
| Interface — **decided 2026-09-06** | **MCP + REST over HTTPS only.** Clients: Claude on the web and in cloud sessions, Claude Code local and cloud, Codex local and cloud, Cursor local and background agents. No CLI in the plan; admin operations are `scripts/`. **No Telegram surfaces in the workspace.** The existing Telegram bot is retained *only* as the out-of-band channel for order approval and pages, because approval must come from something an agent cannot call; a signed approval page under `/admin` can replace it later without touching anything else. | Owner: "just need it accessible here and Codex/Cursor locally and in cloud sessions, API/MCP based vs CLI, not too local so everything keeps working." Nothing in the workspace depends on a machine being on. |
| Scope cuts (v0.3) | **13F deferred** from Phase 4; **sector is a labelled current-vintage covariate**, never a required stratum; **analyst-estimate surprise from vendors replaced by XBRL seasonal SUE, with consensus recovered deterministically from timestamped news where articles state it** (N §4.0); **regime is reported, not a refusal gate**; **FNSPID dropped**. | Each is a case where the honest version of the feature is unavailable at retail and the approximate version would contaminate the number in the flattering direction (verification §22–§25, §34). |

**Resolved 2026-09-05:** the earlier instruction "if you have access to Robinhood
already pretend it's that, but make a Schwab" was superseded by "RH is fine, might do
Schwab later." Robinhood is the only broker built in this series. The capability
interface in Spec L §5 is still designed so a second adapter drops in; the adapter
itself is not written until asked.

---

## 4. Architecture

```text
   Claude (web / cloud)   Claude Code (local / cloud)   Codex   Cursor
            \                    |                 |            /
             \                   |                 |           /
              +------------- remote MCP + REST (HTTPS) ---------+
                                 |
                   ┌─────────────┴──────────────┐
                   │   Workspace API (FastAPI)  │   Spec K
                   │   one service, Railway     │
                   └─────────────┬──────────────┘
                                 |
     ┌──────────┬────────────────┼────────────────┬──────────────┐
     |          |                |                |              |
     v          v                v                v              v
 Portfolio   Research        Comparable       Evidence       Strategy Lab
  ledger     workspace     -setups engine      planes         experiments
  Spec L      Spec M           Spec N          Spec O          Spec Q
     |          |                |                |              |
     +----------+----------------+----------------+--------------+
                                 |
                    ┌────────────┴─────────────┐
                    │  Postgres (Railway)      │  structured, bitemporal
                    │  + repo Markdown mirror  │  narrative, git-versioned
                    └────────────┬─────────────┘
                                 |
                    ┌────────────┴─────────────┐
                    │  Scheduled Python jobs   │  zero inference:
                    │  sync · price · mature   │  cohort stats, reconcile,
                    │  reconcile · evaluate    │  forward returns, snapshots
                    └──────────────────────────┘
```

Three boundaries are load-bearing and must not be crossed:

1. **No agent touches a broker.** Agents call `propose_order`. Only the execution
   service, after a recorded human approval, calls a broker adapter. (Spec L §6)
2. **No LLM output is promotion evidence.** Model text can motivate a hypothesis and
   summarize a result. It can never be the statistic. (Spec N §8, Spec Q §10)
3. **Every fact carries `known_at_utc`.** A fact without a "when did we learn this"
   stamp is not eligible for any historical claim — it is labelled
   `archival_reconstructed` and quarantined. (Spec O §2)

---

## 5. Research slots — decided

The best-in-class research landed 2026-09-05:
[`research/2026-09-05-best-in-class-research.md`](research/2026-09-05-best-in-class-research.md)
(full report, ~900 lines, with sources). **Read its verification caveat first:** the
research session's egress proxy blocked most vendor domains, so prices and several
vendor capabilities are marked **[S]** (search extract) rather than **[V]** (fetched).
Re-check every price before spending. The findings changed vendors, libraries, and a
handful of spec sections that were wrong on the evidence (§8 lists them); they did not
change the architecture in §4.

| Slot | Spec | Decision | Runner-up | What would change it |
|---|---|---|---|---|
| Backtest / event-study engine | N, Q | **Keep and extend `backtest/simulator.py`** — vectorised NumPy path + `ExecutionPolicy` dataclass. No engine reproduces its semantics and §10 needs bit-for-bit equality. `backtrader` is inactive; `nautilus_trader` is LGPL + Rust mid-migration; `vectorbt` OSS is Commons-Clause and maintenance-only. | `vectorbt` OSS for cohort-wide forward-return matrices only | Intraday fills or multi-leg options → `nautilus_trader` |
| PIT equity prices + corp actions + delisted | N, O | **Sharadar "Prices" tier, $9/mo "from" (pricing verified by owner screenshot 2026-09-08)**: tickers with listing/delisting dates, actions, S&P 500 constituents, stock and fund prices, metrics. Delisted terminal-price handling still subject to the twenty-delisting audit before paying. Polygon/Massive `adjusted` is **splits-only, verified** (price returns, not total returns); Norgate needs a Windows updater process; no free source keeps delisted names. | EODHD All-World $19.99 (verified); Tiingo Power $30 (verified, internal-use-only licence) | The audit showing Sharadar stops at the last quote → synthesise Shumway terminal returns (§4.2), which the spec already provides for |
| PIT fundamentals | N, O | **SEC XBRL `companyfacts` + `submissions` (free) — verified by live API call**: `accn`, `filed`, `form`, `fy`, `fp`, `frame` on each fact; `acceptanceDateTime` per filing with second resolution and UTC. **`acceptanceDateTime`, never `filingDate`** (a Form 4 filed 2026-09-03 was accepted 22:30 UTC). Coverage is per-tag, so alias maps + discontinuity alerts (Spec O §3.4). FMP stays exploratory-tier. | Sharadar SF1 `datekey` (unverified); EODHD `filing_date` (unverified) | Needing pre-2009 fundamentals → a vendor becomes mandatory |
| SEC filings / entity resolution | O | **`edgartools`** (MIT, 5.56.0 on 2026-09-02 — verified on PyPI) + SEC bulk insider / financial-statement data sets + **OpenFIGI** (verified: CUSIP accepted as input; 25 req/6 s and 100 jobs/request with a key; one-way CUSIP→FIGI). CIK↔ticker history rebuilt from `submissions` `formerNames`. **13F deferred.** Still to confirm in a REPL: Form 4 code/10b5-1 fields and 13F-HR/A handling in `edgartools`. | `sec-api.io` Personal $49/mo [S] | Heavy 20-year full-text search over exhibits |
| PIT macro | O | **Keep `fredapi`** (has `get_series_as_of_date` / `get_series_all_releases`), pinned and wrapped — upstream is rated inactive. Vintages are **day-precision** (Spec O §2). `USREC` only via its vintage. Treasury curves are unrevised. | `pyfredapi` (active 2025) | `fredapi` breaking on a pandas release |
| News with timestamps | O | **Alpaca News API** (Benzinga, 2015→, free, 200 req/min — verified in Alpaca docs). **Terms bar publishing "any derived products"**, so nothing news-derived leaves Postgres (Spec O §5). Finnhub as cross-check. Gemini-search + Firecrawl **removed from every cohort path**. GDELT never supplies `known_at`. **FNSPID dropped** (CC BY-NC-4.0, verified). | Tiingo news, bundled if Tiingo is bought | — |
| Statistical rigor | N, Q | **`arch` 8.0.0 — verified by reading the shipped source**: `optimal_block_length` cites Politis–White 2004 + Patton–Politis–White 2009; `SPA`, `StepM`, `MCS` present (block lengths differ from Patton's MATLAB reference by design). + **`statsmodels`** (two-way clustered SEs) + ~250 vendored lines (Wilson, method-of-moments empirical Bayes, calendar-time regression, purged K-fold). **`mlfinlab` verified not open source — never a dependency.** | Write purged CV yourself (~40 lines) rather than depend on `purgedcv` / `ml4t-diagnostic`, whose maintenance is unverified | Spec Q growing real ML factor work → `skfolio` |
| Remote MCP transport / auth | K | **Official `mcp` SDK mounted in FastAPI**, streamable HTTP, **OAuth 2.1 with dynamic client registration plus static bearer** — both accepted, one scope model. Verified: `mcp` 2.1.1 renames `FastMCP`→`MCPServer` and `streamablehttp_client`→`streamable_http_client`; the SDK's own guidance is `mcp<2` for v1 code. **The unbounded pin was a live bug and is fixed in this revision.** Codex bearer verified; Claude Code's documented path is OAuth. | — | — |

Verified cost: **~$40–50/month** — Railway Pro $20 plus ~$10–20 overage (Hobby's $5
credit will not cover an always-on service plus Postgres) plus Sharadar Prices $9.
Sharadar's other tiers (Fundamentals $19, Investors $19, Bundle $29) were verified by
owner screenshot on 2026-09-08 and are not bought while EDGAR covers the same facts.

**If a spec's design would change because of a research finding, that is a bug in the
spec** — it was said so in v0.1, and §8 lists where it turned out to be true.

---

## 6. Delivery order

Sequenced so each phase is independently useful and nothing large lands before it can
be verified.

| Phase | Ships | Useful on its own because |
|---|---|---|
| **0a. Schema discipline** | Alembic baseline (already required by Spec Q §14); SQLite-ism audit with the fixes applied so the same models run on both engines; CI matrix runs the suite on SQLite **and** Postgres | Every later table is a migration, not an inline `create_all()`. Spec N and Spec Q can start the moment this merges — they need Alembic, not the cutover |
| **0b. Cutover** | Postgres provisioned on Railway, one-shot migration with parity report, Railway workspace service skeleton | Unblocks every cloud session; ends the SQLite single-host trap. Blocks Phases 1, 2, 4 (anything a remote session reads live) |
| **1. See it** | Spec L portfolio ledger + Robinhood sync + Spec K read-only tool surface. **First checkpoint: dump the `place_equity_order` / `review_equity_order` JSON Schema from `tools/list`; second: start the 30-day unattended refresh log on Railway.** | "What do I own and what am I exposed to" answerable from anywhere — and the two facts Robinhood does not document are measured instead of assumed |
| **2. Remember it** | Spec M dossiers/theses + Markdown mirror | Research survives the session it was done in |
| **3. Measure it** | **3a:** minimum SEC ingestion contract — XBRL `companyfacts`, `submissions` `acceptanceDateTime`, 8-K Item 2.02 index — into `source_observations` (free, no credentials, Spec N §4.0). **3b:** Spec N comparable-setups engine, `quick` depth first, price-only setups (gap-and-go) before the earnings roster | The core question gets a trustworthy answer; the earnings roster and `market_cap_decile` have their inputs without waiting on Phase 4 |
| **4. Widen it** | Spec O filings (13D/G, Form 4, 8-K — **13F deferred**) + macro vintages + news timestamps | Cohorts get better covariates and exact event timestamps; insider and activist views appear |
| **5. Race it** | Spec Q Strategy Lab (its own six-PR plan) | Champion vs challengers, on evidence |
| **6. Act on it** | Spec L §6 proposal→approval→execution path, live only per Spec Q §12 | Approved orders, with full protection lifecycle |

Phases 3 and 5 depend only on **0a**; they do not depend on each other or on 0b and can
run in parallel by different agents while 0b is in flight. Phase 3's regime split uses
`regime_v1`, built only from never-revised inputs (Spec O §4.2), so it does not wait on
Phase 4's macro-vintage work either; revised series enter as `regime_v2` afterwards. The comparable-setups engine
is SQL + NumPy over the event library that already exists locally; forcing it to wait
on a database cutover was a sequencing error in v0.1.

---

## 7. Definition of done for the series

- Opening this repo in Claude Code, a Claude cloud session, or Codex and asking
  *"what do I own, what is my thesis on X, and how have setups like X's performed"*
  returns the same answer in all four places, with sources and timestamps.
- Every performance number printed anywhere carries sample size, benchmark, uncertainty
  interval, and a data-quality tier.
- No scheduled job requires an LLM call to complete successfully.
- No code path exists by which an agent can place an order.
- `pytest` green, and a documented disaster case: the workspace API being down degrades
  agent sessions to read-only-from-git, never to wrong answers.

---

## 8. Changelog — v0.1 → v0.2 (2026-09-05)

What the research and the owner's replies changed. Each item names the section so a
reader of v0.1 can diff by eye.

**Owner decisions (§3).** Robinhood confirmed; Schwab deferred (L §5.2 rewritten, stub
dropped from Phase 1). Three new rows opened: Robinhood execution scope, data stack,
and two settled defaults — data budget ceiling and data licensing.

**Sequencing (§6, goal-prompts).** Phase 0 split into 0a (Alembic + engine-neutral
models) and 0b (Postgres cutover). Specs N and Q now depend on 0a only. `regime_v1`
decoupled from Phase 4.

**Spec N — sections the evidence overturned.** §4.2 stored `universe_membership` +
named delisting-return convention (Shumway); §4.3 three price series + factors with
ex-dates; §4.5 SMD warn 0.10 / label 0.25 + variance ratio, CEM over PSM; §5.2 BHAR
only >20 sessions, calendar-time portfolio is the tiebreak (Fama; Mitchell & Stafford);
§5.5 decay split; §6.1 block length estimated by `optimal_block_length` and printed;
§6.2 clustered SE unreliable below 30 clusters, asymmetric disagreement rule; §6.4
Wilson + declared shrinkage; §7 trial count keyed by family slug, Romano–Wolf StepM,
**deflated Sharpe / PBO moved to Spec Q**; §8 `depth` (quick/full), `vendor_pit`
provenance class; `horizons_days` → `horizons_sessions`; lookahead harness borrowed
from freqtrade; analog ranker constrained to pre-event features. Engine and library
decisions recorded in §3.

**Spec O.** `precision` and provenance class on every observation (§2); day-precision
macro known at the close; SEC XBRL as the free PIT fundamentals source (§3.4); Form 4
transaction codes named; OpenFIGI + `formerNames` history; deterministic regime
classifier stated as deliberate with `regime_v1` on unrevised inputs only (§4.2);
Alpaca news primary, Gemini/Firecrawl out of cohort paths, MinHash dedup (§5).

**Spec K.** Official `mcp` SDK pinned `<2`; bearer-for-CLI auth reality; concrete rate
limits; `CLAUDE.md` imports `AGENTS.md` instead of a parity test; licensing rule and CI
check on the mirror.

**Spec L.** Official-vs-unofficial Robinhood server resolution and the Agentic-account
boundary (§5.1); attached-stop probe verifies at the broker; options never silently
omitted; booking method + wash-sale awareness on lots; hourly sync; max/avg pairwise
correlation instead of a matrix; **§6.6 position sizing — agents never set quantity**;
Schwab 7-day refresh-token evidence; IBKR OAuth institutional-only.

**Spec M.** Stated probability required for `active`; Brier with decomposition;
calibration table floors.

**Spec P.** Subagent front-matter (`tools`, `model`, `maxTurns`, no `Agent`) as the
enforcement point; Codex parity stated as pasted briefs; per-response nonce on
untrusted content; `research_write` refuses all-untrusted sources; injection risk
bounded by the no-broker-path boundary.

**Explicitly not adopted (yet):** conformal predictive intervals (N §6.4 note),
Ken French three-factor exposure (L §3 note), Redis stream resumability (K §4 note),
OpenBB-style progressive tool disclosure (fifteen tools do not need it), HMM or
jump-model regimes (O §4.2 says why).

---

## 9. Owner actions — cheap, and they change the design

Four things only Bryan can do, each under half an hour, each blocking something:

1. **Dump the Robinhood `tools/list` schema** for `place_equity_order` and
   `review_equity_order` (the broker's `_tools_cache` already holds it). This is the
   only source for order types, bracket/attached-stop support, time-in-force, and GTC
   persistence — Robinhood documents none of it. Decides `can_place_attached_stop` and
   whether the exit simulator models an executable policy. Blocks Phase 6.
2. **Sharadar Prices tier, five minutes at checkout:** what "from $9" gates (history
   depth or request volume — the full-history tier is the one that matters), whether
   bulk download is included, and the redistribution terms. Pricing itself is now
   verified (§3).
3. **Run the twenty-delisting audit** (Spec N §4.2) against whichever vendor is on the
   table before paying. Decides EODHD vs Tiingo vs Sharadar on the one property that
   matters and that no vendor documents.
4. **Cherry-pick the `mcp<2` pin to `main`** (commit `18b47a2`) before the next Railway
   deploy. A fresh install today breaks every Robinhood call.

## 10. Changelog — v0.2 → v0.3 (2026-09-06)

From the primary-source verification pass
(`docs/research/2026-09-research-verification.md`). Verdicts that changed a section:

**Robinhood (L §5.1, README §3).** Official server confirmed as the one already in use;
beta not GA; no developer docs; placement confined to the Agentic account (verified
verbatim); T+1 on cash accounts; options tradeable (workspace never uses the write
tools); no dividend or cash-movement tools; stop is a separate post-fill order and the
execution service owns the fill-to-stop race; stale ledger refuses proposals.

**Spec N.** Floor is on distinct event dates first; calendar-time portfolio is the
*primary* estimator with `n_eff` reported; headline policy return is net of a
half-spread-by-liquidity cost model; regime is warned not refused; sector is a labelled
current-vintage covariate with cap/vol/price deciles as the honest strata;
`sue_seasonal` from XBRL replaces analyst-estimate surprise; announcement time from the
8-K Item 2.02 `acceptanceDateTime`; self-defined `liquid_us_equity_v1` universe;
twenty-delisting audit and within-cohort delisting rate with refusal on composition;
shrinkage `k` by method of moments; pre-registered setup roster; `cells_examined`;
§6.5 engine prediction ledger; sizing scaled by the CI lower bound (L §6.6).

**Spec O.** `acceptanceDateTime` never `filingDate`, with a test; XBRL per-tag coverage
alias maps + discontinuity alerts; 13F deferred; FNSPID dropped; news derivatives stay
in Postgres.

**Spec K.** Both OAuth 2.1 and static bearer accepted; the `mcp<2` bug recorded;
verified licence clauses in the mirror rule.

**Spec P.** Delimiters demoted to parsing aids; ingestion agent has no write tools;
`source_url` / `source_trust` on every ingested claim.

**Pushed back on, and why.** The verification pass suggested dropping regime
stratification and sector entirely. Regime stays as a *reported* split with a
`single_regime` warning because the cost of reporting is small once the calendar-time
series exists, and knowing the answer describes one market mood is worth having. Sector
stays as a labelled covariate because migration is rare and the contamination is
bounded; what changes is that it can no longer be a required stratum. Both are
reversible by config.

## 11. Changelog — v0.3 → v0.4 (2026-09-06, post-review)

From the independent plan review. Eleven should-fix findings, two cuts, one question.

**Taken, all eleven.** Phase 3 split into 3a (minimum SEC ingestion) and 3b, because
the earnings roster and market cap needed Phase 4 inputs (N §4.0, §6). The headline
quantity is now defined to the session: event clock, equal-weighted calendar-time
portfolio, `α × h`, and a hand-calculated fixture (N §5.0). Engine self-scoring uses
sign and cohort percentile, never CI "coverage" (N §6.5). Facts extracted from news
carry their article's timestamp, not the cluster's earliest (O §5.1). One news
eligibility matrix replaces two contradictory sentences (O §5.3). `shares_outstanding`
from the XBRL cover page feeds market cap (N §4.0). Delistings with known reasons are
resolved with a terminal return carried through every horizon; only unknown endings
are censored (N §4.4). Replay runs on split-adjusted prices with a split-invariance
test (N §4.3). The Markdown mirror is deliberately partial, filtered by provenance,
with the round-trip guarantee scoped to permitted content (M §5, §8; K §3.3). Sizing
has a formula, citation requirements, and an uncited path (L §6.6). Result status and
evidence tier are separate axes, refusals are their own schema, and there is one floor
configuration (N §8).

**Cuts taken.** Variance ratio against a single query event was undefined; the
query-vs-cohort diagnostic is now standardized distance plus percentile, and SMD /
variance ratio apply to matched-cohort-vs-pool where two groups exist (N §4.5).
Empirical-Bayes shrinkage and SPA/MCS are gated on a family having five cohorts;
v1 ships Wilson, the bootstrap CI, and StepM (N §6.4, §7).

**The question, answered as a default** (§3 sizing row): no, uncited proposals do not
size to zero; they are capped at half the risk budget and labelled. Cited negative
evidence sizes to zero. Owner to confirm.
