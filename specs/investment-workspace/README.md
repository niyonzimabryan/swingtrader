# SwingTrader Investment Workspace — spec series K–Q

**Status:** Draft v0.1, for owner decisions
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
| Broker of record, today | **Robinhood** via the connected MCP server | It is the account and interface that actually exists and is authenticated. |
| Second broker | **Schwab** adapter written to the same capability interface, shipped **stubbed and unauthenticated** | Schwab absorbed TD Ameritrade; its Trader API is the plausible second venue. No credentials are assumed. IBKR is deferred (Spec L §7). |
| Trade execution | **Agent proposes; Bryan approves; code executes.** No agent, local or cloud, may place an order. | Non-negotiable. Encoded in Spec L §6 and Spec P §5, not in prompts. |
| State of record | **Postgres on Railway**, with narrative research as **Markdown in the repo** | SQLite on a Railway volume is single-host and unreachable from a cloud session. This is the change that unlocks "accessible everywhere." |
| Hosting | Existing Railway project `e556a6d9-2023-4c81-a031-e32e160a33be`, one new web service for the workspace API/MCP | Reuses the deploy path already documented in the root `CLAUDE.md`. |
| Model spend | Deterministic-by-default. Every scheduled job is Python. LLM calls only on a human turn or an explicit synthesis step, each one budgeted and ledgered. | Bryan has already hit credit exhaustion once (BRY-301); the architecture should make that a nuisance, not an outage. |
| Live capital | Unchanged from Spec Q: one live champion, entry-by-entry approval, hard caps, kill switch | The Strategy Lab safety model is already correct and is not reopened here. |
| Repo mode | **Production.** Surgical, flagged, backward-compatible. | There is live-money code in this repo. Every spec below ships behind a flag defaulting off. |

**Assumption stated explicitly:** Bryan's instruction "if you have access to Robinhood
already pretend it's that, but make a Schwab" is read as *build against the live
Robinhood MCP now, and define the Schwab adapter to the same interface so it drops in
when credentials exist.* If that reading is wrong, only Spec L §7 changes.

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

## 5. Research slots — what the pending best-in-class research decides

A separate deep-research task (dispatched 2026-09-05, results not yet back at the time
of writing) is scanning open- and closed-source options. **Its findings change vendors
and libraries, not the architecture above** — every slot below sits behind an adapter
that already exists in `data/base_adapter.py` or is defined in these specs.

Do not treat the candidate names as endorsements. They are the shortlist the research
should score, not verified capabilities.

| Slot | Spec | Adoption criteria (in priority order) | Candidates for the research to score |
|---|---|---|---|
| Backtest / event-study engine | N, Q | Must reproduce `backtest/simulator.py` exit semantics exactly, or be rejected. Then: PIT correctness, speed, dependency weight. | keep-and-extend the existing simulator; `vectorbt`; `nautilus_trader`; `backtrader`; `zipline-reloaded` |
| Point-in-time equity price + corp-action data | N, O | Adjustment provenance and delisted-name coverage first. Cost second. | `Polygon`; `Databento`; `Tiingo`; `Nasdaq Data Link`; incumbent `FMP` |
| Point-in-time fundamentals | N | Availability timestamps (`known_at_utc`) present, or the source is exploratory-only. | `Sharadar SF1`; `S&P Compustat PIT`; incumbent `FMP` |
| SEC filings / entity resolution | O | CIK↔ticker history, amendment handling, full-text search. | `edgartools`; raw EDGAR full-text + `frames` API; `sec-api.io`; Robinhood MCP `get_sec_filing_facts` |
| Point-in-time macro | O | Vintage/as-published series, not revised. | `FRED` + **`ALFRED` vintages** (key already provisioned); Treasury direct |
| News with timestamps | O | Publication timestamp fidelity and dedup, not volume. | incumbent Gemini-search + Firecrawl; `Benzinga`; `Tiingo news`; Robinhood MCP `get_equity_news` |
| Statistical rigor library | N | Block bootstrap, multiple-testing, deflated Sharpe. | `arch` (bootstrap); `statsmodels`; hand-rolled per Spec N §6 |
| Remote MCP transport / auth | K | Works unmodified in Claude Code, Codex, and a cloud session. | `fastmcp` streamable-HTTP; hand-rolled FastAPI + MCP SDK |

When the research lands, the only edit required is this table plus the named adapter
in the relevant spec. **If a spec's design would change because of a research finding,
that is a bug in the spec** — say so and fix the boundary.

---

## 6. Delivery order

Sequenced so each phase is independently useful and nothing large lands before it can
be verified.

| Phase | Ships | Useful on its own because |
|---|---|---|
| **0. Foundation** | Alembic baseline (already required by Spec Q §14), Postgres migration, Railway workspace service skeleton | Unblocks every cloud session; ends the SQLite single-host trap |
| **1. See it** | Spec L portfolio ledger + Robinhood sync + Spec K read-only tool surface | "What do I own and what am I exposed to" answerable from anywhere |
| **2. Remember it** | Spec M dossiers/theses + Markdown mirror | Research survives the session it was done in |
| **3. Measure it** | Spec N comparable-setups engine over the existing warmed event library | The core question gets a trustworthy answer |
| **4. Widen it** | Spec O filings + macro vintages + news timestamps | Cohorts get better covariates; smart-money and macro views appear |
| **5. Race it** | Spec Q Strategy Lab (its own six-PR plan) | Champion vs challengers, on evidence |
| **6. Act on it** | Spec L §6 proposal→approval→execution path, live only per Spec Q §12 | Approved orders, with full protection lifecycle |

Phases 3 and 5 both depend on Phase 0; they do not depend on each other and can run in
parallel by different agents.

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
