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
| `notify/registry.py` | `configured_channels(settings)`, `email_channels`, `broadcast` |
| `notify/resend.py` | `ResendChannel` — one `POST https://api.resend.com/emails` |
| `notify/telegram.py` | `TelegramChannel` (HTTPS Bot API), `CallableChannel` (the bot's queue) |
| `notify/cards/base.py` | the document model and both renderings |
| `notify/cards/chart.py` | the PNG, matplotlib on `Agg` |
| `notify/cards/{proposal,memo,scorecard,alert,digest}.py` | one builder per kind |
| `notify/approval.py` | the approval card as an email, shared by both processes |
| `notify/links.py` | the HMAC over a card uid |
| `notify/store.py` | `cards` and `notifications_sent` |
| `workspace/app.py` | `/cards/{uid}` and `/cards/{uid}/chart.png` |

`notify/` imports `config`, `database`, `portfolio` and `utils` and nothing
else first-party. It may never import `bot/`, `execution/` or `orchestrator/`:
the workspace mints cards, and the workspace's import closure is asserted never
to reach a broker adapter (`tests/test_no_execute_scope.py`, Spec K §8,
Spec L §6.1).

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

Telegram has no flag of its own here. It is configured when
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, which is the condition it
has always been delivered under. Removing Telegram is the headless-runtime
change's job, not this one.

`WORKSPACE_BASE_URL` must be set on **both** Railway services if you want links
in email sent from the bot process as well as from the workspace. It is only
read to build the URL; the workspace does not use it to decide what to serve.

### Flag combinations

| `NOTIFY_EMAIL_ENABLED` | `TELEGRAM_*` | Result |
|---|---|---|
| false | unset | Nothing is delivered. `notify_no_channel` is logged per message. |
| false | set | Telegram only — today's behaviour, unchanged. |
| true (fully configured) | unset | Email only. **Cards arrive and nothing can be approved**; the workspace logs `proposal_card_not_approvable` at startup saying so. |
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

Rendered examples of all five are in [`examples/cards/`](examples/cards/) —
open the `.email.html` files in a browser. Regenerate with
`python -m scripts.render_example_cards`; `--check` fails if they are stale.

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

- **It cannot approve anything.** Approval is the signed, expiring, single-use,
  owner-bound Telegram callback handled in `bot/handlers/proposals.py`, in the
  bot process. An email has no callback and the card page is read-only. Turning
  email on adds a way to *see* a proposal, never a way to release one.
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
   constructor argument so it can be tested without a network.
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
- **A card with no price bars** renders without a chart and says so; the chart
  route 404s. That is the normal state for a name the price plane has not
  backfilled.
- **Nothing here is scheduled.** Delivery happens on the path that produced the
  thing being reported, which keeps AGENTS.md §1.4 intact: no scheduled job in
  this package needs a model, because no job in this package needs one.
