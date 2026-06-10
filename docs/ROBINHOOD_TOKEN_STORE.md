# Robinhood OAuth Token Store

Swing Trader uses the MCP SDK's OAuth support for Robinhood Agentic Trading. The app does not ask for a Robinhood password, API key, or client secret.

## What Gets Stored

`database/token_store.py` implements the MCP SDK `TokenStorage` interface. It stores:

- OAuth access token metadata.
- Refresh token, when Robinhood issues one.
- Dynamic OAuth client registration metadata.
- Absolute token expiry metadata for status checks.

The payload is encrypted with Fernet and written to `robinhood_token.enc` beside the configured SQLite database. Local default:

```text
sqlite:///swing_trader.db -> ./robinhood_token.enc
```

Deployment example:

```text
sqlite:////data/swing_trader.db -> /data/robinhood_token.enc
```

The token file is ignored by git.

## Encryption Key

Generate a key:

```bash
python -m scripts.robinhood_auth --gen-key
```

Set it as `TOKEN_ENCRYPTION_KEY` in `.env` or your deployment secret manager.

Important: if this key is lost, existing encrypted token files cannot be read. Re-run the OAuth bootstrap.

## Bootstrap

Desktop flow:

```bash
python -m scripts.robinhood_auth
```

Headless flow:

```bash
python -m scripts.robinhood_auth --callback-file /tmp/robinhood-callback.txt
```

The bootstrap prints an authorization URL. Open it in a browser, approve access, and complete the callback. The script persists tokens through the encrypted store and prints a masked status report.

## Runtime Behavior

When `TOKEN_ENCRYPTION_KEY` is set, `RobinhoodMCPBroker` passes an `OAuthClientProvider` to the MCP streamable HTTP client. The SDK:

- Loads existing tokens from the encrypted store.
- Adds the `Authorization` header.
- Refreshes automatically when a refresh token is present and the token is expired.
- Persists rotated tokens through the encrypted store.

If the token is missing, expired, or cannot be refreshed, runtime calls fail closed with an operator re-auth message. The bot process does not launch an interactive browser.

If `TOKEN_ENCRYPTION_KEY` is not set, the broker preserves the advanced static-auth fallback using `ROBINHOOD_MCP_AUTH_TOKEN` and `ROBINHOOD_MCP_HEADERS_JSON`.

## Status

Check masked local status:

```bash
python -m scripts.robinhood_auth --status
```

Telegram `/broker` also reports whether the token store is configured, whether the encrypted file exists, whether a refresh token is present, and whether re-auth is needed.

## Security Notes

- Do not commit `.env`, `robinhood_token.enc`, OAuth redirect URLs, raw tokens, or account numbers.
- Back up `TOKEN_ENCRYPTION_KEY` out of band.
- Restore token-store backups carefully. Some OAuth providers rotate refresh tokens; restoring an old encrypted token file can require re-authentication.
- Public pull requests should test this path with fakes and unit tests, not live Robinhood credentials.
