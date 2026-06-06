# Robinhood Integration Handoff

Date: 2026-06-06

## Goal

Implement Swing Trader as the research/control plane for Alpaca paper trading and Robinhood Agentic Trading, with paper mode, review-only mode, live mode, broker audit logging, and Telegram controls.

## Current Implementation State

- Added normalized broker layer under `execution/brokers/`.
- Added Alpaca adapter preserving current paper behavior.
- Added Robinhood MCP adapter for accounts, portfolio, positions, quotes, tradability, order review, placement, cancellation, and close-position flows.
- Added broker router so `EXECUTION_MODE=paper` routes to Alpaca paper while review/live modes route to the selected primary broker.
- Added settings/env support for `BROKER_PRIMARY`, `EXECUTION_MODE`, `ALLOW_LIVE_TRADING`, Robinhood MCP URL/account/caps, and order type.
- Added `Trade` broker fields, `PipelineRun`, and `OrderEvent` for auditability.
- Added `/broker`, `/mode`, `/orders`, and `/attr` Telegram commands.
- Updated approval callback to handle review-only, warning confirmation, paper placement, and live placement.
- Added `scripts.hermes_bridge` for JSON status, scan, memos, orders, positions, runs, and attribution.
- Implemented small-sample attribution in `tracking/attribution.py`.

## Live Trading Gates

- Default remains `EXECUTION_MODE=paper`.
- Robinhood live placement requires:
  - `BROKER_PRIMARY=robinhood`
  - `EXECUTION_MODE=live`
  - `ALLOW_LIVE_TRADING=true`
  - `ROBINHOOD_ACCOUNT_NUMBER=<dedicated Agentic account number>`
- Robinhood orders are equities-only in this integration.
- Robinhood short trades are blocked; use paper mode for short ideas.
- Robinhood default order type is `market` because the current MCP schema only allows `dollar_amount` for market orders. `ROBINHOOD_ORDER_TYPE=limit` is supported but requires at least one whole share.
- Every Robinhood order calls review before placement.

## Key Files

- `execution/brokers/base.py`
- `execution/brokers/alpaca.py`
- `execution/brokers/robinhood.py`
- `execution/brokers/factory.py`
- `execution/order_manager.py`
- `execution/order_monitor.py`
- `execution/position_monitor.py`
- `bot/handlers/commands.py`
- `bot/handlers/callbacks.py`
- `bot/handlers/performance.py`
- `scripts/hermes_bridge.py`
- `tracking/attribution.py`
- `database/models.py`
- `database/db.py`
- `config/settings.py`
- `config/onboarding.py`
- `.env.example`
- `requirements.txt`

## Validation To Run

```bash
.venv/bin/python -m pip check
.venv/bin/python -m compileall agents backtest bot config database execution memo orchestrator scanning scoring screening scripts tracking utils main.py
.venv/bin/python -m unittest discover tests
.venv/bin/python -m scripts.hermes_bridge status --json
```

Latest validation in this implementation pass:

- `.venv/bin/python -m compileall agents backtest bot config database execution memo orchestrator scanning scoring screening scripts tracking utils main.py` passed.
- `.venv/bin/python -m pip check` passed.
- `.venv/bin/python -m unittest discover tests` passed: 95 tests.
- `BROKER_PRIMARY=alpaca EXECUTION_MODE=paper .venv/bin/python -m scripts.hermes_bridge status --json` returned paper-mode JSON status.

## Known Risks / Follow-Ups

- Robinhood MCP OAuth/token persistence is environment-dependent. The app supports MCP headers/token env vars, but production Railway needs a proper token store or local sidecar before unattended service execution.
- Robinhood fractional dollar orders do not create broker-side OTO stop/target orders in this implementation. The app stores stop/target plans and monitors/alerts; exits must be handled by callbacks/manual close unless future Robinhood order classes support this directly.
- `/broker accounts` can list accounts, but the operator must explicitly choose the Agentic account; the app should not infer a default.
- Do not run live placement tests from automation. Use review-only first, then a deliberate micro live order after verifying account/caps in Telegram.
