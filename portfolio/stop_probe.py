"""The live protective-exit probe record: read it here, write it from the script.

``docs/EXECUTION_LIFECYCLE.md`` §6 and Spec L §5.1 both say "until this probe
passes, live entries stay closed". Until this module existed, nothing said it to
the code: ``execution/lifecycle.py::live_gate_refusal`` was a two-condition
conjunction over two environment variables, and neither of them was the probe.

What the probe establishes is the one empirically unverified fact in the
Robinhood integration — that a ``gtc`` ``stop_market`` placed through the MCP is
visible in ``get_equity_orders`` the next session. The whole protective-exit
contract rests on ``ROBINHOOD_CAPABILITIES.can_place_standalone_gtc_stop``,
which is a constant in the adapter, declared and never checked. If it is wrong,
a live position has no automated exit.

**Why a row and not a flag.** ``ROBINHOOD_STOP_PROBE_PASSED=true`` would record
an assertion — a human writing down that something is so — and this repository
already has two of those in ``ALLOW_LIVE_TRADING`` and ``EXECUTION_MODE``. A row
written by :mod:`scripts.robinhood_stop_probe` records a *verification*: the
script read a real ``gtc`` ``stop_market`` back out of the live broker and stored
its order id and the moment it saw it. The script places nothing — the probe
itself is an owner action performed by hand (non-negotiable 1, §6) — it verifies
and records what the owner already did.

**Why it is keyed per account.** A probe proves something about the account it
ran in. A verified stop in the funded probe account says nothing about a second
Robinhood account, so the key is ``(broker, account_fingerprint)`` and a row for
one account never authorises another.

**Why absence is never read as probed.** Spec Q §12 invariant 1: absence or
invalidity of a record must mean *not probed*, never permission. Every function
here is a positive check, and every path that cannot reach an answer reports
"not probed" rather than silently passing.

**What "not probed" then costs is the owner's call.** Bryan ruled on 2026-09-15
that this gate is *advisory by default*: with
``ROBINHOOD_STOP_PROBE_REQUIRED`` unset (``False``), an unprobed live Robinhood
entry proceeds and :func:`refusal` logs a loud warning naming exactly what is
unverified and what a record would have contained. With it ``true``, the same
condition refuses, with the same codes and the same messages. The *finding* does
not move with the flag — only what is done about it — which is the whole reason
the warning is not optional. A claim nobody enforces and nobody prints is
invisible, and invisible was the defect this module was written to fix.

This module lives in ``portfolio/`` for the reason ``portfolio/owner_actions.py``
does: both the runtime and a recording script must import it, and ``portfolio/``
is the package both sides may reach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from database.models import BrokerStopProbe
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("stop_probe")

#: The broker whose protective exit is unverified. Alpaca paper is not gated —
#: a paper fill needs no Robinhood stop, and a Strategy Lab paper arm reaches
#: only Alpaca paper (Spec Q §11).
ROBINHOOD = "robinhood"

#: Refusal codes. Stable strings: the approval channel relays them verbatim and
#: an alert rule may match on them.
NOT_RECORDED = "robinhood_stop_probe_not_recorded"
ACCOUNT_UNKNOWN = "robinhood_stop_probe_account_unknown"
UNVERIFIABLE = "robinhood_stop_probe_unverifiable"

#: The structlog event emitted when the gate found a reason to refuse and the
#: owner has it advisory. Stable: an alert rule may match on it.
NOT_ENFORCED = "robinhood_stop_probe_not_enforced"

#: What a probe record carries, said in one line for the advisory warning. The
#: point of printing it is that "no record" is otherwise indistinguishable from
#: "nothing to record".
WOULD_RECORD = (
    "a broker_stop_probes row carrying the broker's order id for a gtc "
    "stop_market read back out of get_equity_orders, its symbol and stop price, "
    "and the moment it was observed"
)

_REMEDY = (
    "Run the probe by hand (docs/EXECUTION_LIFECYCLE.md §6), then record what "
    "you observed with `python scripts/robinhood_stop_probe.py --record`, which "
    "reads the gtc stop_market back out of get_equity_orders and writes the "
    "row. Nothing places the probe order for you."
)


def fingerprint(account_number: str) -> str:
    """The match key for an account: SHA-256 hex of its normalized number.

    Not the number, because this value lands in a table that is dumped into
    backups and quoted in log lines. Not a mask either: ``****1234`` can collide
    across accounts, and a collision in a gate reads as permission.
    """
    normalized = (account_number or "").strip().upper()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def mask(account_number: str) -> str:
    """``****1234`` — for a human reading a row, never for matching."""
    number = (account_number or "").strip()
    if len(number) <= 4:
        return "****"
    return f"****{number[-4:]}"


@dataclass(frozen=True)
class ProbeRecord:
    """One recorded observation, in the shape a caller wants to print."""

    broker: str
    account_masked: str
    observed_order_id: str
    observed_symbol: str
    observed_stop_price: float | None
    observed_at: datetime | None
    recorded_by: str
    note: str

    def as_dict(self) -> dict:
        return {
            "broker": self.broker,
            "account": self.account_masked,
            "observed_order_id": self.observed_order_id,
            "observed_symbol": self.observed_symbol,
            "observed_stop_price": self.observed_stop_price,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "recorded_by": self.recorded_by,
            "note": self.note,
        }


def _row(session, *, broker: str, account_number: str):
    key = fingerprint(account_number)
    if not key:
        return None
    return session.execute(
        select(BrokerStopProbe).where(
            BrokerStopProbe.broker == (broker or "").strip().lower(),
            BrokerStopProbe.account_fingerprint == key,
        )
    ).scalars().first()


def _is_valid(row) -> bool:
    """A row authorises only if it actually carries an observation.

    Two fields make it evidence rather than an empty placeholder: the broker's
    id for the order that was read back, and the moment it was read. A row
    missing either one records nothing, and invalidity means *not probed*.
    """
    if row is None:
        return False
    return bool((row.observed_order_id or "").strip()) and row.observed_at is not None


def find(session, *, broker: str, account_number: str) -> ProbeRecord | None:
    """The recorded probe for this ``(broker, account)``, or ``None``.

    ``None`` for a missing row *and* for a row that carries no observation —
    the caller must not be able to tell "no row" from "empty row" and treat one
    of them as permission.
    """
    row = _row(session, broker=broker, account_number=account_number)
    if not _is_valid(row):
        return None
    return ProbeRecord(
        broker=row.broker,
        account_masked=row.account_masked or "",
        observed_order_id=row.observed_order_id or "",
        observed_symbol=row.observed_symbol or "",
        observed_stop_price=row.observed_stop_price,
        observed_at=row.observed_at,
        recorded_by=row.recorded_by or "",
        note=row.note or "",
    )


def required(settings) -> bool:
    """Whether an unprobed live Robinhood entry is refused or merely warned.

    Defaults to ``False`` — the owner's ruling of 2026-09-15 — so a settings
    object that predates ``ROBINHOOD_STOP_PROBE_REQUIRED`` is advisory, which is
    also what the owner asked the default to be.
    """
    return bool(getattr(settings, "robinhood_stop_probe_required", False))


def refusal(settings, session) -> tuple[str, str] | None:
    """``(code, reason)`` when a live Robinhood entry must be refused.

    :func:`finding` is what decides whether the probe is missing; this decides
    what that costs. When :func:`required` is on, the finding is returned
    unchanged and the entry is refused. When it is off — the default — the
    finding is logged at WARNING and ``None`` is returned, so the entry
    proceeds.

    The warning is not optional in the advisory mode, and that is the point: the
    defect this module was written for was a safety claim that nothing enforced
    *and* nothing printed. Advisory-and-loud is a decision the owner can see in
    the logs every time it is taken; advisory-and-silent is the same invisible
    claim wearing a flag.
    """
    finding_ = finding(settings, session)
    if finding_ is None:
        return None
    if required(settings):
        return finding_

    code, reason = finding_
    account_number = str(getattr(settings, "robinhood_account_number", "") or "").strip()
    log.warning(
        NOT_ENFORCED,
        code=code,
        broker=ROBINHOOD,
        account=mask(account_number) if account_number else "(unset)",
        enforced=False,
        detail=(
            "a live Robinhood entry is proceeding with no recorded "
            "protective-exit probe. " + reason
        ),
        would_record=WOULD_RECORD,
        enforce_with="ROBINHOOD_STOP_PROBE_REQUIRED=true",
    )
    return None


def finding(settings, session) -> tuple[str, str] | None:
    """``(code, reason)`` when a live Robinhood entry has no probe behind it.

    ``None`` means either "this is not a gated placement" or "a probe is on
    record for this exact account". Everything else is a finding, including the
    two ways of not being able to answer the question:

    * ``session`` is ``None`` — a caller that cannot read the record cannot
      establish the fact, and an unestablished fact is not permission;
    * no account number is configured — there is nothing to key a probe to, so
      no probe can be on record for it.

    Only Robinhood is gated. Paper routes to the Alpaca paper adapter, which
    needs no Robinhood stop, and a Strategy Lab paper arm may reach only Alpaca
    paper (Spec Q §11) — gating those would be refusing a rehearsal because a
    different broker is unverified.
    """
    broker = str(getattr(settings, "broker_primary", "") or "").strip().lower()
    if broker != ROBINHOOD:
        return None

    if session is None:
        return (
            UNVERIFIABLE,
            "the Robinhood protective-exit probe record could not be read (no "
            "database session was available to the gate), so it cannot be "
            "established that a gtc stop_market survives at this broker. An "
            "unestablished fact is not permission (Spec Q §12 invariant 1).",
        )

    account_number = str(getattr(settings, "robinhood_account_number", "") or "").strip()
    if not account_number:
        return (
            ACCOUNT_UNKNOWN,
            "ROBINHOOD_ACCOUNT_NUMBER is not set, so there is no account for a "
            "protective-exit probe to be on record for. A live entry is refused. "
            + _REMEDY,
        )

    record = find(session, broker=ROBINHOOD, account_number=account_number)
    if record is not None:
        return None

    return (
        NOT_RECORDED,
        "no Robinhood protective-exit probe is recorded for this account "
        f"({mask(account_number)}). Until one is, it is unverified that a gtc "
        "stop_market placed through the MCP is visible in get_equity_orders the "
        "next session — which is the only thing standing between a live fill and "
        "a position with no automated exit (Spec L §5.1, "
        "docs/EXECUTION_LIFECYCLE.md §6). A probe recorded for a different "
        "account does not authorise this one. " + _REMEDY,
    )


def record(
    session,
    *,
    broker: str,
    account_number: str,
    observed_order_id: str,
    observed_at: datetime,
    observed_symbol: str = "",
    observed_stop_price: float | None = None,
    recorded_by: str = "",
    note: str = "",
) -> ProbeRecord:
    """Write (or refresh) the observation for one ``(broker, account)``.

    Called by :mod:`scripts.robinhood_stop_probe` and by nothing else. It takes
    an ``observed_order_id`` and an ``observed_at`` because those are the
    observation; a caller with neither has nothing to record, and this refuses
    rather than writing a row that would read as permission.
    """
    key = fingerprint(account_number)
    if not key:
        raise ValueError("an account number is required to key a probe record.")
    if not (observed_order_id or "").strip():
        raise ValueError(
            "a probe record needs the broker's id for the gtc stop_market that "
            "was read back. Without one there is no observation to record."
        )
    if observed_at is None:
        raise ValueError("a probe record needs the moment the order was observed.")

    name = (broker or "").strip().lower()
    row = _row(session, broker=name, account_number=account_number)
    if row is None:
        row = BrokerStopProbe(broker=name, account_fingerprint=key)
        session.add(row)
    row.account_masked = mask(account_number)
    row.observed_order_id = observed_order_id.strip()
    row.observed_symbol = (observed_symbol or "").strip().upper()
    row.observed_stop_price = observed_stop_price
    row.observed_at = observed_at
    row.recorded_by = (recorded_by or "")[:64]
    row.note = note or ""
    row.updated_at = utcnow_naive()
    session.flush()
    return ProbeRecord(
        broker=row.broker,
        account_masked=row.account_masked,
        observed_order_id=row.observed_order_id,
        observed_symbol=row.observed_symbol,
        observed_stop_price=row.observed_stop_price,
        observed_at=row.observed_at,
        recorded_by=row.recorded_by,
        note=row.note,
    )
