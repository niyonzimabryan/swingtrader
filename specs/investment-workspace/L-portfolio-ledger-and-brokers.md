# Spec L — Portfolio ledger and broker adapters

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05
**Flags:** `PORTFOLIO_SYNC_ENABLED=false`, `BROKER_SCHWAB_ENABLED=false`

---

## 1. Problem

The system knows about the trades *it* made. It does not know what Bryan owns. A
research answer that ignores existing exposure is worse than no answer: "buy AMD" reads
differently when semiconductors are already 40% of the book. Nothing today can say what
the portfolio is, what it is concentrated in, or whether a proposed position duplicates
one that already exists.

## 2. Outcome

One canonical portfolio ledger, synced from every connected broker, that answers:

- what is held, in what quantity, at what basis, in which account;
- how much cash and buying power exists;
- what orders are open or recently filled;
- what the book is **exposed to** — by sector, factor, single name, and thesis;
- how stale each of those answers is.

And one execution path: propose → human approves → code executes → reconcile.

## 3. Domain model

New tables, Alembic-managed. All timestamps tz-aware UTC.

### `brokerage_accounts`
Broker slug, external account id, account type (`cash`/`margin`/`ira`), currency,
capability set (§5), enabled flag, last successful sync, last sync error.

### `holdings`
Account, instrument (ticker for equities; a typed instrument row for anything else),
quantity, average cost when the broker supplies it, market value, `as_of_utc`, source.
**Options are out of scope for analysis but never invisible:** a Robinhood account can
hold options, and a portfolio view that silently omits a short put is the worst failure
this table can have. Any non-equity position the sync sees is stored with
`instrument_type` and rendered as an `unsupported_instrument_present` warning with its
notional, in every overview, until options are modelled. **Point-in-time by append**, not by update: a sync writes a new row
and supersedes the previous one, so "what did I hold on date D" is answerable. A
nightly job collapses unchanged runs to keep the table small.

### `tax_lots`
Account, ticker, open date, quantity, cost basis, term (`short`/`long`), broker lot id,
and a per-account **booking method** from `{STRICT, FIFO, LIFO, AVERAGE, NONE}` — the
vocabulary borrowed from beancount, where `STRICT` means a sale must name its lots and
`NONE` means lots are not tracked. Populated where the broker exposes lots; when absent,
the field is null and every consumer must treat null as *unknown*, never as zero.

One derived flag, because it is cheap and a swing trader who trims and re-adds triggers
it constantly: **`wash_sale_window`** on a proposed buy that falls within the 61-day
window (30 days before, the sale day, 30 days after — off-by-one is the usual bug) of a
realised loss in a substantially identical security, per IRS Publication 550. The check
spans every Robinhood account the token can read, since the rule reaches across
accounts; it cannot see a spouse's or another broker's, and says so. Same-FIGI matches
are `high` confidence; same-issuer different-class or convertibles are `possible`; the
flag **never emits a determination** — "substantially identical" is a facts-and-
circumstances test, not a computation. An IRA purchase is flagged separately because
there the loss is permanently disallowed rather than deferred. Shown on the proposal
card. Basis and
the flag are **informational only**; this system does not compute taxes and does not
give tax advice.

### `cash_balances`
Account, settled cash, unsettled cash, buying power, `as_of_utc`.

### `broker_orders`
Account, broker order id, ticker, side, quantity, order type, limit/stop prices,
status, submitted/filled timestamps, average fill price, and the linking
`strategy_trades.execution_id` when the order originated inside the system. Orders
placed by Bryan in the Robinhood app appear here too, with a null execution link and
`origin='external'`.

### `portfolio_snapshots`
Daily rollup: total value, cash, gross/net exposure, per-sector and per-name weights,
largest positions, concentration metrics, and the hash of the inputs. This is what
Spec N uses when it reports "this setup would add to an exposure you already have."

Also, computed deterministically from stored daily returns (no vendor risk model):
portfolio beta to the broad benchmark over 60 and 250 sessions, realized portfolio
volatility, and the **maximum and average pairwise** return correlation among the
largest positions — not a full matrix, which over ten names and sixty sessions is mostly
noise. A portfolio of six "different" names with 0.8 pairwise correlation is one
position, and sector codes will not say so. Rendered with the lookback window and n
beside every figure. Later, not v1: a three-factor regression on Ken French's freely
published factors, which are point-in-time clean.

### `exposure_tags`
Free-form tags attached to holdings (`ai-infra`, `rate-sensitive`, `china-revenue`)
sourced from dossiers (Spec M). Exposure by *narrative* is the thing a sector code
cannot express and is usually where correlated risk actually hides.

## 4. Sync

A scheduled Python job, no inference:

1. For each enabled account, pull positions, cash, orders, and lots.
2. Normalize to the tables above; write append-style with a shared `sync_id`.
3. Reconcile: any position the system believes it opened that the broker does not
   report, or vice versa, raises a `reconciliation_required` event through the existing
   `tracking/position_reconciliation.py` path and pages the out-of-band channel.
4. Compute the daily `portfolio_snapshots` row after the close.

Cadence: hourly during market hours, on demand from any tool call that finds the
freshness budget exceeded, once after the close, once pre-market. Freshness budget is
60 minutes intraday; past that, every tool response carries `stale=true` (Spec K §4.2).
A swing-trading horizon does not need a 15-minute sync, and hourly is a quarter of the
API pressure on a broker surface whose limits are not published.

**Failure policy:** a broker that errors leaves the previous rows intact and marks the
account stale. It never zeroes a position. A sync that would delete more than 50% of
known holdings fails closed and pages instead of writing.

## 5. Broker adapters

Extends the existing `execution/brokers/base.py` interface rather than replacing it.

Every adapter declares a **capability set**, and callers check capabilities before
intent — the rule already established in Spec Q §12:

```python
@dataclass(frozen=True)
class BrokerCapabilities:
    can_read_positions: bool
    can_read_tax_lots: bool
    can_read_orders: bool
    can_place_equity_market: bool
    can_place_equity_limit: bool
    can_place_attached_stop: bool      # a protective exit that survives our process
    can_place_bracket: bool
    supports_fractional: bool
    market_hours_only: bool
```

`can_place_attached_stop` is the decisive one. **If a broker cannot place a protective
exit that outlives our process, no unattended entry may be opened on it** — Spec Q §12
already states this and it is restated here because it is the single most expensive
mistake available. An in-process watcher is not protection; it disappears with the
process.

### 5.1 Robinhood — the live adapter

Implemented against the connected Robinhood MCP server. `execution/brokers/robinhood.py`
and the encrypted token store (`docs/ROBINHOOD_TOKEN_STORE.md`) already exist and are
extended, not rewritten.

**Which server — resolved.** The repo already talks to the *official* Robinhood
Trading MCP (`agent.robinhood.com/mcp/trading`, set in `config/settings.py`) over the
MCP SDK's OAuth provider; the nine tools `execution/brokers/robinhood.py` calls are all
on Robinhood's published list (verification §6). No unofficial server was ever used;
the v0.1 tool names were a drafting error. Facts verified from Robinhood's own support
pages (verification §1–§5), and what they leave open:

- **Beta, not GA.** The May 2026 launch post says "launching in beta"; no exit-from-beta
  statement exists. There is no developer documentation: no order-type table, no
  `place_equity_order` schema, no rate limits, no token lifetime, no SLA, no
  deprecation policy. Onboarding and re-auth are desktop-only. The documented recovery
  path for a broken connection is a human reconnect.
- **Placement is confined to the Agentic account**, verbatim: "Your agent can only place
  trades in your Robinhood Agentic account." Reads span all accounts. No published
  balance cap. **The Agentic account is a cash account by owner decision (2026-09-08),
  funded with a budget Bryan loads; no margin of any kind.** Cash settles T+1 —
  proceeds from a Monday close are not redeployable until Tuesday — so the ledger
  tracks settled versus unsettled cash separately, a proposal that would need
  unsettled funds is created `risk_rejected` with the settlement date, and the
  simulator models the T+1 redeploy delay (Spec N §5.3).
- **Tools exist for** equity positions, `get_equity_tax_lots`, `get_realized_pnl`,
  `get_pnl_trade_history`, orders, quotes, tradability, `review_equity_order`,
  `place_equity_order`, `cancel_equity_order`, and the same for options and crypto.
  Options are readable **and tradeable**; the workspace never uses the option or crypto
  write tools (import-graph test).
- **No tool exposes dividends received, cash movements, deposits, fees, or corporate
  actions on the account.** `get_equity_fundamentals` carries security-level dividend
  metadata, not receipts. So the ledger's cash and total-return accounting reconciles
  dividends from a market-data dividend feed applied to holdings, flagged
  `reconstructed`, until Robinhood exposes them.
- **Order semantics — verified from the server's own `tools/list` schema, 2026-09-08**
  (`docs/robinhood/tool_schemas.json`, 73 tools; dumped with
  `scripts/dump_robinhood_tool_schemas.py`):
  - `type` is one of `market`, `limit`, `stop_market`, `stop_limit`. **There is no
    bracket, OCO, OTO, or attach-stop parameter.** A protective stop is a separate
    order placed after the fill. `can_place_attached_stop` is therefore **false** and
    stays false; the capability that matters is `can_place_standalone_gtc_stop`, which
    is **true**.
  - `time_in_force` is `gfd` or `gtc` (default `gfd`). Protective stops are placed
    `gtc`. How long Robinhood keeps a GTC order open is not stated anywhere; the
    execution service re-reads open orders daily and re-places a stop that has
    disappeared, and a position whose stop cannot be read back pages (§6).
  - `stop_market` and `stop_limit` are **regular-hours only** (rejected in extended or
    all-day sessions) and **whole-share only** — fractional quantities are allowed for
    `market` + `regular_hours` and nothing else. Consequence: **every protected entry
    is a whole-share order**; `dollar_amount` entries are never used for positions the
    system is responsible for protecting (§6.6 rounds down).
  - `ref_id` is a client idempotency key the upstream deduplicates on. Every order the
    execution service places carries one generated once per logical order, so the
    fill-to-stop race can retry safely.
  - `tax_lots` allows specified-lot selling (up to 30 lots, from `get_equity_tax_lots`)
    on plain sells, **not** on stop orders or `dollar_amount` or all-day sessions. So the
    booking method for discretionary sells can be `STRICT`; protective exits sell FIFO
    and the ledger records that.
  - `account_number` must be `agentic_allowed=true`; the API rejects any other account.
    The boundary in README §3 is enforced by Robinhood, not only by us.
- **Unattended session survival — one data point so far.** The access token is issued
  with roughly a 6.7-day lifetime and a refresh token alongside it. A token store left
  idle from June to September **did not survive**: the refresh was rejected and a
  desktop browser re-auth was required. Whether a *continuously refreshing* service
  survives is still unknown, so the second Phase 1 checkpoint runs the sync on Railway
  with no human for 30 days and logs every refresh with timestamps. Until then, every
  read path asserts freshness and **refuses rather than serves** when `as_of_utc` is
  older than the budget on any path that feeds a proposal — a ledger that silently
  stops syncing is worse than one that loudly breaks.

**The Agentic-account boundary — decided (README §3): use the Agentic account.** Live
entries under §6 are placed there; the primary account is read-only to the workspace.
Two consequences the code must honour: every risk check in §6 (concentration, sector,
daily notional, drawdown) runs over the **combined** book across both accounts, never
the Agentic account in isolation — otherwise a name held in the primary account is
invisible to the cap on adding to it; and `brokerage_accounts` carries an
`agent_placeable` flag so a proposal targeting a read-only account is created
`risk_rejected` with that reason rather than failing at placement.

Work required:
- Read paths for positions, lots (where exposed), cash, and orders → the tables in §3,
  across every account the token can see.
- Capability probe at startup, recorded on `brokerage_accounts`, re-probed daily.
- **The protective exit is a separate `stop_market` order, `gtc`, whole shares, placed
  after the fill** — settled by the schema above. The execution service owns the race:
  entry fills → stop placed with its own `ref_id` → stop **read back from the broker
  via `get_equity_orders`** → only then is the position `protected`; a position filled
  and not read back as protected within the configured window pages immediately and
  blocks further entries. A daily job re-reads open orders and re-places any stop that
  is no longer open, so a 20-session hold cannot outlive its stop whatever Robinhood's
  unstated GTC horizon is. What remains empirical, and is the live probe in Phase 6:
  that a `gtc` `stop_market` placed through the MCP is visible in `get_equity_orders`
  the next session and triggers when touched. Record the result in
  `docs/ROBINHOOD_INTEGRATION_PLAN.md`. Until the probe passes, live entries stay closed.
- Use the broker's own `review_equity_order` as the pre-trade snapshot in §6 rather than
  reimplementing its price collar.

### 5.2 Schwab — deferred (owner decision 2026-09-05)

Not built in this series. What Phase 1 ships instead is the thing that makes a second
adapter cheap later: the `BrokerCapabilities` contract, a fake broker, and contract
tests every adapter must pass. When Bryan asks for Schwab, the work is
`execution/brokers/schwab.py` against that contract plus its auth flow, and nothing
upstream changes. No Schwab API behaviour is asserted in code or docs until then.

The deferral is also the right call on the evidence: Schwab's Trader API refresh token
expires after **seven days** and renewing it requires an interactive browser login that
`schwab-py` cannot perform unattended. For a Railway service that is a weekly manual
re-auth, forever. Revisit only if Schwab changes it.

### 5.3 Alpaca — unchanged

Remains the paper venue, per Spec Q §11. A paper arm may reach only the Alpaca paper
adapter regardless of the primary broker setting.

## 6. Execution path — the boundary that matters

```text
agent session ──propose_order──► broker_orders row (status='proposed')
                                          │
                                          │  approval card to owner over the
                                          │  out-of-band channel (Telegram today,
                                          │  signed /admin page later) — never
                                          │  a tool an agent can call
                                          ▼
                                   owner approves
                                          │
                                          ▼
                            execution service (not an agent)
                            risk check → reserve → place → verify protection
                                          │
                                          ▼
                                    reconcile + ledger
```

Enforced invariants (in addition to every invariant in Spec Q §12):

1. **No agent-reachable code path calls a broker placement method.** Asserted by an
   import-graph test, not by prompt instruction. The MCP surface has no execute tool
   and no token scope grants one (Spec K §4.1).
2. A proposal carries the full portfolio context hash it was computed against. Risk is
   **re-evaluated from fresh state at approval time**, never reused from the proposal.
3. Approval is per-order, single-use, expiring, and owner-bound. A thesis approval, a
   strategy promotion, and a prior approval are none of them an approval of this order.
4. A proposal that would breach concentration, sector, daily-notional, or drawdown
   limits is created in `risk_rejected` and shown with the reason, so the agent can
   explain *why* rather than silently omitting the idea.
5. The kill switch (`/live_kill on`) blocks approval-to-placement and survives restart.
6. **An agent never chooses a quantity.** `propose_order` carries `entry`, `stop`, and a
   `risk_fraction` of equity (bounded by config, default 0.5%, hard cap 1%); the
   execution service computes size as `risk_dollars / (entry − stop)`, **rounded down to
   whole shares** (Robinhood accepts fractional quantities only on unprotected market
   orders, §5.1), then applies the concentration, sector, and daily-notional caps. A
   size that rounds to zero shares is `risk_rejected` with the reason. **Evidence scaling, specified:**
   - A proposal carries at most one `cohort_answer_id`. If present it must resolve to a
     `depth="full"` answer for the same ticker, computed within the last 5 sessions,
     with `status="ok"`; otherwise the proposal is created `risk_rejected` with the
     reason. An `insufficient` or `inconclusive` citation is not a citation.
   - Let `LB` and `PE` be the lower 90% bootstrap bound and point estimate of the
     **policy-simulated net return** (§5.3 of Spec N) at the horizon nearest the
     proposal's expected hold, both as fractions (a percentage input is rejected, not
     converted). For an **evidenced** proposal (`status="ok"`, `PE > 0`, `LB > 0`) the
     multiplier is `m = clip(LB / PE, 0, 1)` and `risk_fraction_effective =
     risk_fraction × m`, drawn from the evidenced budget.
   - **Two budgets, advisory mode (owner decision 2026-09-08).** Everything that is not
     evidenced — an uncited proposal, a citation with `status="insufficient"`, or a
     citation with `LB ≤ 0` — is re-labelled **`discretionary`**, draws from a separate
     budget (`DISCRETIONARY_RISK_CAP` per trade, default 0.25%; `DISCRETIONARY_DAILY_NOTIONAL`),
     and carries the evidence on the card in full, including a negative lower bound
     when there is one. **Nothing is sized to zero on evidence alone.** The two budgets
     are separate rows in the ledger and the journal, so "how do my judgment trades do
     versus my evidenced trades" is a query, which is the point. `EVIDENCE_GATE_MODE`
     is `advisory` by default; `strict` makes `LB ≤ 0` size to zero and refuses uncited
     proposals, with no code change.
   - The card shows `risk_fraction`, `m`, `LB`, `PE`, the horizon used, and every cap
     that bound the final size. The engine outputs a distribution; sizing from its
     mean alone throws away the thing that makes it valuable. Sizing dominates selection in realised
   P&L and nothing in v0.1 said where a quantity came from. Kelly-style sizing is
   explicitly not used: it needs an edge estimate this system has just spent Spec N
   proving is uncertain, and at these sample sizes half-Kelly is still a guess. The
   computed size and every cap that bound it are shown on the approval card.

**Assistant boundary, stated plainly:** an AI assistant operating this workspace does
not place trades and does not give personalized investment advice. It assembles
evidence, shows exposure math, and prepares proposals that Bryan decides on. That is a
property of the architecture above, not a policy in a prompt.

## 7. Interactive Brokers

Deferred. IBKR is a separate integration with its own gateway/session model, not a
Schwab or TD substitute — its OAuth direct connection is institutional-only, and retail
access runs through a local Client Portal Gateway process that must be kept alive, which
is the worst unattended-session model of the three for this architecture. Revisit only after Robinhood read+protect is proven and the
Schwab decision is resolved. Tracked in `todoscratchpad.md` under Future Improvements
where it already sits.

## 8. Test plan

| Test | Asserts |
|---|---|
| `test_sync_is_append_only` | Historical holdings remain queryable after a sync |
| `test_sync_never_zeroes_on_error` | A broker exception leaves prior rows and sets stale |
| `test_mass_deletion_fails_closed` | A sync dropping >50% of holdings pages and writes nothing |
| `test_unknown_basis_is_null_not_zero` | Consumers of null basis raise or flag, never compute |
| `test_capabilities_gate_intent` | An order requiring an attached stop is refused on an adapter declaring `can_place_attached_stop=False`, before placement |
| `test_no_agent_path_to_broker` | Import-graph: nothing reachable from the MCP surface imports a placement method |
| `test_approval_is_single_use` | A replayed approval callback is rejected |
| `test_risk_recomputed_at_approval` | A proposal valid at creation is refused when fresh state breaches a limit |
| `test_external_orders_ingested` | An order placed outside the system appears with `origin='external'` |
| `test_fake_broker_contract` | The fake broker satisfies every `BrokerAdapter` method and every capability declaration path, so a future adapter has a contract to pass |
| `test_options_never_silently_omitted` | A synced account holding an option renders `unsupported_instrument_present` with notional in `portfolio_overview` |
| `test_wash_sale_window_flagged` | A proposed buy 20 days after a realised loss in the same name carries `wash_sale_window=true`; 40 days does not |
| `test_agent_cannot_set_quantity` | `propose_order` rejects a `quantity` argument; size is derived from `risk_fraction`, entry and stop |
| `test_risk_fraction_capped` | A `risk_fraction` above the hard cap is refused, not clamped silently |
| `test_citation_must_be_full_ok_recent` | A `quick`, `insufficient`, `inconclusive`, stale, or other-ticker citation lands the proposal in `risk_rejected` |
| `test_nonpositive_lower_bound_goes_discretionary` | In `advisory` mode `LB ≤ 0` re-labels the proposal `discretionary`, prints the bound on the card, and draws from the discretionary budget; in `strict` mode it sizes to zero |
| `test_discretionary_budget_is_separate` | An uncited proposal cannot exceed `DISCRETIONARY_RISK_CAP` or push discretionary daily notional past its cap, and never consumes the evidenced budget |
| `test_unsettled_cash_rejected` | On the cash Agentic account a proposal needing T+1 proceeds is `risk_rejected` with the settlement date |
| `test_percentage_input_rejected` | `risk_fraction=0.5` meaning 0.5% is refused as out of range; `0.005` is accepted |
| `test_attached_stop_verified_at_broker` | The protection probe passes only when the `gtc` `stop_market` is readable from `get_equity_orders` after entry |
| `test_protected_entries_are_whole_shares` | A protected entry never carries a fractional `quantity` or a `dollar_amount`; a size rounding to zero is `risk_rejected` |
| `test_stop_orders_regular_hours_gtc` | Every protective stop is `stop_market`, `regular_hours`, `gtc`, with a `ref_id` |
| `test_missing_stop_replaced_daily` | An open position whose stop is absent from `get_equity_orders` gets a new stop placed and a page |
| `test_unprotected_fill_pages` | A fill whose stop is not read back within the window raises a page and blocks further entries |
| `test_stale_ledger_refuses_proposal` | `propose_order` against holdings older than the freshness budget is refused with the age, not served |
| `test_no_option_or_crypto_write_tools` | Import-graph: no code path references `place_option_order`, `place_crypto_order`, or their review/cancel tools |
| `test_dividends_reconstructed_flagged` | A dividend cash flow derived from a market-data feed carries `reconstructed=true` |
| `test_t1_settlement_modelled` | On a cash Agentic account, proceeds from a close are unavailable to a same-day proposal |
| `test_risk_caps_span_all_accounts` | A concentration cap counts the primary account's holding when sizing an Agentic-account proposal in the same name |
| `test_proposal_on_readonly_account_rejected` | A proposal targeting an account with `agent_placeable=False` lands in `risk_rejected` with the reason |

## 9. Definition of done

- `portfolio_overview` returns holdings, cash, exposure, and freshness for every enabled
  account, correct against a hand-check of the Robinhood app on one trading day.
- Reconciliation raises on a deliberately induced mismatch and pages.
- Robinhood's protective-exit capability is documented with evidence either way.
- The 30-day unattended refresh log has started on Railway (the schema dump is done:
  `docs/robinhood/tool_schemas.json`).
- The import-graph test proves no agent path to a broker placement.
