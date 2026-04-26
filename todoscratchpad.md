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
- [x] **Register Reddit app** — ✅ No longer needed. Reddit agent superseded by WebResearchAgent (live web search).
- [x] **First live test** — `/test AAPL`, `/test NVDA`, `/test MSFT` all returned memos to Telegram

---

## Technical Debt

- [x] **Pattern Agent is stubbed** — ✅ Built full implementation: Sonnet setup classification → yfinance historical instance search → forward return computation (T+5/T+10/T+15/T+20) → max drawdown → summary stats → Sonnet interpretation → scoring. Cached via SQLite. NVDA: 29 instances, 55.2% win rate. Fixed `revenue_acceleration` routing to use yfinance earnings search.
- [x] **Reddit Sentiment Agent is stubbed** — ✅ Superseded by `WebResearchAgent` which does live web search via Anthropic's `web_search_20250305` tool. Reddit agent file (`agents/reddit_agent.py`) is orphaned dead code — can be deleted. No PRAW credentials needed.
- [ ] **1 Telegram command still stubbed** — `/upcoming` returns "coming soon". All others are live including `/watchlist` (view, add, remove with inline buttons).
- [ ] **No test suite** — Zero tests currently. Need unit tests for scoring engine, risk manager, position sizing, and integration tests for the full pipeline
- [ ] **Signal attribution needs 30+ trades** — `tracking/attribution.py` is a stub. Can't do meaningful signal-level performance analysis until enough closed trades exist
- [ ] **`run_in_executor` in pipeline** — `run_ad_hoc_async` uses `loop.run_in_executor` which works but isn't ideal. Consider making the full pipeline natively async
- [ ] **No database migrations** — Using `create_all()` for now. Should add Alembic for schema changes as the project evolves
- [x] **Scoring weights need rebalancing** — ✅ Updated to: catalyst 40%, fundamental 30%, pattern 22%, sentiment 8%. Revisit after 50+ trades with real attribution data.
- [ ] **Deep research poll loops on persistent errors** — `deep_research_client.py:205-208` catches all exceptions during polling and continues. If error is persistent (bad task_id, auth failure), it loops for 30 min before timeout. Add consecutive-error counter to break after 5 failures.
- [ ] **Web research error → neutral 0.5 score** — `_fallback_output()` returns `score=0.5` on failure. Not a bug (0.5 = "no opinion") but could improve by adding `is_valid` flag to AgentOutput and excluding failed agents from weighted average in ScoringEngine.

---

## Ideas

- [x] **Expand ticker universe** — ✅ Already S&P 500 (503 tickers across 11 GICS sectors). Auto-generated via `scripts/update_sp500.py`. Could later add mid-caps or dynamic screener-based refresh.
- [ ] **Tune scoring weights from real data** — After 50+ closed trades, run attribution analysis to see which agents actually predict winners, then rebalance weights
- [ ] **Email backup channel** — PRD calls for email delivery as Telegram backup. Not critical for MVP but useful for audit trail
- [ ] **Backtest framework** — PRD Phase 2 scope. Replay historical data through the pipeline to validate strategy before going live
- [x] **Dockerfile for deployment** — ✅ Done. Dockerfile + railway.toml deployed to Railway. See Autonomous Operation section.
- [x] **Watchlist with alerts** — ✅ Full stack: CRUD backend (`orchestrator/universe.py`), lower Haiku threshold for re-scanning, "Watchlist" button on memos, `/watchlist` command (view list, add/remove tickers, inline remove buttons). Remaining idea: dedicated alert notifications when catalysts strengthen between scans.
- [ ] **Multi-timeframe analysis** — Current system is swing-focused (3-15 day). Could add day-trade and position-trade modes
- [ ] **Portfolio rebalancing** — Auto-suggest trimming winners and adding to conviction positions based on drift from target allocation
- [ ] **WATCHLIST/PASS override: Opus-adjusted params** — When Opus recommends watchlist or pass, have Opus still generate adjusted trade params (reduced size, tighter stops) as an "if you must trade" fallback, instead of showing Sonnet's raw draft params. Currently override shows Sonnet's unmodified params which defeats the purpose of the Opus layer.
- [ ] **Pattern Agent: incorporate own trade history** — Once 30+ closed trades exist, add our own trade outcomes as additional pattern data alongside historical market data. Our trades are higher-signal because they went through the full scoring pipeline.
- [ ] **RL / training loop for scoring** — Explore reinforcement learning or fine-tuning on top of pattern data + trade outcomes. Use closed trade P&L as reward signal to optimize scoring weights, agent prompts, and setup classification. Could start simple (Bayesian weight optimization from attribution data) and graduate to more sophisticated RL as data accumulates.

---

## Product / UX Questions

- [x] **Open-source onboarding flow** — ✅ Added README-backed setup path + refreshed `.env.example` that treats Anthropic, Telegram bot token/chat ID, Alpaca paper, market/data provider keys (Finnhub, FMP, Alpha Vantage, FRED), and database config as required; Gemini remains an optional add-on.
- [x] **Config doctor command** — ✅ Added `python -m scripts.doctor` with presence checks, SQLite path check, Telegram/Alpaca/data-provider live validation, and Gemini warning-only handling.
- [x] **First-run setup wizard** — ✅ Added `python -m scripts.setup_wizard` local web app at `localhost:8765` that creates `.env`, walks users through every required provider key, opens signup docs, discovers `TELEGRAM_CHAT_ID`, tests Telegram delivery, validates provider connectivity, and handles Gemini/observability/advanced integrations separately.
- [x] **Move search-heavy stages to Gemini Pro** — ✅ `WEB_SEARCH_PROVIDER=gemini` now routes Discovery and WebResearch through Gemini Pro + Google Search grounding, with Anthropic fallback if Gemini is absent; Gemini Flash screening and Gemini deep research remain separate pipeline roles.
- [x] **Use leading-edge Gemini for search-heavy stages** — ✅ Updated defaults, wizard schema, `.env.example`, local `.env`, and tests to use `gemini-3.1-pro-preview`; live smoke test confirmed grounded search works with the configured key.
- [x] **Pipeline stage progress messages** — ✅ Implemented: `run_ad_hoc()` accepts `progress_cb` callable, fires at each stage (regime → catalyst → fundamental → pattern → web research → scoring → memo). `/test` handler edits the status message in real-time via `asyncio.run_coroutine_threadsafe()`. `/scan` uses start message + scan completion notification.
- [ ] **Approval flow for scheduled scans** — Currently full scans generate memos but there's no batch approval UX. Should scheduled memos queue up for morning review?
- [ ] **Position sizing confidence** — Should users be able to override the calculated position size, or is the system's sizing authoritative?
- [x] **Risk parameter tuning** — ✅ Thresholds already configurable via `.env` / environment variables (`drawdown_circuit_breaker_pct`, `daily_loss_halt_pct` in `config/settings.py`). No runtime Telegram command to change them, but that's fine for now.

---

## Bugs

- [x] **V2 compliance audit (imports/models/pipeline/config/scoring/contracts/errors/time)** — ✅ Completed static scan against `swing-trader-prd.md` Section 16 with file/line findings captured in chat report
- [x] **Deep research auto-trigger fails on scheduled scans** — ✅ Fixed: replaced `asyncio.get_event_loop().create_task()` with `asyncio.run_coroutine_threadsafe(coro, self.bot_loop)`. Bot event loop ref stored at init via `main.py`. Deep research now schedules correctly from sync scheduler context.
- [x] **Pattern narrative missing in memo output** — ✅ Fixed: added `"reasoning": pattern.reasoning` to pattern dict in `memo/generator.py:78`. Memo template can now render Sonnet's interpretation text.
- [x] **Web/catalyst API error responses treated as valid neutral signals** — ✅ Fixed: `_compute_catalyst_score()` now returns (0.1, 0.1, 0.1) when `"error" in sonnet_result`. All 3 Sonnet-calling paths log warning and propagate `sonnet_error` in raw_data. Failed calls score 0.1 instead of 0.5.
- [x] **UTC consistency cleanup** — ✅ Fixed: replaced `datetime.now()` with `datetime.utcnow()` in `pipeline.py`, `memo/generator.py`, `discovery_agent.py`, `pdf_generator.py`. Data adapters (date range computation) left as-is since they compute relative offsets.
- [x] **Opus missing V2 pattern similarity stats** — ✅ Fixed: `scoring/engine.py` now includes `total_instances`, `weighted_win_rate_t10`, `hs_count`, `hs_win_rate_t10`, `hs_median_return_t10`, `most_similar_instance` from `pattern.raw_data` in the signal package sent to Opus.
- [x] **Direction always SHORT** — ✅ Fixed in `scoring/engine.py` (normalize ambiguous→neutral, derive primary_direction from highest-priority non-neutral signal, default to bullish for Phase 1) and `memo/generator.py` (use scoring_result direction instead of catalyst.direction). Verified: all three test tickers show LONG.
- [x] **Catalyst confidence shows `?` in memos** — ✅ Fixed: merge AgentOutput.confidence into catalyst raw_data dict in `memo/generator.py`, format as percentage in `memo/templates/ic_memo.py`. Verified: NVDA=78%, AAPL=72%, MSFT=75%.
- [x] **Scoring weights diluted by stubs** — ✅ Updated to catalyst 40%, fundamental 30%, pattern 22%, sentiment 8%.
- [x] **FMP returning 402 (fundamental data dead)** — ✅ Rewrote `data/fundamental_data.py` to use yfinance as primary source, FMP as optional fallback. Same output schema, no agent changes needed. Verified: AMAT quality=0.26, valuation=0.39, growth=0.27, balance=0.90.
- [x] **MarkdownV2 escaping broken** — ✅ Fixed `memo/templates/ic_memo.py`: added `fmt()` helper for safe numeric formatting, ensured all dots inside backtick code spans, all free text through `esc()`. Memos now render with bold/code formatting on Telegram.
- [x] **Opus API calls taking 30+ minutes** — ✅ Added `analyze_with_fallback()` to `utils/anthropic_client.py` with Sonnet fallback on timeout/rate-limit. Reduced retry attempts (3→2), added 120s client timeout. Pipeline now completes in ~50s.
- [x] **Model upgrade to Sonnet 4.6 + Opus 4.6** — ✅ Updated all model IDs: `claude-sonnet-4-6`, `claude-opus-4-6`. Haiku stays at `claude-haiku-4-5-20251001`. Updated `model_selector.py`, `settings.py`, `anthropic_client.py`.
- [x] **Message too long for Telegram** — ✅ Fixed: `test_command()` in `bot/handlers/test_idea.py` now uses `split_message()` to chunk memos >4096 chars. Keyboard (approve/reject buttons) attached to last chunk only. Both MarkdownV2 and plain text fallback paths handle splitting.
- [x] **Inconsistent memo formatting across split Telegram messages** — ✅ Root cause fixed: removed mutating marker repair from `bot/formatters.py`, added deterministic memo splitter (`split_memo_message`) and shared all-or-nothing delivery fallback (`bot/handlers/_memo_delivery.py`) so chunks no longer mix Markdown/plain formatting.
- [x] **Telegram memo WEB RESEARCH truncation + chunk-2 parse miss** — ✅ Fixed both root causes: removed hard clipping in memo template (`peer_comparison`, WEB RESEARCH dimensions) and expanded Markdown parse detection to catch Telegram `"Can't parse entities"` variants (including reserved `'.'`) so fallback now reliably rolls back + resends full plain text.
- [x] **Opus thinking mode deprecation** — ✅ Fixed: switched `thinking.type` from `enabled` to `adaptive` in both occurrences in `utils/anthropic_client.py`. Committed & deployed.
- [ ] **Trade params contradict SHORT direction** — entry/stop/target are always computed as LONG params (stop below entry, targets above). If direction is actually short, these need to be inverted. For Phase 1 long-only this is cosmetic but will matter later.

---

## Autonomous Operation (Feb 2026)

- [x] **Order Monitor** — `execution/order_monitor.py` polls Alpaca every 30s for fill/stop/target status. Handles: entry fills → update trade + place target sells, stop triggers → close trade + P&L, target hits → partial/full exit, time exits → close after max_holding_days, cancelled orders → cleanup. Wired into `main.py` lifecycle.
- [x] **`/scan` command** — Triggers `pipeline.run_full_scan()` from Telegram. Runs in executor, reports completion via scan notification.
- [x] **Enhanced `/performance`** — Full dashboard: live Alpaca equity/cash/day P&L, open positions with stop-loss from DB, closed trade stats (win rate, profit factor, best/worst, avg hold).
- [x] **Scan completion notifications** — `NotificationManager.scan_complete()` fires at end of every `run_full_scan()`. Shows duration, tickers scanned, memos generated with scores.
- [x] **Order monitor wired to main.py** — Starts after bot init, stops on shutdown. Runs as async background task.
- [x] **Railway deployment** — ✅ Deployed to Railway with Dockerfile, volume mount at `/data` for SQLite persistence, tzdata for scheduler, polling conflict retry (10 attempts with backoff). GitHub auto-deploy connected — `git push` to `main` triggers deploy. Project: `e556a6d9-2023-4c81-a031-e32e160a33be`.
- [x] **Fund Alpaca paper account** — ✅ Confirmed: $100K cash, $200K buying power, 0 positions. Ready for live paper trading.
- [ ] **First autonomous scan** — Run `/scan` from Telegram, verify memos arrive, approve one, verify order submitted + monitored.

---

## Design Notes

- [ ] **Memo readability on mobile** — Telegram MarkdownV2 formatting can be finicky on small screens. Test memo layout on phone once bot is live
- [x] **Message splitting** — ✅ Deterministic memo split now prefers section boundary before Opus/final params and includes regression tests (`tests/test_memo_formatting_delivery.py`)

---

## Architecture Review Follow-ups (Feb 22, 2026)

- [x] **Create architecture trigger guide doc** — Added `/ARCHITECTURE_EVOLUTION_TRIGGERS.md` with measurable thresholds for when to implement handler offloading, parallelization, SQLite hardening, Postgres migration, and Redis.
- [x] **Create implementation handoff doc** — Added `/claudehandoff.md` with decision context, per-file changes, env vars, fallback behavior, validation notes, rollback steps, and trigger-linked follow-ups.
- [x] **Retitle completed architecture revisions doc** — Renamed typo file to `/ARCHITECTURE_REVISIONS_FINAL.md` and updated heading to reflect final, completed revisions.
- [x] **Offload blocking bot handlers from event loop** — Implemented shared blocking helper and moved `/ask`, `/regime`, `/score`, `/performance` heavy sync work to executor with immediate ack + timeout-safe fallback responses.
- [x] **Parallelize independent agent stages in ad-hoc pipeline** — Implemented shared post-catalyst parallel helper and applied to both `/test` and `/scan` flows (fundamental + pattern + web research).
- [x] **Add automatic parallel stability controller** — Implemented rolling health tracker with auto degrade/recover (3 bad runs in 12 → workers 3→2, cooldown + healthy streak recovery), plus Telegram/log alerts on mode change.
- [ ] **(Deferred / potential) Add cache-first reads for repeat analyses** — Keep as an optional optimization later; only implement if repeated same-day ticker analysis becomes common enough to justify added cache complexity.
- [ ] **Persistent run logging & cost tracking** — Railway logs vanish on redeploy. Need to persist per-scan metrics (token counts per stage, duration, costs, tickers processed/escalated/memoed) to DB or external service. Currently flying blind on API spend. Exploring Langfuse for structured LLM observability — could reuse across all projects.
- [x] **Harden SQLite for concurrent workload** — Enabled WAL mode + busy_timeout in `database/db.py`, added runtime index creation for hot filters, and added model indexes for `trades.status`, `trades.exit_date`, `memos.status`, and `memos.created_at`.
- [ ] **Define DB migration path trigger to Postgres** — Keep SQLite now, but migrate when multi-replica/worker deployment or persistent lock contention appears.

---

## Perf / Cost Review Follow-ups (Mar 8, 2026)

- [x] **Create DOCX perf/cost review report** — added `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/SwingTrader_Post_Architecture_Perf_Cost_Review_2026-03-08.docx`
- [x] **Fix Tier 2 gate bypass in scan builder** — `orchestrator/pipeline.py:_build_scan_list()` now only falls back to Tier 1 for tickers Gemini did not screen, so screened names with `escalated=0` no longer leak into Tier 3/4.
- [x] **Fail closed when Gemini screening is quota-exhausted** — Batch failures now synthesize screened results so Tier 1 names do not fan out into Anthropic stages after Gemini quota/rate-limit failures, and discovery-provider exhaustion disables full-universe fallback.
- [x] **Propagate Langfuse session context into post-catalyst worker threads** — Wrapped `ThreadPoolExecutor` submissions with `contextvars.copy_context()` so `fundamental`, `pattern`, and `web_research` inherit the active scan/ad-hoc session.
- [x] **Fix daily digest + weekly report schema drift** — `bot/daily_digest.py` now uses `Memo.created_at` and keeps ORM-derived calculations inside the DB session; `bot/weekly_report.py` now uses `Memo.status`, `Memo.created_at`, and `Trade.pnl_absolute`.
- [x] **Attach a real Railway volume or move off SQLite-on-/data** — Created Railway volume `swingtrader-volume` and attached it to production at `/data`; new deployment metadata now includes `volumeMounts: ["/data"]`.
- [x] **Fix Alpaca stop placement for approved trades** — Approved trades now submit as protected OTO limit entries, persist as `pending_fill` until filled, and reconcile attached/fallback stop IDs from the order monitor instead of placing a standalone stop before the entry exists.
- [x] **Repair pattern-data FMP fallbacks** — `data/pattern_data.py` now uses current FMP endpoints for earnings (`/earnings`) and analyst grades (`/grades`), points insider lookups at the current search endpoint, and logs 402 restricted endpoints clearly.
- [x] **Add regression coverage for perf-review fixes** — Added `tests/test_pipeline_reports_execution.py` covering Tier 2 gating, report schema usage, and the `pending_fill` approved-trade flow.
- [x] **Confirm Railway deployment `39557038-c880-402a-ad12-371830f10391` reaches SUCCESS** — Production deployment completed successfully and Railway reports `volumeMounts: ["/data"]` on the active build.
- [x] **Investigate recent low scan volume / low Sonnet escalation** — Langfuse review showed Mar 11-13 scheduled scans often collapsed to discovery-only or a single `HIMS` watchlist pass. Root cause: discovery responses were truncating at the 4096-token cap, causing JSON parse failures and empty discovery output. Fixed by raising discovery output budget to 8192, preserving more raw text on parse errors, and recovering complete discovery ticker objects from truncated JSON. Added `tests/test_discovery_agent.py`.
