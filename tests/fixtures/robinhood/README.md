# Recorded-shape Robinhood MCP fixtures

**These are not recorded from a live account.** Robinhood is unreachable from
CI and from the build environment this phase was written in, and reaching it
needs the owner's encrypted token store on a desktop machine. Every file here
was built from two sources: the 2026-09-08 dump of the server's own
`tools/list` schema (Spec L §5.1 — the dump is not committed to this repository;
`docs/robinhood/tool_schemas.json` does not exist here), and the response
handling already present in `execution/brokers/robinhood.py`, which encodes the
field names the live server has actually returned.

So they prove that the adapter's normalizers handle the *shape* — the key
aliases, the string-typed numbers, the nesting under `results`, the missing
fields — not that the shape is byte-identical to production. Anything that
depends on a real value (a rate limit, a token lifetime, whether a `gtc`
`stop_market` survives overnight) is **not** provable from these and is marked
as unverified in `docs/ROBINHOOD_INTEGRATION_PLAN.md`.

Replace them on a machine that has the token store:

```bash
python -m scripts.record_robinhood_fixtures --out tests/fixtures/robinhood
```

That script masks every account number before writing. Do not commit a fixture
it did not produce or that you have not checked for one.

## What each file covers

| File | Covers |
| --- | --- |
| `get_accounts.json` | two accounts: the Agentic one (`agentic_allowed: true`, cash) and the read-only primary |
| `get_portfolio__agentic.json` | cash split into settled and unsettled |
| `get_equity_positions__agentic.json` | a position with a basis, and one **without** — the null-basis case |
| `get_equity_positions__primary.json` | the same name held in the read-only account, so exposure must span both |
| `get_equity_tax_lots__agentic.json` | lots with and without a cost basis, short and unknown term |
| `get_equity_orders__agentic.json` | an open limit order and a filled one, both placed outside this system |
| `get_option_positions__agentic.json` | a short put — the position an overview must never silently omit |
| `get_pnl_trade_history__agentic.json` | a recent sell, from which the T+1 pending tranche is derived |
| `get_realized_pnl__agentic.json` | a realised loss, for the wash-sale window flag |
