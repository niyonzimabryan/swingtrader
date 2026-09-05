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
Account, ticker, quantity, average cost when the broker supplies it, market value,
`as_of_utc`, source. **Point-in-time by append**, not by update: a sync writes a new row
and supersedes the previous one, so "what did I hold on date D" is answerable. A
nightly job collapses unchanged runs to keep the table small.

### `tax_lots`
Account, ticker, open date, quantity, cost basis, term (`short`/`long`), broker lot id.
Populated where the broker exposes it — Robinhood's MCP surface includes an equity
tax-lot endpoint. When absent, the field is null and every consumer must treat null as
*unknown*, never as zero. Basis is **informational only**; this system does not compute
taxes and does not give tax advice.

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

Cadence: every 15 minutes during market hours, once after the close, once pre-market.
Freshness budget is 20 minutes intraday; past that, every tool response carries
`stale=true` (Spec K §4.2).

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

Implemented against the connected Robinhood MCP server, which exposes accounts,
portfolio, equity positions, tax lots, orders, realized P&L, and order review/placement
tools. `execution/brokers/robinhood.py` and the encrypted token store
(`docs/ROBINHOOD_TOKEN_STORE.md`) already exist and are extended, not rewritten.

Work required:
- Read paths for positions, lots, cash, and orders → the tables in §3.
- Capability probe at startup, recorded on `brokerage_accounts`, re-probed daily.
- **Determine empirically** whether an attached protective exit is available and
  survivable. Record the finding in `docs/ROBINHOOD_INTEGRATION_PLAN.md` with evidence.
  Until that is proven, `can_place_attached_stop=False` and live entries stay closed.

### 5.2 Schwab — the stubbed second adapter

`execution/brokers/schwab.py` implements the same interface with every method raising
`BrokerNotConfigured` until credentials exist. Ships with:
- the capability set declared as all-`False`,
- a documented auth flow outline in `docs/SCHWAB_INTEGRATION_PLAN.md`,
- contract tests that run against the fake broker so the adapter is provably
  interface-complete before any credential exists.

No Schwab API behaviour is asserted in code or docs without verification against their
current documentation. TD Ameritrade's platform was absorbed into Schwab; the account
and API situation must be confirmed by Bryan before the adapter is wired.

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

**Assistant boundary, stated plainly:** an AI assistant operating this workspace does
not place trades and does not give personalized investment advice. It assembles
evidence, shows exposure math, and prepares proposals that Bryan decides on. That is a
property of the architecture above, not a policy in a prompt.

## 7. Interactive Brokers

Deferred. IBKR is a separate integration with its own gateway/session model, not a
Schwab or TD substitute. Revisit only after Robinhood read+protect is proven and the
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
| `test_schwab_interface_complete` | The stub satisfies every `BrokerAdapter` method against the fake broker |

## 9. Definition of done

- `portfolio_overview` returns holdings, cash, exposure, and freshness for every enabled
  account, correct against a hand-check of the Robinhood app on one trading day.
- Reconciliation raises on a deliberately induced mismatch and pages.
- Robinhood's protective-exit capability is documented with evidence either way.
- The Schwab adapter is interface-complete, credential-free, and disabled.
- The import-graph test proves no agent path to a broker placement.
