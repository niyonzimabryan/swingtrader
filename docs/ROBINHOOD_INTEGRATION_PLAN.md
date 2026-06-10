# Robinhood Integration Guide

Swing Trader can use Robinhood Agentic Trading through Robinhood's MCP trading endpoint. The integration is optional and off by default; Alpaca paper trading remains the default broker.

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
- Robinhood fractional dollar orders do not create broker-side stop/target OTO orders here. Swing Trader stores stop/target plans and reports status, but exits may still require manual action.

## Validation

Run deterministic checks:

```bash
python -m pip check
python -m compileall -q agents bot config data database execution memo orchestrator scanning scoring screening scripts tracking utils main.py
python -m unittest discover -s tests -p "test_*.py"
```

Then perform live-provider checks locally with your private `.env`:

```bash
python -m scripts.doctor --skip-live
```

The final release drill is an operator task: run a scan, approve one Alpaca paper trade, and verify order submission plus monitor reconciliation before enabling any live broker path.
