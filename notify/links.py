"""The signed link to a card page.

**The link is the credential.** ``/cards/<uid>?s=<hmac>`` needs no bearer token,
and the reasoning is worth writing down because it is a real trade:

- The page exposes exactly what the email that linked to it already contains,
  to the same single recipient's inbox. A leaked link leaks what a forwarded
  email leaks.
- It is read-only. There is no route under ``/cards`` that writes anything, and
  no path from it to an approval: approval is Telegram's signed, expiring,
  single-use callback, handled in the bot process, which the workspace cannot
  import (Spec L §6.1).
- Requiring a token would mean putting one in an email, which is strictly worse.

**It does not expire**, unlike an approval reference, and that is on purpose: an
approval is a decision about a book that moves, so a stale one is dangerous; a
card page is a record of what was said at a moment, so a stale one is the point.
The uid is 128 bits of ``secrets.token_hex`` and the signature is a full
SHA-256 HMAC — no truncation here, because nothing caps the length of a URL the
way Telegram caps ``callback_data`` at 64 bytes.

The key is ``CARD_LINK_SECRET``, falling back to ``EXECUTION_APPROVAL_SECRET``.
The fallback exists because production already carries the latter, so the
feature works the moment the flag goes on; the dedicated variable exists so the
two can be rotated independently, and they have very different lifetimes.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
from urllib.parse import quote

#: The domain separator. A signature over a card uid must never verify as a
#: signature over anything else that might one day be signed with the same key.
_PREFIX = "card\x1f"


class CardLinkUnavailable(Exception):
    """No signing key, so no link can be minted or verified.

    Raised rather than returning an unsigned link: a page reachable without a
    signature is not the same feature with a missing control, it is a different
    feature.
    """


def new_card_uid() -> str:
    """128 bits, hex. Unguessable on its own; the signature is the control."""
    return secrets.token_hex(16)


def card_secret(settings) -> str:
    """``CARD_LINK_SECRET``, else ``EXECUTION_APPROVAL_SECRET``, else refuse."""
    secret = (getattr(settings, "card_link_secret", "") or "").strip()
    if not secret:
        secret = (getattr(settings, "execution_approval_secret", "") or "").strip()
    if not secret:
        raise CardLinkUnavailable(
            "neither CARD_LINK_SECRET nor EXECUTION_APPROVAL_SECRET is set, so "
            "no card link can be signed or verified. See docs/ENV_SETUP.md."
        )
    return secret


def sign_uid(uid: str, secret: str) -> str:
    """The full hex HMAC-SHA256 over the domain-separated uid."""
    return hmac.new(
        secret.encode("utf-8"), f"{_PREFIX}{uid}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_uid(uid: str, presented: str, secret: str) -> bool:
    """Constant-time compare of the whole digest. No prefix match, ever."""
    presented = (presented or "").strip()
    if not presented:
        return False
    return hmac.compare_digest(sign_uid(uid, secret), presented)


def card_path(uid: str, signature: str) -> str:
    return f"/cards/{quote(str(uid), safe='')}?s={quote(str(signature), safe='')}"


def chart_path(uid: str, signature: str) -> str:
    return f"/cards/{quote(str(uid), safe='')}/chart.png?s={quote(str(signature), safe='')}"


def card_links(uid: str, *, settings) -> tuple[str, str]:
    """``(page_url, chart_url)`` — absolute when a base URL is configured.

    Returns ``("", "")`` when ``WORKSPACE_BASE_URL`` is unset. An email with no
    link is a degraded email; an email carrying a *relative* link is a broken
    one, and a fabricated host would be worse than both.
    """
    base = (getattr(settings, "workspace_base_url", "") or "").strip().rstrip("/")
    if not base:
        return "", ""
    signature = sign_uid(uid, card_secret(settings))
    return f"{base}{card_path(uid, signature)}", f"{base}{chart_path(uid, signature)}"
