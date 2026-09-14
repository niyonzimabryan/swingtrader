"""The test package — and the one place the live delivery guard is armed.

This file was empty. It is now the seam, because it is the *only* module
guaranteed to be imported before any test in the suite: ``unittest discover -s
tests`` and CI's ``python -m unittest tests.test_foo tests.test_bar`` both
import the ``tests`` package first. There is no ``conftest.py`` here and a
pytest one would not run — the suite is unittest.

**What happened.** On 2026-09-13 the suite was run inside the production bot
container, whose environment holds live ``RESEND_API_KEY``,
``PAGER_EMAIL_FROM``, ``PAGER_EMAIL_TO``, ``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID`` and ``NOTIFY_EMAIL_ENABLED=true``. The notification tests
built real channels and delivered 12 real emails and Telegram messages,
carrying test-fixture trade proposals, to the owner's personal inbox. The
tests' own database was a throwaway; the credentials were not. See
:mod:`notify.testguard` for the full reasoning.

Two statements below, in this order, and the order is the point.
"""

from __future__ import annotations

# `config.settings` calls `load_dotenv(override=True)` at import time, copying a
# gitignored `.env` over whatever the process already had in `os.environ`
# (docs/OWNER_SETUP.md §6 names this trap). Import it *first* so that has
# already happened, then blank the credentials: environment variables take
# precedence over pydantic-settings' `env_file`, so the blanks win over `.env`
# for every `Settings()` the suite constructs afterwards.
import config.settings  # noqa: F401

from notify import testguard

testguard.neutralise_environment()
testguard.arm()
