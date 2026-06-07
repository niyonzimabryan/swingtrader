# Robinhood MCP Token Store — Design

Status: **design approved, build gated on confirming the real OAuth contract** (see Blocker).
Source: multi-agent design workflow (Jun 6, 2026). Chosen over an Upstash/hosted-KV variant.

---

## REVISION (Jun 7, 2026) — use the MCP SDK's OAuth, don't hand-roll

Research findings after connecting the real Robinhood agentic MCP:
- Robinhood Agentic Trading auth = **OAuth, no client secret, no developer signup, no API key**; any MCP-capable client may connect (incl. a custom server). So swingtrader **can** get its own token.
- The authorize endpoint is `https://robinhood.com/oauth`; the observed scope was **`internal` (NOT `offline_access`)** → a refresh token may NOT be issued. Token lifetime/refresh is undocumented publicly.
- The installed `mcp` SDK (1.27.2) ships `mcp.client.auth.OAuthClientProvider` (dynamic client registration → PKCE → code exchange → **auto-refresh when a `refresh_token` is present**) and a pluggable `TokenStorage` interface (4 async methods: `get_tokens`/`set_tokens`/`get_client_info`/`set_client_info`). `OAuthToken` exposes `access_token, token_type, expires_in, scope, refresh_token`.

**Revised approach:** don't hand-roll the refresher. Implement `database/token_store.py` as an `mcp.client.auth.TokenStorage` backed by a Fernet-encrypted file on `/data`, and pass `OAuthClientProvider(storage=...)` as the httpx `auth=` in `robinhood.py:_call_tool`. The SDK does registration/exchange/refresh and discovers the token endpoint via MCP OAuth metadata — nothing hardcoded. Our code shrinks to: encrypted persistence + a one-time bootstrap + (if no refresh token) expiry-detection + re-auth alerting.

**Open question that gates the production build:** does Robinhood issue a `refresh_token`? Resolve empirically with a discovery bootstrap (`scripts/robinhood_auth.py` running `OAuthClientProvider` once, reporting whether `set_tokens` received a `refresh_token` + the `expires_in`/`scope`). If yes → unattended auto-refresh works out of the box. If no → unattended live requires periodic interactive re-auth; design the store to detect expiry and alert.

The sections below are the original hand-rolled design — superseded by the SDK approach for the refresh mechanics, but still accurate on storage location (/data, not a Railway var), Fernet encryption, sealed-key handling, atomic writes, and the risk list.

## Recommendation

An **encrypted-file token store on the existing `/data` Railway volume**: a new
`database/token_store.py` holding a Fernet-encrypted JSON blob beside the SQLite DB,
with a **synchronous** in-process refresher that keeps the live
`Settings.robinhood_mcp_auth_token` field current.

### Why /data, not a Railway variable
Writing a rotated refresh token back to a Railway *variable* triggers an auto-redeploy,
which restarts the very bot doing the rotation → churn loop. The rotating secret must
live on `/data` (runtime read/write, no redeploy). Only the **encryption key** belongs
in a Railway **sealed variable** (long-lived, app never writes it back).

### Why sync, not async
The broker is fully synchronous — `robinhood.py:_call_tool_sync` wraps `_call_tool` in
`asyncio.run()`. The refresher must use `threading.RLock` + a sync `httpx` client, not an
`asyncio.Lock`. The MCP adapter reads the token in exactly one place:
`robinhood.py:_headers()` does `getattr(self.settings, "robinhood_mcp_auth_token", "")`,
so a refresher that mutates the live `Settings` object keeps `_headers()` unchanged.

## Implementation steps
1. Add `cryptography>=42,<47` to `requirements.txt`.
2. `config/settings.py`: add `token_encryption_key: str = ""  # env TOKEN_ENCRYPTION_KEY` in the Robinhood block (keep empty-string default + `load_dotenv(override=True)` precedence).
3. `database/token_store.py` (NEW): derive the file dir from `settings.database_url` using the **same logic as `db.py:18-23`** (→ `/data/robinhood_token.enc` on Railway, project root locally — never hardcode `/data`). Plaintext JSON `{version, access_token, refresh_token, expires_at (absolute UTC, computed once), token_type, scope, extra_headers, rotated_count}`, persisted as Fernet ciphertext. Atomic write: temp file `0o600` → `fsync` → `os.replace`.
4. Process-wide singleton `RobinhoodTokenStore`: `load()` (decrypt on startup); `ensure_fresh()` (sync, `threading.RLock` + double-checked locking, refresh when `now >= expires_at - 300s`); `refresh()` (sync `httpx` POST `grant_type=refresh_token`; persist the NEW rotated refresh token to disk **before** publishing the access token); `_publish()` sets `self.settings.robinhood_mcp_auth_token` on the live Settings object.
5. `refresh()` errors: backoff+jitter (~3 attempts) on transient/5xx; treat `invalid_grant`/`invalid_client` as **terminal** → raise `ReauthRequired` + fire a Telegram alert (operator re-runs interactive auth).
6. Startup wiring: where `init_db(settings.database_url)` is called, call `RobinhoodTokenStore.start(settings)`. Empty `token_encryption_key` → log a warning + **passthrough mode** (static env token, no refresh) so paper/local dev keep working.
7. Call-path wiring: at the top of `robinhood.py:_call_tool_sync` (line ~301), before `asyncio.run`, call `RobinhoodTokenStore.instance().ensure_fresh()`. `_headers()` stays unchanged. Optional: single retry-on-401 that calls `ensure_fresh(force=True)`.
8. `status()` returning **masked** fields (reuse `config/onboarding.py:mask_value`, `robinhood.py:_mask_account`) — token present/absent, `expires_at`, seconds-to-refresh, `rotated_count`, reauth flag. Surface in `/status`.
9. `scripts/robinhood_auth.py` (NEW): one-time interactive bootstrap (authorization-code flow with **`offline_access`** scope via the `mcp__robinhood-trading__authenticate` handshake) → `store.set_initial(token_response)` encrypts+persists the first refresh token. Stop the Railway bot during local seeding (Telegram single-polling), or seed via `railway run` against the mounted volume.
10. `.env.example` + `CLAUDE.md`: document `TOKEN_ENCRYPTION_KEY`, the `railway variables set` sealed-variable steps, and the bootstrap.

## Code touchpoints
- `database/token_store.py` (NEW)
- `config/settings.py` (add `token_encryption_key`)
- `execution/brokers/robinhood.py` (`ensure_fresh()` at `_call_tool_sync` top; `_headers()` unchanged)
- `database/db.py` (reference — copy path-derivation `db.py:18-23`)
- `requirements.txt` (add `cryptography`)
- `scripts/robinhood_auth.py` (NEW — one-time bootstrap)
- `bot/notifications.py` (reauth-required alert), `bot/handlers/commands.py` (masked `/status`), `config/onboarding.py` (reuse `mask_value`)
- `.env.example`

## Risks
- **Encryption-key loss is unrecoverable** — back up `TOKEN_ENCRYPTION_KEY` out-of-band; losing it forces interactive re-auth.
- **Stale-backup hazard** — restoring an old `/data` backup replays a consumed single-use refresh token → provider reuse-detection can revoke the token family. Volume backups are DR-only, never a rotation safety net.
- **Crash between consuming the old refresh token and the atomic write** bricks the single-use token → manual re-auth. Mitigated (temp+`os.replace`+`fsync`) not eliminated.
- **Unverified OAuth contract (BLOCKER)** — the real token-endpoint URL, scopes, whether it issues/rotates a refresh token, and Bearer-vs-custom are NOT in the codebase. Confirm against the actual `mcp__robinhood-trading__authenticate` flow before implementing `refresh()`. `offline_access` is mandatory or no refresh token is issued.
- **Single-instance ceiling** — in-process `RLock` + single-writer volume means one replica only. Fine given Telegram single-polling; document it.
- **Refresher must mutate the live Settings object** — `load_dotenv` runs only at import, so any code that reconstructs `Settings()` mid-process bypasses the refreshed token. Guard against new Settings instances for the broker.
