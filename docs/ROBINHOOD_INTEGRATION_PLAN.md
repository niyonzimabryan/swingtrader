# Robinhood Integration Plan

Date: 2026-06-06

## Current Health Check

- GitHub: latest `origin/main` CI succeeded on 2026-05-23.
- Local checks: `.venv/bin/python -m pip check`, `compileall`, and 93 unit tests pass.
- Local doctor: fails because local `.env` is missing Anthropic, Telegram, Alpaca, Finnhub, FMP, Alpha Vantage, and FRED. Gemini is configured.
- Railway: production service is linked locally and reports `SUCCESS` for deployment `d7faf677-5212-4b12-ad42-c54653772e1b`.
- Railway latest build logs: dependency install and image export succeeded.
- Railway latest deploy logs: recent errors are Telegram polling network errors (`Bad Gateway`, `httpx.ReadError`), not build failures.
- Docker local reproduction: unavailable because `docker` is not installed locally.

## Robinhood Facts To Design Around

Official Trading MCP endpoint:

```text
https://agent.robinhood.com/mcp/trading
```

Robinhood currently documents these relevant trading tool calls:

- `get_accounts`
- `get_portfolio`
- `get_equity_positions`
- `get_equity_quotes`
- `get_equity_orders`
- `get_equity_tradability`
- `review_equity_order`
- `place_equity_order`
- `cancel_equity_order`
- watchlist tools

Important constraints:

- The agent gets broad read access to Robinhood accounts, positions, balances, transactions, and order history.
- The agent can only place trades in the dedicated Robinhood Agentic account.
- The Agentic account/onboarding flow is desktop-only.
- Options tool calls exist but are still rolling out, so Phase 1 should stay equities-only.
- `get_equity_tradability` must be used to confirm whether a symbol supports fractional trading before proposing small-dollar orders.

## Product Direction

Swing Trader remains the research and control plane:

```text
scan -> catalyst/research -> scoring -> memo -> operator approval -> broker execution -> monitoring -> attribution
```

Robinhood becomes the primary live broker for the small Agentic account. Alpaca stays as a selectable paper broker and fallback proving ground.

The Telegram UX should stay simple:

- `/scan` finds catalysts and sends memos.
- `/test TICKER thesis` runs a single-name memo.
- Inline buttons approve, reject, watchlist, or request deep research.
- `/status`, `/positions`, `/orders`, `/performance`, `/risk`, and `/config` show current state.
- New broker/mode commands should be short and explicit.

## Target Architecture

### 1. Broker Interface

Add a broker protocol under `execution/brokers/`:

```python
class BrokerClient(Protocol):
    name: str
    supports_fractional: bool
    supports_order_review: bool

    def get_account_info(self) -> dict: ...
    def get_positions_detail(self) -> list[dict]: ...
    def get_orders(self, status: str | None = None) -> list[dict]: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, dict]: ...
    def get_tradability(self, symbol: str) -> dict: ...
    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview: ...
    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult: ...
    def cancel_order(self, order_id: str) -> dict: ...
    def close_position(self, ticker: str) -> dict: ...
```

Then wrap the current Alpaca client as `AlpacaBroker` without changing behavior.

### 2. Robinhood MCP Broker

Add `RobinhoodMCPBroker` using the official MCP Python SDK Streamable HTTP client.

Design details:

- List tools on startup and map only the supported subset.
- Require `get_equity_tradability` before sizing each order.
- Always call `review_equity_order` before `place_equity_order`.
- Preserve the full review response in the database for auditability.
- Parse tool results defensively because MCP tool output may be text or structured content.
- Do not store Robinhood credentials or OAuth tokens in the repo.

Open auth question:

- Codex already has local OAuth for the Robinhood MCP, but the Railway app cannot safely reuse Codex’s local token.
- For Railway execution, we need a proper service-side OAuth flow/token store or a local sidecar/worker that owns the user-authorized MCP session.
- Until that is resolved, implement Robinhood in local/Hermes read-only or review-only mode first.

### 3. Broker Selection Config

Add config knobs:

```text
BROKER_PRIMARY=robinhood
BROKER_PAPER=alpaca
EXECUTION_MODE=review_only        # review_only | paper | live
ALLOW_LIVE_TRADING=false
REQUIRE_ORDER_REVIEW=true

ROBINHOOD_MCP_URL=https://agent.robinhood.com/mcp/trading
ROBINHOOD_ACCOUNT_BUDGET=25
ROBINHOOD_MAX_ORDER_NOTIONAL=5
ROBINHOOD_MAX_DAILY_NOTIONAL=10
ROBINHOOD_MAX_OPEN_POSITIONS=3
ROBINHOOD_ALLOW_FRACTIONAL=true
ROBINHOOD_ALLOW_OPTIONS=false
ROBINHOOD_ALLOWED_SYMBOLS=
ROBINHOOD_BLOCKED_SYMBOLS=

ALPACA_ENABLED=true
ALPACA_PAPER_ONLY=true
```

With only $25 in Robinhood, order sizing should default to dollar notional, not whole shares. If the Robinhood MCP rejects fractional/notional orders for a symbol, the memo should propose watchlist/pass instead of forcing a whole-share trade.

### 4. Database Changes

Extend `Trade`:

- `broker`
- `broker_account_id`
- `broker_order_id`
- `broker_stop_order_id`
- `broker_order_strategy`
- `order_review_json`
- `execution_mode`
- `requested_notional`
- `filled_notional`

Add `PipelineRun`:

- run id, trigger source, start/end, status
- counts: scanned, screened, researched, memos generated, approved
- cost/timing fields where available
- errors and degraded-stage information

Add `OrderEvent`:

- broker, order id, event type, status, raw payload, timestamp

These tables give Telegram, Hermes, and future analysis one shared source of truth.

### 5. Attribution Fix

Replace the current attribution stub with real small-sample-aware attribution.

Minimum useful output:

- closed trade count, win rate, average R, realized P&L
- performance by setup type
- performance by regime
- performance by direction
- performance by signal score bucket
- agent-level correlation between scores/confidence and realized R
- approval/rejection/watchlist conversion rates
- small-sample warnings instead of refusing to run before 30 trades

Telegram:

- `/attr` returns a compact attribution dashboard.
- `/performance` includes a short attribution summary.

Hermes:

- `python -m scripts.hermes_bridge attribution --json`

### 6. Hermes Interface

Add a non-Telegram command bridge so Hermes can trigger scans and read outputs:

```sh
python -m scripts.hermes_bridge status --json
python -m scripts.hermes_bridge scan --source hermes --json
python -m scripts.hermes_bridge memos --latest 10 --json
python -m scripts.hermes_bridge memo --id 123 --json
python -m scripts.hermes_bridge positions --json
python -m scripts.hermes_bridge orders --json
python -m scripts.hermes_bridge attribution --json
```

The bridge should use the same DB and pipeline code as Telegram. It should not bypass broker risk checks or order review.

For Railway, add an HTTP admin surface only if needed later. A CLI bridge is safer for the first pass.

## Telegram Command Plan

Keep common commands simple:

- `/scan` - run catalyst scan and send trade memos
- `/test AAPL thesis` - one-off memo
- `/broker` - show active broker/mode/account budget
- `/broker robinhood` - switch primary broker
- `/broker alpaca` - switch to Alpaca paper
- `/mode review` - review-only, no placement
- `/mode live` - live placement allowed after approval
- `/orders` - recent orders across active broker
- `/attr` - attribution dashboard
- `/risk` - current risk limits
- `/config` - broker, mode, sizing, model, scheduler settings

Approval flow should stay one click:

```text
Approve -> review order -> if review passes and mode allows it -> place order
```

If review returns warnings, send one confirmation message with the warnings and a final confirm button.

## Implementation Phases

### Phase 0: Restore Operational Baseline

1. Move local branch to an up-to-date implementation branch from `origin/main`.
2. Add missing local credits/keys or verify Railway has them.
3. Run `scripts.doctor` with live checks where safe.
4. Confirm Railway deployment status and logs.
5. Complete the blocked end-to-end Alpaca paper drill or explicitly defer it.

### Phase 1: Attribution + Run Ledger

1. Add `PipelineRun` and `OrderEvent`.
2. Implement real attribution in `tracking/attribution.py`.
3. Add `/attr` and Hermes JSON output.
4. Add tests with synthetic trades/memos.

### Phase 2: Broker Abstraction

1. Introduce `BrokerClient` protocol and normalized models.
2. Wrap Alpaca as `AlpacaBroker`.
3. Update pipeline, commands, daily digest, weekly report, order monitor, and position monitor to use `pipeline.broker`.
4. Keep `pipeline.alpaca` compatibility only temporarily.
5. Add regression tests proving Alpaca behavior is unchanged.

### Phase 3: Robinhood MCP Read/Review Mode

1. Add MCP dependency.
2. Implement `RobinhoodMCPBroker` with account, positions, quotes, tradability, orders, and order review.
3. Add `/broker robinhood`, `/orders`, `/mode review`.
4. Run read-only and review-only tests with the $25 Agentic account.
5. Store order reviews but do not place live orders yet.

### Phase 4: Robinhood Live Micro-Trading

1. Enable live only behind `ALLOW_LIVE_TRADING=true`.
2. Default caps for $25:
   - max order notional: $5
   - max daily notional: $10
   - max open positions: 3
   - equities only
   - fractional only when tradability confirms support
3. Require order review for every trade.
4. Place only limit orders.
5. Verify order monitor reconciliation with Robinhood order history.

### Phase 5: Hermes Automation

1. Add `scripts.hermes_bridge`.
2. Make Hermes scan triggers create `PipelineRun` rows.
3. Make latest memos/orders/attribution readable as JSON.
4. Add a documented runbook for Hermes-driven scans.

## First Implementation Ticket Breakdown

1. `swingtrader: restore deploy/test baseline before Robinhood`
2. `swingtrader: implement attribution dashboard and run ledger`
3. `swingtrader: introduce broker adapter interface with Alpaca parity`
4. `swingtrader: add Robinhood MCP broker read/review mode`
5. `swingtrader: enable Robinhood live micro-trading controls`
6. `swingtrader: add Hermes bridge for scans and outputs`

## Recommendation

Start with Phase 1, not Robinhood execution. Attribution and run logging are the foundation for deciding whether the strategy works. Then do broker abstraction. Robinhood MCP should enter after Alpaca parity tests pass, so the new live path has the same memo, approval, monitoring, and performance semantics as the current paper path.
