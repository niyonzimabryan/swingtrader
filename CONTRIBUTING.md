# Contributing

Thanks for helping improve Swing Trader. This project automates research and paper-trading workflows, so changes should keep setup, provider fallbacks, and trading safety easy to verify.

## Local Setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use the setup wizard for real local runs:

```bash
.venv/bin/python -m scripts.setup_wizard
```

Do not commit `.env`, API keys, Telegram IDs, broker credentials, database files, generated logs, or local virtualenvs.

## Secret Handling and Rotation Runbook

CI runs `gitleaks` against the full git history on every pull request and push to `main`. Run it locally before opening a PR:

```bash
gitleaks git --redact --verbose .
```

If you accidentally commit a secret:

1. Stop using the exposed credential immediately. Do not paste the value into an issue, PR comment, chat, or log.
2. Revoke or rotate the credential at the provider before doing anything else:
   - Anthropic, Gemini, OpenAI, Finnhub, FMP, Alpha Vantage, FRED, Langfuse: revoke the leaked key and create a replacement in the provider dashboard.
   - Telegram: rotate the bot token with BotFather (`/revoke`) and update the bot deployment.
   - Alpaca: revoke the paper-trading key pair and create a new paper key pair. Never replace it with live-trading credentials as a shortcut.
3. Update the local `.env` and deployment variables with the replacement value. Never commit the new value.
4. Remove the secret from the commit or branch, then re-run `gitleaks git --redact --verbose .`.
5. If the secret reached `main` or any public fork, assume compromise even if the commit is later removed. Record which provider was rotated and when, but do not record the raw secret.

## Test Before Opening a PR

Run the same checks as CI:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q agents bot config data database execution memo orchestrator scanning scoring screening scripts tracking utils main.py
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

For changes that touch provider credentials or onboarding, also run:

```bash
.venv/bin/python -m scripts.doctor --skip-live
```

`scripts.doctor` requires a populated `.env`; CI intentionally does not run it because public pull requests should not need secrets.

## Test Priorities

Add or update tests when changing:

- Order execution, position state transitions, stops, targets, or risk checks.
- AI/provider response parsing, JSON recovery, model selection, and provider fallback behavior.
- Setup wizard schema, `.env` writing, required/optional key rules, and doctor validation.
- Telegram memo rendering, Markdown escaping, chunking, and plain-text fallback behavior.
- Scoring, scan-list gating, and escalation thresholds.

Prefer deterministic unit tests with fake provider responses. Live API smoke tests are useful locally, but they should not be required in CI.
