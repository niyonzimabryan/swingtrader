Closes BRY-187

## Summary

- Reworked Telegram MarkdownV2 memo rows for mobile-first reading at 390px width.
- Split dense pipe-separated metric rows into stacked rows across header, catalyst modifiers, fundamentals, historical precedent, risk analysis, signal agreement, Opus evaluation, and trade params.
- Added a repeatable local preview helper: `scripts/render_memo_mobile_qa.py`.
- Captured before/after evidence for three representative memo states: NVDA/proceed, AAPL/watchlist, MSFT/pass.

## Before / after screenshots

| Before | After |
| --- | --- |
| ![Before mobile memo preview](docs/assets/bry-187-mobile-memo-qa/before.png) | ![After mobile memo preview](docs/assets/bry-187-mobile-memo-qa/after.png) |

## QA notes

Before:
- Header clipped on 390px width.
- Modifier code span overflowed.
- Fundamentals, historical precedent, signal agreement, risk, and trade params were too dense on mobile.

After:
- Visible 390px Telegram-style preview has no horizontal clipping or code-span spillover.
- Proceed, watchlist, and pass memo states render with cleaner stacked rows.
- Live Telegram `/test` was not run locally to avoid creating a second polling connection while Railway may be active.

## Tests

- `.venv/bin/python -m unittest discover` — 93 tests passed.
