# Portfolio ledger

*Spec L, Phase 1. Flag: `PORTFOLIO_SYNC_ENABLED=false`.*

One canonical record of what is held, in which account, at what basis, with how
much cash — and how stale each of those answers is. It is filled by a scheduled
read-only sync from every connected broker, and it is read by three MCP tools on
the workspace service.

Nothing in this system places an order. That is a property of the import graph,
not of a policy: `tests/test_portfolio_import_graph.py` asserts that nothing
reachable from the workspace can reach a broker adapter, and that no module
anywhere names an option or crypto write tool.

---

## The tables

Seven, all in `migrations/versions/0004_portfolio_ledger.py`, all timestamps
naive UTC.

| Table | Holds | Append-only |
| --- | --- | --- |
| `brokerage_accounts` | broker, masked account id, `account_type`, booking method, `agent_placeable`, declared capabilities, last sync id/time/error | no — it describes an account, not an observation |
| `holdings` | one position as one sync saw it: quantity, basis, market value, `instrument_type`, notional | **yes** |
| `tax_lots` | broker lots where the broker exposes them, with `booking_method` | **yes** |
| `cash_balances` | settled vs unsettled cash, buying power, the pending T+1 tranches | **yes** |
| `broker_orders` | every order at the broker, with `origin` (`external`/`system`) | no — an order has a broker-side lifecycle |
| `portfolio_snapshots` | the daily rollup plus beta, realized vol, max/avg pairwise correlation | one row per date, recomputed in place |
| `exposure_tags` | exposure by narrative (`ai-infra`, `rate-sensitive`), attached by symbol | retired, not deleted |

**Append-only means append-only.** A sync inserts new rows and stamps
`superseded_at` and `superseded_by_sync_id` on the ones it replaces. Nothing is
updated in place and nothing is deleted, so "what did I hold on date D" is a
filter rather than a backup restore. Current state is `superseded_at IS NULL`.

Every row a run writes shares that run's `sync_id`.

No foreign key leaves the set, and `broker_orders.execution_id` names
`strategy_trades.execution_id` (Spec Q, Phase 5) as a plain string rather than a
foreign key — the column can be written before the table it refers to exists,
and the integration merge with Phase 2 stays a no-op join.

## Sync

`python -m scripts.portfolio_sync` runs one. The bot process registers the
cadence when `PORTFOLIO_SYNC_ENABLED=true`:

| Job | When (ET) |
| --- | --- |
| `portfolio_sync_pre_market` | 08:00, weekdays |
| `portfolio_sync_intraday` | on the hour, 09:00–15:00, weekdays |
| `portfolio_sync_post_close` | 16:30, weekdays — also writes the daily snapshot |

Weekday cron plus a holiday check at fire time, because a cron expression
cannot express "not Thanksgiving". The closure table is
`utils/market_hours.py`, shared with the monitor and the scan scheduler.

Hourly, not every fifteen minutes: a swing-trading horizon does not need it, and
hourly is a quarter of the pressure on a broker API whose rate limits are not
published anywhere.

### Failure policy

Three rules, and they are the reason `portfolio/sync.py` is longer than its
happy path:

1. **Never zero on error.** A broker or an account that raises leaves every
   previous row intact and is marked stale with the error text. Writing the
   empty result of a failed read would turn a network blip into "you own
   nothing" — a view that passes every schema check and is catastrophic to act
   on.
2. **Fail closed on mass deletion.** A sync that would supersede more than
   `PORTFOLIO_MASS_DELETION_THRESHOLD` (default 50%) of an account's known
   holdings writes **nothing for that account** and pages. Real liquidations
   happen and are indistinguishable from a partly-successful read from inside
   this process, so a human unblocks it:
   `python -m scripts.portfolio_sync --force-mass-deletion`. Failing closed is
   per account: an account that read cleanly still syncs.
3. **Reconcile and page, do not repair.** A position this system believes is
   open that the broker does not report — or a broker position with no matching
   open trade in an agent-placeable account — pages `reconciliation_required`.
   Repair stays in `tracking/position_reconciliation.py`.

## Freshness

Budget: **60 minutes** (`PORTFOLIO_FRESHNESS_BUDGET_MINUTES`). Two rules that
point in opposite directions, on purpose:

- **Reads flag.** Every read tool returns a `provenance` block —
  `as_of_utc`, per-field `sources`, `stale`, `data_quality`
  (`fresh`/`stale`/`unknown`), `age_minutes`, `freshness_budget_minutes` — and
  past the budget it returns the data **with `stale: true`**. Never a silent
  guess, never an exception the agent papers over (Spec K §4.2).
- **Proposals refuse.** `portfolio.freshness.require_fresh` raises
  `StaleLedger` with the age, and `portfolio.guards.check_ledger_fresh` returns
  a `risk_rejected` reason. `propose_order` is Phase 6; the guard ships now
  because a ledger that has quietly stopped syncing is worse than one that
  loudly breaks, and the two failures do not cost the same.

`as_of_utc` for the whole ledger is the **oldest** current stamp across
accounts, not the newest: an overview is only as fresh as its stalest account.

**A read tool cannot trigger a sync.** Spec L §4 wants one on a budget miss, but
syncing means reaching a broker adapter and the workspace may not import one.
So a stale response names the sync that would fix it and the sync runs in the
bot process or from `scripts/portfolio_sync.py`. This is the one place where
Spec L §4 and Spec L §6.1 pull against each other, and §6.1 wins.

## What Robinhood does and does not expose

Verified 2026-09-08 from the server's own `tools/list` schema, not from
documentation — there is none, and the schema dump itself is not committed to
this repository. Details, evidence, and what is still unverified in
[`ROBINHOOD_INTEGRATION_PLAN.md`](ROBINHOOD_INTEGRATION_PLAN.md).

**Exposed and used:** `get_accounts`, `get_portfolio`, `get_equity_positions`,
`get_equity_tax_lots`, `get_equity_orders`, `get_option_positions`,
`get_realized_pnl`, `get_pnl_trade_history`, `get_equity_quotes`.

**Not exposed at all:** dividends received, cash movements, deposits, fees, and
corporate actions. `get_equity_fundamentals` carries security-level dividend
metadata — the declared rate for the instrument — not a receipt for the account.
So dividend cash flow is **reconstructed** from a market-data dividend feed
applied to the holdings this ledger recorded, and every row it produces carries
`reconstructed: true` with the reasons it can be wrong (an intraday trade on the
record date, withholding, ADR fees, reinvestment, a late special dividend).
There is no code path that clears the flag.

**Placement, for the record, since this phase places nothing:** confined to the
Agentic account (`agentic_allowed=true`); `type` is one of `market`, `limit`,
`stop_market`, `stop_limit` with **no bracket, OCO, OTO, or attached stop**;
`stop_market`/`stop_limit` are regular-hours and whole-share only.
`BrokerCapabilities.can_place_attached_stop` is therefore `False` and stays
false, and `can_place_standalone_gtc_stop` is the capability that carries the
protection.

**Options are readable and never omitted.** An option position is stored in
`holdings` with `instrument_type='option'` and its contract notional, and every
overview renders it as `unsupported_instrument_present` with that notional. Out
of scope for analysis is not the same as invisible: a portfolio view that
silently drops a short put is the worst failure this ledger has.

**Cost basis may be missing, and missing stays missing.** A null basis is
*unknown*, never zero. `portfolio.guards.require_cost_basis` raises rather than
returning a zero; the read tools carry `cost_basis_known` beside every basis and
report unrealized P&L as `null`.

## The Agentic account is a cash account

Owner decision, 2026-09-08: no margin of any kind. So proceeds settle **T+1** —
a Monday sale is redeployable Tuesday — and the ledger tracks settled and
unsettled cash separately with the pending tranches and their settlement dates.
`portfolio.guards.check_settled_cash` refuses a proposal that would need
unsettled funds **and names the date they arrive**, because "not until Tuesday"
is actionable and "insufficient funds" is not.

Settlement dates are the next *trading* day: a Friday sale settles Monday, and a
sale before a holiday skips it.

## Exposure spans every account

Reads span every account the token can see; placement is confined to the
Agentic one. Every aggregate — concentration, sector, tag — therefore runs over
the **combined** book. A cap computed over the Agentic account alone would be
blind to the same name held in the primary account, which is exactly the
position that makes adding to it a bad idea. `brokerage_accounts.agent_placeable`
is what makes a proposal against a read-only account a `risk_rejected` row with
a reason rather than a failure at placement time.

## The snapshot's risk block

Computed deterministically from stored daily returns in `price_data`. No vendor
risk model and no model call — Spec K §5 requires every scheduled job to be pure
Python.

- beta to the benchmark over **60** and **250** sessions, each with its `n`;
- realized (annualised) portfolio volatility with its `n`;
- the **maximum and average pairwise** correlation among the ten largest
  positions, with the lookback and how many names entered it.

Max and average rather than a matrix: ten names over sixty sessions is 45 cells
estimated from 60 observations each, and most of it is noise. The two numbers
that survive that sample size are the worst pair and the average pair — and six
"different" names at 0.8 average pairwise correlation are one position, which
is the thing a sector code cannot say.

A figure whose sample is too short (fewer than 20 overlapping sessions) is
stored **null with a reason** in `metrics_notes_json`, never computed from
whatever was available.

## The workspace tools

Registered at scope `read` on the existing service (`workspace/tools.py`):

| Tool | Returns |
| --- | --- |
| `portfolio_overview` | holdings, cash, exposure across every enabled account, with provenance |
| `position_detail` | one position: rows, lots, basis, unrealized, exposure tags |
| `orders_open` | pending and recently filled orders, with `origin` |

`/health` reports `portfolio_sync.last_sync_age_seconds` and whether it is past
the budget.

## Token refresh, and the 30-day question

One data point: a token store left **idle** from June to September did not
survive — the refresh was rejected and a desktop re-auth was needed. Whether a
*continuously refreshing* service survives is unknown, and the Spec L §5.1 probe
cannot run without a record of every attempt.

So `database/token_store.py` logs `robinhood_token_refresh_due` when an expired
access token is read (the SDK only calls `set_tokens` on success, so a *failed*
refresh would otherwise leave no trace at all) and `robinhood_token_refresh` on
every write, with the previous and new expiry timestamps. The last 200 entries
are also kept in the encrypted blob so a log rotation does not end the probe;
`refresh_count` and `last_refresh_at` appear in
`python -m scripts.robinhood_auth --status`. No token value ever reaches either.

## Environment

```
PORTFOLIO_SYNC_ENABLED=false
PORTFOLIO_FRESHNESS_BUDGET_MINUTES=60
PORTFOLIO_SYNC_INTERVAL_MINUTES=60
PORTFOLIO_SYNC_PRE_MARKET_HOUR=8
PORTFOLIO_SYNC_POST_CLOSE_HOUR=16
PORTFOLIO_SYNC_POST_CLOSE_MINUTE=30
PORTFOLIO_MASS_DELETION_THRESHOLD=0.5
```

## Testing without a broker

Robinhood is not reachable from CI or from a cloud build environment, and
reaching it needs the owner's encrypted token store on a desktop. So:

- `execution/brokers/fake.py` — a deterministic in-memory broker that declares
  the most constrained real capability set and refuses every write path. It is
  the contract a future adapter passes (`test_fake_broker_contract`).
- `tests/fixtures/robinhood/` — **recorded-shape** payloads replayed through the
  real adapter's normalizers, so the key aliases, string-typed numbers, nesting,
  and missing fields are exercised by the code that runs live. They are not a
  live recording; see the README there for what that does and does not
  establish.
- `python -m scripts.record_robinhood_fixtures` replaces them with real
  responses on a machine that has the token store, masking every account number
  before writing.
