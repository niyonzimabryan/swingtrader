# Brief: the headless runtime — the bot process without Telegram

Branch: `claude/headless-runtime`. Model: opus.

## Why

Bryan does not use Telegram. After #81 (`notify/`: Resend email channel, HTML
cards, signed card page) and #82 (owner tools over MCP + the runtime approval
poller), every human-facing message can reach his inbox and every owner
decision can be recorded from his coding-agent chat. What still forces a
Telegram token into production is `main.py` itself: it refuses to start
without `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, builds the Telegram
`Application` unconditionally, and threads `app.bot`, `app.bot_data` and the
Telegram-backed `NotificationManager` through everything else. This PR makes
Telegram optional. **With `TELEGRAM_ENABLED` unset, nothing changes.**

## What to build

1. **`TELEGRAM_ENABLED`** (settings, default **true**). When false:
   - `main.py` does not require `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, does
     not construct `SwingTraderBot`, `Application`, or `MessageQueue`, and
     never calls the Telegram API. It still runs: `TradingPipeline`, the
     startup position reconciliation, `PipelineScheduler` (scans if
     `SCHEDULER_ENABLED`), `OrderMonitor`, `PositionMonitor`, `MonitorWatchdog`,
     the daily digest and weekly report jobs, Phase 6's `ExecutionService`, the
     Strategy Lab `StrategyExecutionService` and its three jobs, and the owner
     `ApprovalPoller` (#82). The process must stay up as a plain asyncio
     program with the same graceful-restart and shutdown behaviour the daily
     restart relies on.
   - Whatever today lives in `app.bot_data` (`execution_service`,
     `strategy_execution_service`, `strategy_lab_adapters`, …) gets a
     process-level home that does not depend on Telegram (a small runtime
     container object), and the Telegram handlers read from it when Telegram
     is on. `bot/handlers/*` must keep working unchanged when it is on.
2. **`NotificationManager` becomes channel-agnostic.** Today every method
   formats a message and pushes it to the Telegram `MessageQueue`. Give it a
   sink abstraction: with Telegram on, the queue as today (plus the email
   cards #81 already added); with Telegram off, `notify.registry` channels
   (email) only. Every one of its ~20 methods (order filled, stop triggered,
   target hit, regime change, drawdown warning, agent failure, deep-research
   updates and PDFs, scan complete, system message, position alerts, strong/
   rough day, …) must produce a `notify.Notification` with a sensible subject
   and text, and, where #81 already has a card kind (scan memo, digest,
   scorecard, page), reuse that renderer rather than a second formatter.
   Deep-research PDFs become an email attachment or a signed link — your
   call, say which.
   - `pipeline.notification_manager`, `billing_alerts.register(...)`,
     `research_paging.register(...)`, the `_pager` in `main.py`, and the
     approval poller's `notify=` all keep working headless.
   - Memo delivery (`bot/handlers/_memo_delivery.py`, `scan_complete`): with
     Telegram off, the scan memo card email is the delivery; with Telegram on,
     behaviour is unchanged.
3. **Approval cards headless.** `bot/handlers/proposals.py::register_bot_card_sender`
   is Telegram-specific; with Telegram off, register `notify.approval.EmailCardSender`
   (#81) as the card sender in the bot process too, so proposals created by the
   bot's own paths (the Strategy Lab paper dispatcher) reach Bryan by email.
   The card must say approval happens through the MCP owner tools (#82) and
   name the proposal uid the agent will need.
4. **Kill switch and owner commands.** With Telegram off there is no
   `/live_kill`; the `kill_switch` MCP tool (#82) is the switch. Make sure
   nothing in the runtime *requires* the Telegram command path (grep for
   `bot_data`, `chat_id`, `context.bot`, `update.effective_*` in non-handler
   code). `OWNER_ID` (#82) must be set when Telegram is off, since the
   default derives from `telegram_chat_id`; refuse to start headless with
   Phase 6 on and no owner id, with a clear log line.
5. **Startup validation.** The "missing API keys" check in `main.py` gates on
   the flag. Log one line at startup naming the mode and the channels that
   will carry notifications (`notify.registry.configured_channels`). If
   Telegram is off and email is not configured, start anyway but log a
   warning that no human-facing channel exists (the log pager is the
   fallback, as in `portfolio.paging.log_pager`).
6. **Docs**: `docs/ENV_SETUP.md` (a "Headless" subsection: the flag, what
   still runs, what needs `OWNER_ID`), `.env.example`, `docs/OWNER_SETUP.md`
   §5/§6 (the sequence to go headless in production: set
   `NOTIFY_EMAIL_ENABLED=true` and confirm an email arrives, set `OWNER_ID`,
   then `TELEGRAM_ENABLED=false`), `docs/NOTIFICATIONS.md`, `CLAUDE.md`
   deployment notes (the "one polling connection" warning becomes
   conditional), `docs/SYSTEM_OVERVIEW.md` §8 (one paragraph).

## Tests

- A test like `tests/test_service_role_guard.py` that runs `main.py`'s startup
  path with `TELEGRAM_ENABLED=false` and no Telegram variables, with the
  pipeline/broker/scheduler pieces faked, and asserts: no `telegram` module
  network object is constructed, the scheduler and monitors start, the
  approval poller starts when its flags are on, and shutdown is clean.
- The same path with `TELEGRAM_ENABLED=true` (default) asserting today's
  wiring is unchanged (`SwingTraderBot` built, `bot_data` populated).
- `NotificationManager` in both modes: every public method produces exactly
  one delivery on the configured sink, with subject/text asserted for a
  sample, and no Telegram call when off.
- The headless card sender registration and a proposal created by the paper
  dispatcher reaching the email channel (fake transport).
- The `OWNER_ID` refusal.
- Existing tests untouched; `tests/test_no_execute_scope.py` and
  `tests/test_execution_lifecycle_isolation.py` must stay green — the
  runtime container object must not make `execution/` reachable from
  anything the workspace imports.

## Not in scope

Removing Telegram code; changing what any notification says; new MCP tools.
## Worker rules (every brief)

- Read `AGENTS.md`, then `docs/investment-workspace/handoff/HANDOFF.md`, before
  writing code. The four non-negotiables are asserted by tests; never weaken a
  test to pass. `mcp` stays pinned `<2`. No secrets anywhere in the diff.
- Work on the branch named in this brief, from `origin/main` (`git fetch origin
  main` first). Commit after each coherent unit; push often.
- Validate on Python 3.12 (CI's version): `python -m compileall -q .` and
  `python -m unittest discover -s tests -p "test_*.py"` (~14 min; 3 Postgres-only
  skips are expected). CI is sharded; `scripts/test_shard_weights.json` and the
  `test-count-check` job exist — if you add test modules, run
  `python scripts/ci_shard.py --help` and follow it so the count check passes.
- Every new capability ships behind a flag defaulting **off**. Production must
  not change behaviour when this PR merges with no variable set.
- When done: open the PR against `main` with a body that states what was built,
  every design decision you made where the spec was silent, what was verified
  and how, what was NOT verified, and what you deferred and why. Then **stop**.
  Do not schedule check-ins, do not subscribe to the PR, do not merge. The
  orchestrating session reviews and merges.
- If you hit a usage limit, the orchestrator will resume you; keep the branch
  pushed so nothing is lost.
