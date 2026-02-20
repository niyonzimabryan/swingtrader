# Swing Trader — Scratchpad

> Running capture of ideas, next steps, tech debt, and decisions for the Swing Trader project.
> Check items off as you complete them. Don't delete — just mark done.

---

## Setup (Do First)

- [x] **Copy Anthropic key** — added to `.env`
- [x] **Register Alpaca** — paper trading keys added to `.env`
- [x] **Register Finnhub** — API key added to `.env`
- [x] **Register FMP** — API key added to `.env`
- [ ] **Register Alpha Vantage** — _(deferred — FMP covers this for now)_
- [x] **Register FRED** — API key added to `.env`
- [x] **Create Telegram bot** — bot token + chat_id added to `.env`
- [ ] **Register Reddit app** — reddit.com/prefs/apps (for PRAW). _(Deferred — agent is stubbed. Get credentials when ready to build out sentiment agent)_
- [x] **First live test** — `/test AAPL`, `/test NVDA`, `/test MSFT` all returned memos to Telegram

---

## Technical Debt

- [x] **Pattern Agent is stubbed** — ✅ Built full implementation: Sonnet setup classification → yfinance historical instance search → forward return computation (T+5/T+10/T+15/T+20) → max drawdown → summary stats → Sonnet interpretation → scoring. Cached via SQLite. NVDA: 29 instances, 55.2% win rate. Fixed `revenue_acceleration` routing to use yfinance earnings search.
- [ ] **Reddit Sentiment Agent is stubbed** — Returns 0.5 score. Real PRAW integration code is written but needs credentials + tuning of sentiment scoring
- [ ] **10 Telegram commands are stubbed** — `/watchlist`, `/upcoming`, `/pause`, `/resume`, `/config` return "coming soon" messages. Need full implementations
- [ ] **No test suite** — Zero tests currently. Need unit tests for scoring engine, risk manager, position sizing, and integration tests for the full pipeline
- [ ] **Signal attribution needs 30+ trades** — `tracking/attribution.py` is a stub. Can't do meaningful signal-level performance analysis until enough closed trades exist
- [ ] **`run_in_executor` in pipeline** — `run_ad_hoc_async` uses `loop.run_in_executor` which works but isn't ideal. Consider making the full pipeline natively async
- [ ] **No database migrations** — Using `create_all()` for now. Should add Alembic for schema changes as the project evolves
- [x] **Scoring weights need rebalancing** — ✅ Updated to: catalyst 40%, fundamental 30%, pattern 22%, sentiment 8%. Revisit after 50+ trades with real attribution data.

---

## Ideas

- [ ] **Expand ticker universe** — Currently 89 tickers (S&P 100 equivalent). Could add mid-caps, sector-specific lists, or dynamic screener-based universe refresh
- [ ] **Tune scoring weights from real data** — After 50+ closed trades, run attribution analysis to see which agents actually predict winners, then rebalance weights
- [ ] **Email backup channel** — PRD calls for email delivery as Telegram backup. Not critical for MVP but useful for audit trail
- [ ] **Backtest framework** — PRD Phase 2 scope. Replay historical data through the pipeline to validate strategy before going live
- [ ] **Dockerfile for deployment** — Run on a VPS/cloud instead of local machine. Important for 24/7 scheduler reliability
- [ ] **Watchlist with alerts** — Track tickers that scored 0.40-0.55 (below memo threshold) and alert if catalysts strengthen
- [ ] **Multi-timeframe analysis** — Current system is swing-focused (3-15 day). Could add day-trade and position-trade modes
- [ ] **Portfolio rebalancing** — Auto-suggest trimming winners and adding to conviction positions based on drift from target allocation
- [ ] **Pattern Agent: incorporate own trade history** — Once 30+ closed trades exist, add our own trade outcomes as additional pattern data alongside historical market data. Our trades are higher-signal because they went through the full scoring pipeline.
- [ ] **RL / training loop for scoring** — Explore reinforcement learning or fine-tuning on top of pattern data + trade outcomes. Use closed trade P&L as reward signal to optimize scoring weights, agent prompts, and setup classification. Could start simple (Bayesian weight optimization from attribution data) and graduate to more sophisticated RL as data accumulates.

---

## Product / UX Questions

- [ ] **Approval flow for scheduled scans** — Currently full scans generate memos but there's no batch approval UX. Should scheduled memos queue up for morning review?
- [ ] **Position sizing confidence** — Should users be able to override the calculated position size, or is the system's sizing authoritative?
- [ ] **Risk parameter tuning** — The 5 risk rules are "non-negotiable" per PRD, but should the thresholds (10% drawdown, 3% daily loss) be configurable?

---

## Bugs

- [x] **Direction always SHORT** — ✅ Fixed in `scoring/engine.py` (normalize ambiguous→neutral, derive primary_direction from highest-priority non-neutral signal, default to bullish for Phase 1) and `memo/generator.py` (use scoring_result direction instead of catalyst.direction). Verified: all three test tickers show LONG.
- [x] **Catalyst confidence shows `?` in memos** — ✅ Fixed: merge AgentOutput.confidence into catalyst raw_data dict in `memo/generator.py`, format as percentage in `memo/templates/ic_memo.py`. Verified: NVDA=78%, AAPL=72%, MSFT=75%.
- [ ] **Trade params contradict SHORT direction** — entry/stop/target are always computed as LONG params (stop below entry, targets above). If direction is actually short, these need to be inverted. For Phase 1 long-only this is cosmetic but will matter later.
- [x] **Scoring weights diluted by stubs** — ✅ Updated to catalyst 40%, fundamental 30%, pattern 22%, sentiment 8%.
- [x] **FMP returning 402 (fundamental data dead)** — ✅ Rewrote `data/fundamental_data.py` to use yfinance as primary source, FMP as optional fallback. Same output schema, no agent changes needed. Verified: AMAT quality=0.26, valuation=0.39, growth=0.27, balance=0.90.
- [x] **MarkdownV2 escaping broken** — ✅ Fixed `memo/templates/ic_memo.py`: added `fmt()` helper for safe numeric formatting, ensured all dots inside backtick code spans, all free text through `esc()`. Memos now render with bold/code formatting on Telegram.
- [x] **Opus API calls taking 30+ minutes** — ✅ Added `analyze_with_fallback()` to `utils/anthropic_client.py` with Sonnet fallback on timeout/rate-limit. Reduced retry attempts (3→2), added 120s client timeout. Pipeline now completes in ~50s.
- [x] **Model upgrade to Sonnet 4.6 + Opus 4.6** — ✅ Updated all model IDs: `claude-sonnet-4-6`, `claude-opus-4-6`. Haiku stays at `claude-haiku-4-5-20251001`. Updated `model_selector.py`, `settings.py`, `anthropic_client.py`.

---

## Design Notes

- [ ] **Memo readability on mobile** — Telegram MarkdownV2 formatting can be finicky on small screens. Test memo layout on phone once bot is live
- [ ] **Message splitting** — Messages >4096 chars get split. Verify the split points don't break mid-section in real memos
