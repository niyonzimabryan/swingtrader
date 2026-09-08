# Investment Workspace — Quick Read

*For Bryan, three months from now, or a smart friend catching up.*

## What it is

SwingTrader stops being "a Telegram bot that scans and scores tickers" and becomes a
cloud-hosted investment workspace any agent session can attach to — Claude on the web,
Claude Code, Codex, or Cursor, local or cloud (README §1). It holds four things a chat
transcript cannot: the portfolio, the research, the evidence (point-in-time facts,
comparable setups, filings, macro, news), and the experiments (versioned strategies
measured against each other). An agent session is the interface, not the system of
record — durable state lives in a database and in the repo, not a conversation (README
§1).

## The one question, and why it's hard to answer honestly

The system exists to answer one question well: *given this setup, how have setups
genuinely like it performed — measured honestly, with the sample size, the benchmark
subtracted, and the uncertainty shown?* (Spec N §1). Producing *a* number is easy;
producing one that isn't misleading is hard, because there are well-known ways to fool
yourself (Spec N §2): **survivorship** (a sample built from today's tickers never
includes the ones that went to zero); **lookahead** (using a restated earnings number
or revised GDP print that didn't exist yet on the day studied); **"the market did it"**
(calling "stocks like this went up 4%" an edge when the whole market went up 4% that
stretch); and **clustering** (fifty "independent" events around four earnings days are
really four observations in fifty costumes, which makes intervals look tighter than
they are). Every design choice that follows defends against one of twelve named failure
modes like these (Spec N §2).

## The four things it holds

**Portfolio** — one ledger, synced hourly from every broker: what's held, at what
basis, exposed by sector and name, and how stale each answer is. Ask what you own and
what a new position adds to exposure you already have (Spec L §2).

**Research** — durable dossiers and theses that survive the session they were written
in: what's believed, why, and what would prove it wrong. A thesis can't go "active"
without one machine-checkable invalidator (Spec M §2, §4).

**Evidence** — point-in-time facts: SEC filings, insider trades, macro data as it was
actually known on a given day rather than revised with hindsight, and timestamped news
— the raw material the comparable-setups engine runs on (Spec O §1).

**Experiments** — the Strategy Lab: several versioned strategies running side-by-side,
measured against a champion, before any touches real money (Spec Q §1).

## How you use it

Claude on the web, Claude Code, Codex, and Cursor — local or cloud — attach to the same
workspace over one authenticated connection, so a question gets the same answer
regardless of client (Spec K §2, §4.3). A typical question runs a fixed loop: recall
what's already known, frame the question and show that framing before running it,
gather evidence, send the resulting thesis to a "critic" subagent whose only job is to
attack it, check it against existing exposure, and write the outcome back (Spec P §3).

Trading follows a strict path: an agent can only *propose* a trade; you approve it
through a channel no agent can call (Telegram today); only then does ordinary code —
never an agent — place the order, verify the stop is attached at the broker, and
reconcile (Spec L §6). Nothing scheduled ever calls a model — sync, pricing, and
statistics jobs are plain Python (README §3; Spec K §5) — and by construction, not by
prompt, no agent-reachable code path can place an order and no model output is ever the
statistic in an answer (README §4, boundaries 1–2).

## What it costs

"Cost" means two things here. One is the **monthly bill**: a $100/month ceiling on
data subscriptions, verified stack around $40–50/month — Railway hosting plus one paid
price feed, Sharadar's Prices tier at $9/month (README §3, §5). The other is the **trading cost**
subtracted from every headline number the engine reports — an estimated half-spread
plus slippage, so a base rate is never quoted as if trading were free (Spec N §5.3).

## What's decided, what's open

Most of the shape is locked in: Robinhood is the broker (confirmed), Postgres on
Railway is the state of record, trade execution always requires human approval, and the
interface is MCP/REST only — no CLI, no Telegram surface beyond approvals (README §3).
The data decision is made: free sources for everything with a free primary (SEC, FRED,
Alpaca news, S&P membership history), and Sharadar's Prices tier at $9/month as the one
paid feed, subject to a delisting-completeness audit before paying (README §3). The
Agentic account is a cash account with no margin, funded with a budget Bryan loads. And
evidence is advisory, not a gate: a trade without supporting evidence draws from a
separate discretionary budget and is labelled as such, rather than being blocked.

Three cheap, owner-only actions remain (README §9): run the schema-dump script against
Robinhood to learn what order and stop types are supported; confirm what Sharadar's
"from $9" tier includes at checkout; and run the twenty-delisting audit before paying.

## Glossary

- **Point-in-time** — using only facts actually knowable on the date in question.
- **`known_at_utc`** — timestamp on a fact recording when it could first have been
  known; without one, the fact is quarantined (README §4).
- **Cohort** — historical setups matched as comparable to the one asked about (Spec N
  §3–§4).
- **CAR** — cumulative abnormal return: a setup's return minus the market's, summed
  over the holding period (Spec N §5.0).
- **Calendar-time portfolio** — the engine's primary return measure, pooling every
  event still "in window" on each calendar day rather than treating clustered events
  as independent (Spec N §5.0, §6.2).
- **`insufficient`** — a valid, expected answer meaning the sample doesn't clear the
  size floor; never softened into a hedge (Spec N §8).
- **Agentic account** — the separate Robinhood account the workspace may place live
  trades in; the primary account stays read-only (README §3).
- **Evidence tier vs. status** — tier says how good the data is (`clean_pit` /
  `vendor_pit` / `archival_reconstructed`); status says whether an answer exists at all
  (`ok` / `inconclusive` / `insufficient`) (Spec N §8).
