# SwingTrader Investment Workspace — spec series K–Q

**Status:** Draft v0.3 — verification-upgraded 2026-09-06; one owner decision open (§3, marked **OPEN**) and four owner actions (§9)
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
Claude Code on the laptop, a Claude cloud session, Codex, or a phone. The workspace
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
| Robinhood execution scope — **decided 2026-09-05** | **Use the Agentic account.** Live entries under Phase 6 are placed there through the official Robinhood Trading MCP; the primary account is read-only to the workspace and holds whatever Bryan trades by hand. | **Verified 2026-09-06 from Robinhood's own pages** (`docs/research/2026-09-research-verification.md` §1–§6): "Your agent can only place trades in your Robinhood Agentic account." The repo already uses the official server. Caveats that survived verification: the product is **beta, not GA**; there is **no developer documentation** (order types, brackets, GTC, rate limits, token lifetime all unpublished); a cash Agentic account settles **T+1** unless limited margin is enabled; **no tool exposes dividends received or cash movements**. The ledger reads both accounts; risk caps span the combined book. Whether a protective stop is attachable is decided by dumping the `place_equity_order` schema (Spec L §5.1, owner action §9). |
| Data stack — **OPEN** | Default: **free-tier first**, then the cheapest verified delisting-complete file. SEC XBRL + EDGAR bulk sets, FRED/ALFRED, Alpaca news (Postgres-only), `fja05680/sp500`, existing FMP history for the warmed event library. First paid step: **EODHD All-World at $19.99/mo (verified)** or **Tiingo Power at $30/mo (verified)**, chosen by the twenty-delisting audit (Spec N §4.2). **Sharadar remains unverified on every axis** — price, delisted retention, `datekey`, terms — because its pricing renders client-side; that is a 15-minute owner check (§9), not a research task. | There is no free delisting-complete US price source, and *no vendor's* delisting completeness is verified until the audit runs: a file can keep a delisted ticker and still stop at the last quote. Pre-purchase cohorts are capped by the tier system rather than quietly wrong. |
| Data budget | **$100/month ceiling** on data subscriptions. Adding a paid source retires one or is an explicit owner decision. | Model spend was already budgeted (Spec P §6); data spend was not, and slot-filling ratchets. Verified stack: Railway Pro $20 + ~$10–20 overage, EODHD $19.99 or Tiingo $30, everything else $0 — **~$50–70/month**. |
| Data licensing | `docs/DATA_LICENSES.md` + CI check that `research/` holds no raw vendor series and nothing news-derived (Spec K §3.3). Runs **before the first public commit** of any data-bearing file. | Verified: Tiingo — "you may not display or share the data"; Alpaca — no publishing of "any derived products or services." FNSPID is CC BY-NC and dropped. Git history is permanent. |
| Position sizing | Agents never choose a quantity. `propose_order` carries a `risk_fraction`; code sizes from entry and stop under the caps, scaled by the cited cohort's CI lower bound (Spec L §6.6). | v0.1 had no sizing rule anywhere and sizing dominates selection in realised P&L. The engine outputs a distribution; sizing from its mean throws that away. |
| Scope cuts (v0.3) | **13F deferred** from Phase 4; **sector is a labelled current-vintage covariate**, never a required stratum; **analyst-estimate surprise replaced by XBRL seasonal SUE**; **regime is reported, not a refusal gate**; **FNSPID dropped**. | Each is a case where the honest version of the feature is unavailable at retail and the approximate version would contaminate the number in the flattering direction (verification §22–§25, §34). |

**Resolved 2026-09-05:** the earlier instruction "if you have access to Robinhood
already pretend it's that, but make a Schwab" was superseded by "RH is fine, might do
Schwab later." Robinhood is the only broker built in this series. The capability
interface in Spec L §5 is still designed so a second adapter drops in; the adapter
itself is not written until asked.

---

## 4. Architecture

```text
   Claude Code (laptop)   Claude cloud session   Codex   phone / Telegram
            \                    |                 |            /
             \                   |                 |           /
              +--------- remote MCP + REST + `swing` CLI -----+
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
| PIT equity prices + corp actions + delisted | N, O | **Free-first (§3), then EODHD All-World $19.99/mo or Tiingo Power $30/mo — both prices verified — selected by the twenty-delisting audit.** Polygon/Massive `adjusted` is **splits-only, verified from their docs** (price returns, not total returns); Norgate needs a Windows updater process; no free source keeps delisted names. | Sharadar — **every claim about it is unverified** (owner check, §9) | The audit showing neither cheap vendor carries terminal collapses → Sharadar or synthesise Shumway returns |
| PIT fundamentals | N, O | **SEC XBRL `companyfacts` + `submissions` (free) — verified by live API call**: `accn`, `filed`, `form`, `fy`, `fp`, `frame` on each fact; `acceptanceDateTime` per filing with second resolution and UTC. **`acceptanceDateTime`, never `filingDate`** (a Form 4 filed 2026-09-03 was accepted 22:30 UTC). Coverage is per-tag, so alias maps + discontinuity alerts (Spec O §3.4). FMP stays exploratory-tier. | Sharadar SF1 `datekey` (unverified); EODHD `filing_date` (unverified) | Needing pre-2009 fundamentals → a vendor becomes mandatory |
| SEC filings / entity resolution | O | **`edgartools`** (MIT, 5.56.0 on 2026-09-02 — verified on PyPI) + SEC bulk insider / financial-statement data sets + **OpenFIGI** (verified: CUSIP accepted as input; 25 req/6 s and 100 jobs/request with a key; one-way CUSIP→FIGI). CIK↔ticker history rebuilt from `submissions` `formerNames`. **13F deferred.** Still to confirm in a REPL: Form 4 code/10b5-1 fields and 13F-HR/A handling in `edgartools`. | `sec-api.io` Personal $49/mo [S] | Heavy 20-year full-text search over exhibits |
| PIT macro | O | **Keep `fredapi`** (has `get_series_as_of_date` / `get_series_all_releases`), pinned and wrapped — upstream is rated inactive. Vintages are **day-precision** (Spec O §2). `USREC` only via its vintage. Treasury curves are unrevised. | `pyfredapi` (active 2025) | `fredapi` breaking on a pandas release |
| News with timestamps | O | **Alpaca News API** (Benzinga, 2015→, free, 200 req/min — verified in Alpaca docs). **Terms bar publishing "any derived products"**, so nothing news-derived leaves Postgres (Spec O §5). Finnhub as cross-check. Gemini-search + Firecrawl **removed from every cohort path**. GDELT never supplies `known_at`. **FNSPID dropped** (CC BY-NC-4.0, verified). | Tiingo news, bundled if Tiingo is bought | — |
| Statistical rigor | N, Q | **`arch` 8.0.0 — verified by reading the shipped source**: `optimal_block_length` cites Politis–White 2004 + Patton–Politis–White 2009; `SPA`, `StepM`, `MCS` present (block lengths differ from Patton's MATLAB reference by design). + **`statsmodels`** (two-way clustered SEs) + ~250 vendored lines (Wilson, method-of-moments empirical Bayes, calendar-time regression, purged K-fold). **`mlfinlab` verified not open source — never a dependency.** | Write purged CV yourself (~40 lines) rather than depend on `purgedcv` / `ml4t-diagnostic`, whose maintenance is unverified | Spec Q growing real ML factor work → `skfolio` |
| Remote MCP transport / auth | K | **Official `mcp` SDK mounted in FastAPI**, streamable HTTP, **OAuth 2.1 with dynamic client registration plus static bearer** — both accepted, one scope model. Verified: `mcp` 2.1.1 renames `FastMCP`→`MCPServer` and `streamablehttp_client`→`streamable_http_client`; the SDK's own guidance is `mcp<2` for v1 code. **The unbounded pin was a live bug and is fixed in this revision.** Codex bearer verified; Claude Code's documented path is OAuth. | — | — |

Verified cost (verification §35): **~$60–70/month** with Tiingo, **~$40–50/month** with
EODHD, in both cases Railway Pro $20 plus overage (Hobby's $5 credit will not cover an
always-on service plus Postgres). Sharadar is deliberately absent from the table until
its price is verified.

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
| **3. Measure it** | Spec N comparable-setups engine over the existing warmed event library | The core question gets a trustworthy answer |
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
2. **Sharadar, 15 minutes logged in:** price of the Core US Equities bundle, whether
   SEP retains delisted tickers with terminal prices, whether TICKERS carries a
   delisting reason, whether SF1 `datekey` is the filing date, and the redistribution
   terms. Everything the two research passes say about Sharadar is unverified.
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
