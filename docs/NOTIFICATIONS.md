# Notifications: channels, cards, and the signed card page

Every message this system sends a human goes through `notify/`. There are two
channels — email (Resend) and Telegram — five card kinds, and one read-only page
per card served by the workspace.

The one-sentence summary: **email is how you read it, Telegram is how you
approve it, and nothing in this package can place an order.**

---

## 1. What is where

| | |
|---|---|
| `notify/channel.py` | `Notification` and the `Channel` protocol |
| `notify/registry.py` | `configured_channels(settings)`, `email_channels`, `non_telegram_channels`, `broadcast` |
| `notify/resend.py` | `ResendChannel` — one `POST https://api.resend.com/emails` |
| `notify/telegram.py` | `TelegramChannel` (HTTPS Bot API), `CallableChannel` (the bot's queue) |
| `notify/cards/base.py` | the document model and both renderings |
| `notify/cards/chart.py` | the PNG, matplotlib on `Agg` |
| `notify/cards/{proposal,memo,scorecard,alert,digest}.py` | one builder per kind (`alert.py` holds two: `page` and `alert`) |
| `notify/approval.py` | the approval card as an email, shared by both processes |
| `notify/context.py` | the page's extra sections, read best-effort from the rows that hold them |
| `notify/links.py` | the HMAC over a card uid |
| `notify/store.py` | `cards` and `notifications_sent` |
| `workspace/app.py` | `/cards/{uid}` and `/cards/{uid}/chart.png` |
| `bot/notifications.py` | `NotificationManager` and its two sinks — the Telegram queue, or `notify/` |

`notify/` imports `config`, `database`, `portfolio`, `utils`, and — from
`context.py` only, for the page's read-only sections — `comparables` and
`research_workspace`. It may never import `bot/`, `execution/` or
`orchestrator/`: the workspace mints cards, and the workspace's import closure is
asserted never to reach a broker adapter (`tests/test_no_execute_scope.py`,
Spec K §8, Spec L §6.1). That test walks `notify/` as part of the closure, so the
rule is enforced rather than remembered.

## 2. Configuration

Everything is off by default. **With no variable set, production behaves exactly
as it did before this shipped**: Telegram delivery unchanged, the pager still a
log line, and the `/cards` routes not registered at all.

| Variable | Default | What it does |
|---|---|---|
| `NOTIFY_EMAIL_ENABLED` | `false` | The master flag. Off: no email, no card row written, no card page served. |
| `RESEND_API_KEY` | — | Resend API key (`re_…`). |
| `PAGER_EMAIL_FROM` | — | Sender. Must be on a domain verified in Resend. |
| `PAGER_EMAIL_TO` | — | Recipient, or a comma-separated list. |
| `CARD_LINK_SECRET` | — | HMAC key for the card link. Falls back to `EXECUTION_APPROVAL_SECRET`. |
| `WORKSPACE_BASE_URL` | — | Where the card page lives. Unset means the email carries no link and no chart. |
| `CARD_CHART_SESSIONS` | `60` | Daily bars captured into a card's chart. |

Telegram is configured when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set,
which is the condition it has always been delivered under — **and, in the bot
process, only while `TELEGRAM_ENABLED` is true**. That flag lives in the runtime
rather than here (`docs/ENV_SETUP.md` §11a): with it false the bot builds no
Telegram anything and its `NotificationManager` delivers through
`non_telegram_channels(settings)`, which excludes Telegram *even when its
credentials are still set*. Leaving them set is the expected way to keep the
switch reversible, so "not configured" is the wrong test for "do not use".

The **workspace** process does not read `TELEGRAM_ENABLED`; clear its Telegram
variables if you want it to stop sending Telegram cards too.

`WORKSPACE_BASE_URL` must be set on **both** Railway services if you want links
in email sent from the bot process as well as from the workspace. It is only
read to build the URL; the workspace does not use it to decide what to serve.

### Flag combinations

| `NOTIFY_EMAIL_ENABLED` | `TELEGRAM_*` | Result |
|---|---|---|
| false | unset | Nothing is delivered. `notify_no_channel` is logged per message. |
| false | set | Telegram only — today's behaviour, unchanged. |
| true (fully configured) | unset | Email only. Nothing an email carries can approve; approval is the `approve_order` MCP owner tool (Spec K §10). The workspace logs `proposal_card_not_approvable` when *it* has no Telegram channel, which is about the card, not about whether you can decide. |
| true (fully configured) | set | Both. Telegram carries the approvable card; email carries the designed one plus the link. |
| true, something missing | either | `notify_email_channel_unconfigured` names the missing variable; Telegram still works if set. |

## 3. The card kinds

| kind | built by | from |
|---|---|---|
| `proposal` | `notify/cards/proposal.py` | `portfolio.proposals.proposal_payload(row)` |
| `scan_memo` | `notify/cards/memo.py` | the scan's own counts and memo rows |
| `scorecard` | `notify/cards/scorecard.py` | `scripts.strategy_lab_scoreboard.build_scorecard` |
| `page` | `notify/cards/alert.py` | `portfolio.paging`'s `(event, detail)` |
| `digest` | `notify/cards/digest.py` | the daily digest and weekly report's own MarkdownV2 |
| `alert` | `notify/cards/alert.py::build_alert_payload` | `bot.notifications`, headless: a fill, a stop, a target, a regime change |

`alert` and `page` are deliberately separate kinds for the same renderer. A page
means capital is exposed in a way nobody chose and somebody has to act now; an
alert is a routine operational notice. One kind for both would make "was I paged
last week" unanswerable from `notifications_sent`, which is the question that
log exists for.

Rendered examples of the first five are in [`examples/cards/`](examples/cards/) —
open the `.email.html` files in a browser. Regenerate with
`python -m scripts.render_example_cards`. `--check` compares the HTML and text
byte for byte and the PNGs for presence only: matplotlib stamps its version into
PNG metadata, so a strict image comparison would fail on any machine whose
matplotlib differs from the one that last committed them.

### The document model

Every kind builds the same JSON shape: a list of typed blocks (`rows`, `table`,
`text`, `list`, `quote`, `chart`, `divider`). One renderer walks it twice — once
for the email, once for the page — so the two cannot print different numbers.
A block marked `page_only` is dropped from the email; the email is the summary
and the link, and the page is the full record.

### Three rules the renderer holds by construction

1. **It computes no statistic.** Values arrive pre-formatted from the builder,
   which got them from the stored row. There is no arithmetic on a reported
   figure anywhere in `notify/cards/` (AGENTS.md §1.2).
2. **Staleness travels on the row**, so the flag prints *beside* the number it
   qualifies — in the email, on the page, and in the plain-text part — rather
   than once in a footer (§1.3).
3. **Untrusted text stays visibly distinct.** A `quote` block with
   `trust: "untrusted"` renders on its own plate with a banner, in every
   rendering, and its HTML is escaped (§5).

### What the page adds, and what it does not

`notify/context.py` reads three things the proposal row does not carry, for the
page: the **cited cohort answer** with its `status`, `depth`, subject verdict and
its own `warnings` verbatim; the **active thesis** with its invalidators; and the
**stored bear case**, attributed (Spec M §7). Every one is best-effort — a
missing thesis, an unresolvable citation or a database that blinked produces a
card with one fewer section, never a lost card and never an invented one. Only an
`active` thesis is shown; a draft is not a position's reasoning.

**Exposure impact is not wired.** The renderer prints an `exposure` section when
it is given one, and nothing gives it one yet. The combined-book concentration
and sector figures are computed by `portfolio.proposals.read_context` when the
proposal is sized and are not carried on the row, so filling the section here
would mean a second implementation of the same arithmetic that could disagree
with the one that actually bound the size — exactly the failure a single stored
payload exists to prevent. Carrying those figures on the proposal row is its own
change.

### Email constraints

Table-based layout, inline CSS on every element, no JavaScript, no inline SVG
(Gmail strips it), no `data:` image (Gmail strips those too and proxies remote
ones instead), under 100 KB. The light palette is the baseline and is inlined,
so a client that drops `<style>` still renders correctly; dark mode is a
`prefers-color-scheme` override in `<style>` for the clients that honour it.

The chart is a light-palette PNG in both themes. `prefers-color-scheme` does not
reach an `<img>`, so shipping two images and guessing which to link would be
worse than committing to one on a light plate.

## 4. The signed card page

    https://<workspace>/cards/<uid>?s=<hmac>
    https://<workspace>/cards/<uid>/chart.png?s=<hmac>

**The link is the credential.** No bearer token, and the reasoning is a real
trade worth stating:

- The page exposes exactly what the email that linked to it already contains,
  to the same inbox. A leaked link leaks what a forwarded email leaks.
- It is read-only. No route under `/cards` writes anything, and there is no path
  from it to an approval.
- Requiring a token would mean putting one in an email, which is strictly worse.

The uid is 128 bits of `secrets.token_hex`; the signature is a full SHA-256
HMAC, compared in constant time — no truncation, because nothing caps a URL's
length the way Telegram caps `callback_data` at 64 bytes. It **does not expire**,
unlike an approval reference: an approval is a decision about a book that moves,
so a stale one is dangerous; a card page is a record of a moment, so a stale one
is the point.

The page is served with `Content-Security-Policy: default-src 'none'` (plus
`img-src 'self'` for the chart and `style-src 'unsafe-inline'` for the card's own
inline styles, which the email format forces and which is not script),
`frame-ancestors 'none'`, `Referrer-Policy: no-referrer` and
`X-Robots-Tag: noindex`. The renderer escapes every value it prints; the CSP is
the second line, because this page shows filing and news text that is
attacker-writable (AGENTS.md §5) and one forgotten `esc()` in a future block
should not be the whole defence.

A bad signature, a missing signature, an unknown uid, and an unreachable
database all return the **same 404 with the same body**. Distinguishing them
would make the route an oracle for enumerating uids, and the uid is half the
credential.

### The page is a record, not a view

`cards.payload_json` holds the card's source data, including the price bars
behind the chart. The page re-renders from that and from nothing else, so a card
opened in March says what the email said in January rather than what the ledger
says today.

**Why `cards` is its own table** rather than a JSON column on
`notifications_sent`: a card is a resource and a delivery is an event. The same
card goes out on email and on Telegram, and a re-send is another delivery of the
same card. Folding the payload into the delivery row would store it once per
channel per attempt, and would leave `/cards/<uid>` having to choose which of
several identical copies is the page — a choice with no right answer.

## 5. What this layer cannot do

- **It cannot approve anything.** Approval is a signed, expiring, single-use,
  owner-bound decision made out of band — the Telegram callback handled in
  `bot/handlers/proposals.py`, or the `approve_order` MCP owner tool, which only
  *records* the decision for `orchestrator/approval_poller.py` to act on. An
  email has no callback and the card page is read-only. Turning email on adds a
  way to *see* a proposal, never a way to release one. The proposal card's
  `approval_route` decides which of the two the closing note names, and headless
  it also prints the `proposal_uid` that `approve_order` takes; it changes
  nothing about what can approve.
- **It cannot place an order.** `tests/test_no_execute_scope.py` walks `notify/`
  as part of the workspace's import closure.
- **It cannot lose a row.** Every channel returns a bool and never raises; a
  delivery failure leaves the proposal `proposed` and unapprovable, which is
  visible and safe, rather than rolling back the write.

## 6. The delivery log

`notifications_sent` gets one append-only row per attempt per channel, on
success and on failure alike: `kind`, `ref`, `channel`, `status`
(`sent`/`failed`/`skipped`), `provider_id`, `error`, `card_uid`, `created_at`.

    -- was I ever told about this proposal, and on which channel?
    SELECT channel, status, provider_id, error, created_at
    FROM notifications_sent
    WHERE ref = '<proposal_uid>'
    ORDER BY created_at;

    -- anything failing quietly in the last day?
    SELECT channel, status, count(*), max(error)
    FROM notifications_sent
    WHERE created_at > now() - interval '1 day'
    GROUP BY channel, status;

A channel that is enabled but silently not sending is the failure that costs a
real message, so it gets a named log line (`notify_email_channel_unconfigured`,
`notify_no_channel`) as well as a row.

## 7. Adding a channel

1. Write a class in `notify/` with `name: str` and
   `send(notification) -> bool`. It must **never raise** and must write its
   attempt through `notify.store.record_send`. Take the transport as a
   constructor argument so it can be tested without a network, and resolve it
   with `notify.testguard.resolve_transport(self.name, transport, <live>)` so
   the suite cannot build a channel that reaches your provider — see §9.
2. Add its flag and credentials to `config/settings.py`, `.env.example` and
   §2 above. Default off.
3. Build it in `notify.registry.configured_channels`, and log a named warning
   naming the missing variables when its flag is on but it cannot be built.
4. If the channel has a message shape of its own (buttons, blocks, threads),
   read it out of `notification.detail["<name>"]` rather than widening
   `Notification`. That is how Telegram's inline keyboard travels the shared
   path without the approval callback leaking into the generic one.
5. Add it to `tests/test_notify_channels.py`: the exact request body, a 4xx, a
   transport exception, and the unconfigured case.

## 8. Operating notes

- **Resend requires a verified sending domain.** `PAGER_EMAIL_FROM` must be on
  it or every send 422s. The failure is recorded in `notifications_sent.error`
  with Resend's own message.
- **Telegram's 4096-byte cap** truncates a long body, and the card says it was
  truncated and points at the page. The email has no such cap.
- **An email attachment** (today only the deep-research PDF, headless) travels
  in `notification.detail["attachments"]` as `[{"path", "filename"}]`, read off
  disk at send time by `ResendChannel` and ignored by Telegram, which sends a
  document through the bot's own queue. Over `notify.resend.MAX_ATTACHMENT_BYTES`
  (8 MB) the attachment is dropped with `notify_email_attachment_too_large` and
  the email still goes: losing the attachment beats losing the message, and on a
  headless deployment email is the only channel there is.
- **A card with no price bars** renders without a chart and says so; the chart
  route 404s. That is the normal state for a name the price plane has not
  backfilled.
- **Nothing here is scheduled.** Delivery happens on the path that produced the
  thing being reported, which keeps AGENTS.md §1.4 intact: no scheduled job in
  this package needs a model, because no job in this package needs one.

## 9. Testing: the suite cannot send, and that is enforced

**Never run the test suite in an environment that holds live delivery
credentials.** Not the production container, not a shell with a populated
`.env` on the path, not a Railway `run`.

This is written down because it happened. On **2026-09-13** the full suite was
run with `python -m unittest discover -s tests -p "test_*.py"` inside the
production bot container, whose environment carries a live `RESEND_API_KEY`,
`PAGER_EMAIL_FROM`, `PAGER_EMAIL_TO`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
and `NOTIFY_EMAIL_ENABLED=true`. The notification tests built real channels and
took the real send path: **12 real emails and Telegram messages** went to the
owner's personal inbox, carrying test-fixture trade proposals with subjects
like `[approval needed] NVDA — proposal 1`. Nothing was written to production —
the tests use their own throwaway database — but the database was the only
thing that was sandboxed. The credentials were not.

`notify/testguard.py` is the fix, armed once in `tests/__init__.py`, which is
the only module every run imports (the suite is unittest; there is no
`conftest.py` and a pytest one would not run). Three layers:

1. **Construction refuses.** `ResendChannel` and `TelegramChannel` resolve
   their transport through `testguard.resolve_transport`. Under the suite, a
   channel that would use the real `httpx` transport raises
   `LiveSenderRefused` with a message naming this incident. Passing the live
   transport explicitly is treated as the same request.
2. **An explicit opt-in for tests that need channel objects.**
   `testguard.allow_inert_channels()` permits construction and substitutes a
   transport that refuses at send time. It is for assertions about *which*
   channels the registry builds — it never permits delivery. A test that
   asserts what goes on the wire injects `tests.notifyfixture.RecordingTransport`
   instead, as every notification test already does.
3. **The credentials are blanked.** `testguard.neutralise_environment()` sets
   all six variables to neutral values in `os.environ` for the test process, so
   an unanticipated code path finds nothing to authenticate with. It is called
   *after* `config.settings` is imported, because that module's
   `load_dotenv(override=True)` would otherwise copy `.env` back over the
   scrub; environment variables outrank pydantic-settings' `env_file`, so the
   blanks then win for every `Settings()` the suite builds.

`LiveSenderRefused` derives from `BaseException`, not `Exception`, and that is
load-bearing. Every `send` path here — plus `registry.broadcast` and
`portfolio.paging.send_card` — catches `Exception` and turns it into a logged
`False`, because a channel must never raise into the row it was reporting. A
guard that raised `Exception` would be swallowed into a silent non-delivery,
which is exactly the failure that hides a test believing it asserted a
delivery. As a `BaseException` it passes through all of them untouched, with no
edit to any handler, and `unittest` reports it as a loud test error.

`CallableChannel` is not guarded: it has no transport of its own and is handed
the bot's in-process queue, which is injected at every call site.

CI has no production secrets, so none of this changes what CI does. It changes
what happens when the suite is run somewhere it should not be.
