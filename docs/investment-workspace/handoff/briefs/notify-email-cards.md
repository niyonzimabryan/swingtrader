# Brief: Resend email channel, HTML cards, and the signed card page

Branch: `claude/notify-email-cards`. Model: opus.

## Why

Bryan is the only user and does not use Telegram. Every human-facing message
this system sends — approval cards for proposals, scan memos, Strategy Lab
scorecards, pages (unprotected position, reconciliation discrepancy) — should
reach his inbox as a well-designed HTML email, and every email should link to
a full HTML page served by the workspace. Telegram stays available behind its
own flag but stops being the default surface. A later PR (owner tools over
MCP) makes approval happen in a coding-agent chat; this PR is the delivery and
rendering layer only. **Do not build approval tools here.**

Railway already carries `RESEND_API_KEY`, `PAGER_EMAIL_FROM=swingtrader@updates.readtop5.com`,
`PAGER_EMAIL_TO=niyonzimabryan@gmail.com` on both services. Resend's HTTP API
is `POST https://api.resend.com/emails` with a bearer key; `httpx` is already
a dependency. Do not add an SDK.

## What to build

1. **`notify/` package** (new, ledger-side). It must be importable by both the
   bot process and the workspace, so it may import `config`, `database`,
   `portfolio`, `utils` and nothing from `bot/`, `execution/`, `orchestrator/`
   (`tests/test_no_execute_scope.py` asserts the workspace's import closure;
   extend its allowlist if the test enumerates packages).
   - `notify/channel.py`: `Notification` (frozen dataclass: `kind`, `subject`,
     `text`, `html`, `ref` — a stable id such as the proposal uid, `detail`
     dict) and a `Channel` protocol with `send(notification) -> bool` that
     never raises. `notify/registry.py`: `configured_channels(settings)`
     returns the channels whose flags are on, and `broadcast(notification)`.
   - `notify/resend.py`: `ResendChannel` behind `NOTIFY_EMAIL_ENABLED`
     (default false). Fail closed and log on any error; a channel failure must
     never lose the underlying row (same rule as `portfolio.approvals.send_card`).
     Record every send attempt in a new table `notifications_sent`
     (kind, ref, channel, status, provider_id, error, created_at) via an
     Alembic migration off the current head `0011_comparable_subject_ticker`.
   - `notify/telegram.py`: wrap the existing Telegram senders as a `Channel`
     so the registry treats both the same; behaviour unchanged when
     `TELEGRAM_*` is set.
2. **Card renderer** `notify/cards/`. One card model per kind — proposal
   approval, scan memo, Strategy Lab scorecard, page/alert, daily digest — each
   built from the same data the Telegram formatters use today
   (`bot/formatters.py`, `portfolio/proposals.py::_card`, the Phase 6 card
   `body_md`, `scripts/strategy_lab_scoreboard.py`). Each renders to
   `(subject, html_email, html_page, text)`.
   - **Email HTML**: table-based layout, inline CSS, no JavaScript, no inline
     SVG (Gmail strips it), a light and dark palette via `prefers-color-scheme`
     where clients honour it and a safe default where they do not. Keep it
     under ~100 KB.
   - **Chart**: a PNG (matplotlib `Agg`, add the dependency) of the last ~60
     bars with entry, stop, and target lines for proposals and memos; served
     by the workspace at `/cards/<uid>/chart.png` and referenced by URL from
     the email (Gmail proxies remote images; do not base64-embed).
   - **Page HTML** (`/cards/<uid>`): the full version — the chart, the risk
     math (`risk_fraction`, `m`, `LB`, `PE`, horizon, every cap that bound the
     size, the budget it draws from — see AGENTS.md §6; print a negative lower
     bound when there is one), the cohort evidence with its warnings verbatim,
     the linked thesis and its invalidators, exposure impact, the bear case if
     stored, and the provenance/staleness flags on every number. This is where
     non-negotiables 2 and 3 apply: numbers come from stored answers, never
     computed in the renderer; staleness is repeated wherever the number is.
   - Design the templates seriously: one visual system, typographic hierarchy,
     the ticker and the decision at a glance, the evidence below. Look at what
     `docs/STRATEGY_LAB.md` and the Phase 6 card already print and make it
     legible, not decorative.
3. **Signed card page on the workspace.** Routes `/cards/{uid}` and
   `/cards/{uid}/chart.png` in `workspace/app.py`, gated by an HMAC over
   `uid` with `portfolio.approvals.sign`'s secret (`EXECUTION_APPROVAL_SECRET`)
   or a dedicated `CARD_LINK_SECRET` falling back to it; not expiring (the page
   is read-only), constant-time compare, 404 on a bad signature, no token
   needed (the link *is* the credential, and it exposes only what the email
   already contains). Cards are stored so the page can be re-rendered later:
   persist the card's source data (JSON) on `notifications_sent` or a
   `cards` table — your call, say which and why.
4. **Wire it in**, behind the flag:
   - `workspace/proposal_card.py::register_if_configured`: register the email
     channel when configured; Telegram when configured; both when both. The
     `proposal_card_channel_unconfigured` warning should name what is missing.
   - `portfolio/paging.py`: pages go through the registry too.
   - Bot side (`bot/handlers/proposals.py::register_bot_card_sender`,
     `bot/notifications.py::scan_complete`, the digest and weekly report):
     send the email card **in addition to** Telegram when the flag is on. Do
     not remove Telegram delivery here; the headless-runtime PR does that.
5. **Docs**: `docs/ENV_SETUP.md` (new variables), `.env.example`,
   `docs/OWNER_SETUP.md` §5 (email replaces the Telegram note),
   `docs/WORKSPACE_ACCESS.md` if the card URL matters to clients, and a short
   `docs/NOTIFICATIONS.md` describing channels, cards, the signed page, and
   how to add a channel.

## Tests

Deterministic, no network: a fake Resend transport asserting the request
body; renderer golden tests on the HTML structure (not pixel); the signed
route (good signature 200, bad 404, chart PNG content-type); registry
behaviour with each flag combination; the workspace import-closure test still
passing with `notify/` in it; migration round-trip. Render one of each card to
`docs/examples/cards/*.html` from a fixture so a reviewer can open them.

## Not in scope

Approval tools, the approval poller, removing Telegram, `TELEGRAM_ENABLED`.

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
