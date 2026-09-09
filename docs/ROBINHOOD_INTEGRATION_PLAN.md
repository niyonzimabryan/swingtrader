# Robinhood Integration Guide

Swing Trader can use Robinhood Agentic Trading through Robinhood's MCP trading endpoint. The integration is optional and off by default; Alpaca paper trading remains the default broker.

## What the MCP actually exposes — verified 2026-09-08

Everything in this section comes from the server's own `tools/list` schema
(73 tools, dumped with `python -m scripts.dump_robinhood_tool_schemas`) and from
Robinhood's support pages, not from developer documentation. The dump itself is
**not committed** — `docs/robinhood/tool_schemas.json` does not exist in this
repository, and the facts below are the record of what it showed. Re-run the
dumper on the machine that holds the token store to check any of them. **There is no
developer documentation**: no order-type table, no `place_equity_order` schema
published anywhere, no rate limits, no token lifetime, no SLA, no deprecation
policy. The May 2026 launch post says "launching in beta" and no exit-from-beta
statement exists. Onboarding and re-auth are desktop-only, and the documented
recovery path for a broken connection is a human reconnect.

Spec L §5.1 is the canonical record; this file is the operational half.

### Reads

| Tool | Used for |
| --- | --- |
| `get_accounts` | every account the token can see, with `agentic_allowed` |
| `get_portfolio` | equity, cash, settled vs unsettled |
| `get_equity_positions` | holdings |
| `get_equity_tax_lots` | lots, where exposed |
| `get_equity_orders` | orders, including ones placed in the app |
| `get_option_positions` | option positions — read so they are never omitted |
| `get_realized_pnl`, `get_pnl_trade_history` | realised losses, recent sells |
| `get_equity_quotes`, `get_equity_tradability` | prices and tradability |

Reads span **all** accounts. Placement is confined to the Agentic account,
verbatim from Robinhood's support page: "Your agent can only place trades in
your Robinhood Agentic account." The API rejects any `account_number` that is
not `agentic_allowed=true`, so that boundary is enforced upstream as well as by
`brokerage_accounts.agent_placeable`.

### Not exposed at all

**No tool exposes dividends received, cash movements, deposits, fees, or
corporate actions on the account.** `get_equity_fundamentals` carries
security-level dividend metadata — the declared rate for the instrument — not a
receipt. Consequence: the ledger reconstructs dividend cash flow from a
market-data dividend feed applied to holdings and flags every row
`reconstructed=true` (`docs/PORTFOLIO_LEDGER.md`). This is the single largest
gap in the integration and it is not closeable from this API.

### Order semantics (recorded now; nothing in Phase 1 places an order)

- `type` is one of `market`, `limit`, `stop_market`, `stop_limit`. **There is no
  bracket, OCO, OTO, or attach-stop parameter.** A protective stop is a
  separate order placed after the fill. `can_place_attached_stop` is therefore
  **false and stays false**; `can_place_standalone_gtc_stop` is **true** and is
  the capability that carries the protection.
- `time_in_force` is `gfd` or `gtc` (default `gfd`). Protective stops are `gtc`.
  How long Robinhood keeps a GTC order open is **not stated anywhere**, so the
  execution service re-reads open orders daily and re-places a stop that has
  disappeared.
- `stop_market` and `stop_limit` are **regular-hours only** and **whole-share
  only**. Fractional quantities are allowed for `market` + `regular_hours` and
  nothing else, so **every protected entry is a whole-share order** and
  `dollar_amount` entries are never used for a position this system is
  responsible for protecting.
- `ref_id` is a client idempotency key the upstream deduplicates on, so the
  fill-to-stop race can retry safely.
- `tax_lots` allows specified-lot selling (up to 30 lots, from
  `get_equity_tax_lots`) on plain sells — **not** on stop orders, not with
  `dollar_amount`, not in all-day sessions. So a discretionary sell can book
  `STRICT`; protective exits sell FIFO and the ledger records which.

### The Agentic account is a cash account

Owner decision, 2026-09-08. No margin of any kind, funded with a budget Bryan
loads. Proceeds settle **T+1**, so the ledger tracks settled and unsettled cash
separately and a proposal needing unsettled funds is refused with the settlement
date.

### Unattended session survival — still one data point

The access token is issued with roughly a 6.7-day lifetime plus a refresh token.
A token store left **idle** from June to September **did not survive**: the
refresh was rejected and a desktop browser re-auth was required. Whether a
*continuously refreshing* service survives is **unknown**.

The probe is running-once-deployed, not answered here: every refresh attempt is
logged with timestamps (`robinhood_token_refresh_due` when an expired access
token is read, `robinhood_token_refresh` on every write, with previous and new
expiry), and the last 200 entries are kept in the encrypted blob so a log
rotation does not end the probe. `python -m scripts.robinhood_auth --status`
reports `refresh_count` and `last_refresh_at`.

### What is still unverified

Marked here rather than asserted anywhere in code:

- whether a `gtc` `stop_market` placed through the MCP is visible in
  `get_equity_orders` the next session, and whether it triggers when touched.
  **This is the Phase 6 live probe. Until it passes, live entries stay closed.**
- Robinhood's rate limits, and what happens on exceeding them.
- whether a continuously refreshing token store survives 30 days unattended.
- the real response shapes. Robinhood is unreachable from CI and from the build
  environment, so `tests/fixtures/robinhood/` are **recorded-shape** payloads
  built from the schema dump and from this adapter's existing response handling,
  replayed through the real normalizers. Replace them with
  `python -m scripts.record_robinhood_fixtures` on a machine that has the token
  store; it masks every account number before writing.

## Safety Model

- Default execution is `EXECUTION_MODE=paper`.
- Robinhood is selected only when `BROKER_PRIMARY=robinhood` or `/broker robinhood ...` is used.
- Switching to Robinhood from Telegram always drops execution to `review_only`.
- Live placement requires `EXECUTION_MODE=live`, `ALLOW_LIVE_TRADING=true`, and a configured `ROBINHOOD_ACCOUNT_NUMBER`.
- Every Robinhood order runs broker review before placement.
- Robinhood live orders are long-only equities in this integration.
- Default Robinhood sizing is capped dollar-notional micro-trading.

## Required Configuration

```text
BROKER_PRIMARY=robinhood
EXECUTION_MODE=review_only
ALLOW_LIVE_TRADING=false
ROBINHOOD_MCP_URL=https://agent.robinhood.com/mcp/trading
ROBINHOOD_ACCOUNT_NUMBER=
ROBINHOOD_MAX_ORDER_NOTIONAL=5
ROBINHOOD_MAX_DAILY_NOTIONAL=10
ROBINHOOD_MAX_OPEN_POSITIONS=3
ROBINHOOD_ORDER_TYPE=market
TOKEN_ENCRYPTION_KEY=
```

Keep `ALLOW_LIVE_TRADING=false` until read-only and review-only commands work.

## OAuth Token Store

Robinhood MCP auth uses OAuth. Swing Trader stores the SDK-issued tokens in an encrypted local file named `robinhood_token.enc`, located beside the configured SQLite database. The file is ignored by git.

Generate an encryption key:

```bash
python -m scripts.robinhood_auth --gen-key
```

Set the generated value as `TOKEN_ENCRYPTION_KEY` in your local `.env` or deployment secret manager. Losing this key means the encrypted token file cannot be recovered; run the OAuth bootstrap again.

Run the bootstrap on a desktop browser:

```bash
python -m scripts.robinhood_auth
```

For a remote/headless shell:

```bash
python -m scripts.robinhood_auth --callback-file /tmp/robinhood-callback.txt
```

The script prints an authorization URL. Open it, approve Robinhood access, then write the final redirect URL into the callback file. The script stores the resulting token payload encrypted at rest and reports whether a refresh token was issued.

Check masked auth status:

```bash
python -m scripts.robinhood_auth --status
```

## Telegram Commands

```text
/broker
/broker accounts
/broker robinhood ACCOUNT_NUMBER
/broker robinhood 1
/mode review
/mode live
/orders
/risk
```

Use `/broker accounts` first to inspect available Robinhood accounts. Select only the dedicated Agentic account.

## Operational Notes

- Do not commit `.env`, `robinhood_token.enc`, account numbers, OAuth redirect URLs, or broker responses containing credentials.
- If placement times out, Swing Trader records a `placement_unknown` audit event and tries to reconcile by Robinhood `ref_id` before reporting failure.
- Raw broker payloads persisted to the database are scrubbed for tokens, headers, secrets, and account identifiers.
- Robinhood has no bracket/OCO/OTO order type at all — verified from the tool schema, not inferred. A protective exit is a separate `gtc` `stop_market` order placed after the fill, and it is therefore whole-share and regular-hours only. Swing Trader stores stop/target plans and reports status; until the Phase 6 protection probe passes, exits may still require manual action.

## Validation

Run deterministic checks:

```bash
python -m pip check
python -m compileall -q agents bot config data database execution memo orchestrator portfolio scanning scoring screening scripts tracking utils workspace main.py
python -m unittest discover -s tests -p "test_*.py"
```

Then perform live-provider checks locally with your private `.env`:

```bash
python -m scripts.doctor --skip-live
```

The final release drill is an operator task: run a scan, approve one Alpaca paper trade, and verify order submission plus monitor reconciliation before enabling any live broker path.
