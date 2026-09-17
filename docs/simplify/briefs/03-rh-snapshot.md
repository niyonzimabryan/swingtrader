# Brief 3 — Robinhood read-only snapshot

Branch `claude/rh-snapshot` off `main` of `<RR>`.

## Goal
One script that writes `portfolio/snapshot.json`. Read paths only. Nothing in
this repo can place, cancel or modify an order, and a test asserts it.

## Source material
`execution/brokers/robinhood.py` (read paths: positions, balances, tax lots,
quotes), `database/token_store.py`, `scripts/robinhood_auth.py`,
`docs/ROBINHOOD_TOKEN_STORE.md`, `docs/robinhood/` (tool schema dump) from
`niyonzimabryan/swingtrader` main. Keep the `mcp>=1.27.2,<2` pin and the
comment explaining it.

## Contract
1. `tools/rh_snapshot.py`: `python -m tools.rh_snapshot` writes
   `portfolio/snapshot.json`: `{as_of_utc, source:"robinhood", accounts:[{name,
   cash, buying_power}], positions:[{symbol, qty, avg_cost, market_value,
   lots:[{qty, cost, acquired}]}]}`. No account numbers anywhere in the file.
2. Token store reduced to a file under `~/.config/<RR>/` with 0600 perms, or
   an env var; document both. No database.
3. `tools/rh_auth.py` from `robinhood_auth.py`, trimmed to what the read
   paths need.
4. `tests/test_read_only.py`: grep the package for the order-surface tool
   names in the schema dump (`place_`, `cancel_`, `replace_`, `submit_`) and
   fail if any appears outside a denylist constant.
5. `.claude/skills/rh/SKILL.md`: how to refresh the snapshot, and that the
   research loop's situate step reads it.
6. `portfolio/snapshot.json` gitignored; `portfolio/snapshot.example.json`
   committed with fake data.

## Acceptance
Unit tests with a fake MCP transport; the read-only test; CI green. The owner
runs `rh_auth` once locally — the PR body says so and does not claim a live
snapshot was taken.

## Off limits
`tools/edgar.py`, `learning/`, `research/`.
