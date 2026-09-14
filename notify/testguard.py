"""Fail closed: no test may construct or use a channel that reaches a live API.

**Why this exists.** On 2026-09-13 the full suite was run with
``python -m unittest discover -s tests -p "test_*.py"`` *inside the production
bot container*. That environment holds a live ``RESEND_API_KEY``,
``PAGER_EMAIL_FROM``, ``PAGER_EMAIL_TO``, ``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID`` and ``NOTIFY_EMAIL_ENABLED=true``. The notification tests
took the real send path and delivered **12 real emails and Telegram messages**
to the owner's personal inbox, carrying test-fixture trade proposals with
subjects like ``[approval needed] NVDA — proposal 1``. He reasonably assumed he
was being phished by something wired up to a live brokerage.

Nothing was written to production: the tests use their own throwaway database.
The database was sandboxed; the *credentials* were not. This module sandboxes
the credentials.

The repo is public and MIT licensed, so "whoever runs the suite has a clean
environment" is not a control. Worse, ``config/settings.py`` calls
``load_dotenv(override=True)``, so a gitignored ``.env`` overrides an
already-clean exported environment (docs/OWNER_SETUP.md §6). The suite has to
refuse by construction.

Three layers, in the order they bite:

1. **Construction.** :func:`resolve_transport` is called from
   ``ResendChannel.__init__`` and ``TelegramChannel.__init__``. Armed, with no
   transport injected — meaning the channel would post to the real Resend or
   Telegram API — it raises :class:`LiveSenderRefused`.
2. **Send.** A test that legitimately needs real channel *objects* (the
   registry-policy tests assert which channels get built, never what they send)
   opts in with :func:`allow_inert_channels`, which builds them with
   :func:`refusing_transport` instead. The refusal then lands at send time.
3. **Environment.** :func:`neutralise_environment` blanks the delivery
   credentials in ``os.environ`` for the test process, so a code path nobody
   anticipated still has nothing to authenticate with.

**Why the refusal is a ``BaseException``.** ``notify``'s first rule is that a
channel never raises: ``ResendChannel.send``, ``TelegramChannel.send``,
``registry.broadcast`` and ``portfolio.paging.send_card`` all catch
``Exception`` and turn it into a logged ``False``. A guard that raised
``Exception`` would be swallowed into a silent no-op — which is precisely the
shape that hides a test believing it asserted a delivery. Deriving from
``BaseException`` means every one of those handlers lets it through untouched,
with no edit to any of them, the same way ``KeyboardInterrupt`` passes through.
``unittest`` reports it as a test error, loudly, without aborting the run.

This module imports nothing first-party on purpose: ``tests/__init__.py`` has
to be able to arm it before anything else is imported.
"""

from __future__ import annotations

import contextlib
import os
import sys

#: The delivery credentials :func:`neutralise_environment` blanks, mapped to the
#: value that makes the channel unbuildable rather than merely unset — an empty
#: string is a real value to pydantic-settings, and environment variables take
#: precedence over ``env_file``, so a blank here beats a populated ``.env``.
CREDENTIAL_ENV = {
    "RESEND_API_KEY": "",
    "PAGER_EMAIL_FROM": "",
    "PAGER_EMAIL_TO": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "NOTIFY_EMAIL_ENABLED": "false",
}

_INCIDENT = (
    "On 2026-09-13 the suite was run inside the production container, which holds "
    "live delivery credentials, and sent 12 real emails and Telegram messages "
    "carrying test-fixture trade proposals to the owner's personal inbox. The test "
    "database was a throwaway; the credentials were not."
)

_HOW_TO_FIX = """\
Fix it one of these two ways:

  * Inject a transport — this is what every existing notification test does:

        from tests import notifyfixture as nf
        channel = ResendChannel(..., transport=nf.RecordingTransport())

  * If you are asserting *which* channels the registry builds rather than what
    they send, wrap the call so the objects are built inert:

        from notify import testguard
        with testguard.allow_inert_channels():
            registry.configured_channels(settings)

    The channel objects are real; their transport refuses at send time.
"""


class LiveSenderRefused(BaseException):
    """A test tried to reach a real Resend or Telegram endpoint.

    Deliberately not an ``Exception``: see this module's docstring. Every
    ``send`` path in ``notify`` catches ``Exception`` and returns ``False``, so
    an ``Exception`` here would be downgraded to a silent non-delivery.
    """


_armed = False
_inert_depth = 0


def arm() -> None:
    """Turn the guard on. Called once, from ``tests/__init__.py``."""
    global _armed
    _armed = True


def disarm() -> None:
    """Turn the guard off. For the guard's own tests, and nothing else."""
    global _armed
    _armed = False


def is_armed() -> bool:
    return _armed


@contextlib.contextmanager
def allow_inert_channels():
    """Permit channel *construction*; the transport still refuses to send.

    The narrow, explicit opt-in for a test that needs real channel objects —
    ``registry.configured_channels`` returning ``["email", "telegram"]`` is a
    statement about policy, not about delivery. Nested uses are counted, so an
    inner block cannot re-enable live sending for an outer one.
    """
    global _inert_depth
    _inert_depth += 1
    try:
        yield
    finally:
        _inert_depth -= 1


def refusing_transport(*args, **kwargs):
    """A transport that cannot send. Raises :class:`LiveSenderRefused`."""
    raise LiveSenderRefused(
        "notify.testguard: a channel built under allow_inert_channels() tried to "
        "send.\n\n" + _INCIDENT + "\n\n"
        "allow_inert_channels() exists so a test can assert which channels the "
        "registry builds. It does not permit delivery. A test that means to assert "
        "what goes on the wire injects a recording transport instead.\n"
    )


def resolve_transport(channel_name: str, transport, live_transport):
    """The transport a channel should actually use, given the guard's state.

    ``transport`` is whatever the caller injected (``None`` for the default).
    ``live_transport`` is the module's real network transport. Passing the real
    one explicitly is treated exactly like passing nothing — it is the same
    request, spelled differently.
    """
    injected = transport is not None and transport is not live_transport
    if injected:
        return transport
    if not _armed:
        return transport or live_transport
    if _inert_depth > 0:
        return refusing_transport
    raise LiveSenderRefused(
        f"notify.testguard: refusing to build a live {channel_name!r} channel "
        f"inside the test suite.\n\n" + _INCIDENT + "\n\n" + _HOW_TO_FIX
    )


def neutralise_environment(environ=None, *, warn=True) -> list[str]:
    """Blank the delivery credentials for this process. Returns what was set.

    Belt to the guard's braces: the guard stops a channel being built, and this
    stops an unanticipated code path finding anything to authenticate with.

    Order matters at the call site. ``config/settings.py`` calls
    ``load_dotenv(override=True)`` at import time, which copies a gitignored
    ``.env`` over ``os.environ``; import it *before* calling this, or the
    scrub is undone. Blanking rather than deleting is what makes the scrub
    survive a later ``Settings()``: pydantic-settings reads environment
    variables ahead of ``env_file``, so an empty ``RESEND_API_KEY`` in the
    environment beats a populated one in ``.env``.
    """
    env = os.environ if environ is None else environ
    populated = [
        name
        for name in CREDENTIAL_ENV
        if str(env.get(name, "")).strip() not in ("", "false", "False", "0")
    ]
    for name, neutral in CREDENTIAL_ENV.items():
        env[name] = neutral
    if populated and warn:
        print(
            "notify.testguard: this environment holds live delivery credentials "
            f"({', '.join(sorted(populated))}). They have been blanked for the test "
            "process, and no test may build a live channel. See "
            "docs/NOTIFICATIONS.md §9.",
            file=sys.stderr,
        )
    return populated
