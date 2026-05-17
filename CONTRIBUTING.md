# Contributing

Thanks for helping improve Swing Trader. This project automates research and paper-trading workflows, so changes should keep setup, provider fallbacks, and trading safety easy to verify.

Before contributing, read the [financial disclaimer](DISCLAIMER.md) and [MIT license](LICENSE). Do not submit changes that make live trading unattended or hide the human approval step.

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
