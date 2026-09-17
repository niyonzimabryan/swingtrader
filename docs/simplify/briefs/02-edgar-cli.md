# Brief 2 — the edgar CLI

Branch `claude/edgar-cli` off `main` of `<RR>`.

## Goal
Free SEC endpoints, no key, compact Markdown tables, disk cache, so a 10-K
question costs a few hundred tokens instead of a browsing session.

## Source material
Extract, do not rewrite from memory: `filings/client.py` (throttled httpx
client, `SEC_USER_AGENT` validation, backoff), `filings/sec_minimal.py`,
`filings/form4.py`, `filings/xbrl_aliases.py` from
`niyonzimabryan/swingtrader` main. Fixtures: `scripts/record_sec_fixtures.py`
and `tests/fixtures/` there — copy the recorded responses you need.

## Contract
1. `tools/edgar.py`, single file, stdlib + httpx, `python -m tools.edgar ...`
   and a `console_scripts` entry `edgar` in `pyproject.toml`.
2. Commands, all printing Markdown:
   - `facts TICKER [--concepts revenue,cogs,gross_profit,op_income,net_income,
     ocf,capex,fcf,shares_diluted,cash,debt] [--years 5] [--quarterly]` from
     `companyfacts`; aliases from `xbrl_aliases.py`; FCF derived and marked
     as derived; every row carries the fiscal period end and the `accn`.
   - `filings TICKER [--forms 10-K,10-Q,8-K] [--since DATE] [--limit 20]`
     from `submissions`.
   - `section ACCESSION --item 1|1A|7|7A|8 [--max-chars 12000]`: fetch the
     primary document, extract the item, truncate with a "(truncated at N)"
     footer. Best effort on HTML; say when the parse is uncertain.
   - `insiders TICKER [--days 180]`: Form 4 summary (who, role, buy/sell,
     shares, price, date), from `form4.py`.
   - `peers TICKER`: SIC code, and the other tickers sharing it from
     `company_tickers.json` — a list, not a judgment.
   - `search "phrase" [--forms] [--since]`: SEC full-text (`efts`) by
     default; if `EDGAR_API_KEY`-style variables exist, leave a clearly
     marked hook, unimplemented.
3. Cache under `.cache/edgar/` keyed by URL, TTL 24h for submissions/facts,
   forever for a filing document. `.cache/` gitignored. `--no-cache` flag.
4. `SEC_USER_AGENT` required with the same validation as the source; the
   error message says exactly what to export.
5. `.claude/skills/edgar/SKILL.md`: when to use each command, two worked
   examples, the rule "edgar before web for anything in a filing".
6. Tests on recorded fixtures only, no network in tests.

## Acceptance
`edgar facts AAPL --years 3` against a fixture prints a table under 40 lines;
`edgar section` on a fixture 10-K returns Item 7 text; CI green.

## Off limits
`portfolio/`, `learning/`, `research/`.
