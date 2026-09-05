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
it constantly: **`wash_sale_window`** on a proposed buy that falls within 30 days either
side of a realised loss in the same name. It is shown on the proposal card. Basis and
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
   `tracking/position_reconciliation.py` path and pages Telegram.
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

**Which server, exactly — resolve before Phase 1.** Robinhood ships an *official*
agentic MCP endpoint (`agent.robinhood.com/mcp/trading`, OAuth, GA mid-2026 per the
research; unverified from inside this session). It reads positions, balances, orders
and quotes across accounts, and trades through `review_equity_order` →
`place_equity_order` → `cancel_equity_order` with market/limit/stop/trailing types.
**Order placement is confined to a separately funded "Agentic" account; every other
account is read-only to agents.** The tools named in earlier drafts of this series
(`get_sec_filing_facts`, `get_equity_news`, tax lots, realized P&L) match *unofficial*
community servers that log in through a browser session — a materially different
security posture. The first Phase 1 checkpoint records in
`docs/ROBINHOOD_INTEGRATION_PLAN.md` which server is connected, its exact tool list,
and its auth model, with evidence.

**The Agentic-account boundary is a product decision, not a Phase 6 discovery.** If
Bryan's positions live in the primary account, the propose→approve→execute path in §6
can never act on them through the official server. The options are: fund the Agentic
account and run live entries there; or accept that execution stays manual in the app
while the workspace does everything up to the proposal. README §3 carries the decision;
this spec builds the same read paths either way.

Work required:
- Read paths for positions, lots (where exposed), cash, and orders → the tables in §3,
  across every account the token can see.
- Capability probe at startup, recorded on `brokerage_accounts`, re-probed daily.
- **Determine empirically** whether a protective exit survives our process. Stop and
  trailing order *types* exist on the official surface; no evidence was found of a
  bracket or OCO stop attached to the entry. A standalone stop placed after the fill is
  probably sufficient, but it is a second order that can fail independently, so the
  probe asserts that the stop **exists at the broker after entry** — not that the call
  returned success. Record the finding in `docs/ROBINHOOD_INTEGRATION_PLAN.md` with
  evidence. Until proven, `can_place_attached_stop=False` and live entries stay closed.
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
                                          │  Telegram card to owner,
                                          │  signed + expiring reference
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
   execution service computes size as `risk_dollars / (entry − stop)`, then applies the
   concentration, sector, and daily-notional caps. Sizing dominates selection in realised
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
| `test_attached_stop_verified_at_broker` | The capability probe passes only when the stop is readable from the broker after entry |

## 9. Definition of done

- `portfolio_overview` returns holdings, cash, exposure, and freshness for every enabled
  account, correct against a hand-check of the Robinhood app on one trading day.
- Reconciliation raises on a deliberately induced mismatch and pages.
- Robinhood's protective-exit capability is documented with evidence either way.
- Which Robinhood MCP server is connected, its tool list, and its auth model are
  recorded with evidence, and the Agentic-account decision is in README §3.
- The import-graph test proves no agent path to a broker placement.
