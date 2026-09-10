"""The ``BrokerCapabilities`` contract, and the gate that reads it (Spec L §5).

Every adapter declares what it can do, and **callers check capabilities before
intent** — the rule from Spec Q §12, restated in Spec L §5 because it is the
single most expensive mistake available. An in-process watcher is not
protection: it disappears with the process.

Two capabilities describe protective exits, and the distinction is the whole
point of this module:

``can_place_attached_stop``
    a stop attached to the entry — bracket, OCO, OTO — placed as one order and
    guaranteed by the broker to exist the moment the entry fills.
``can_place_standalone_gtc_stop``
    a separate ``stop_market`` order, ``gtc``, placed *after* the fill.

Spec L v0.1 assumed the first. The 2026-09-08 dump of the Robinhood MCP's own
``tools/list`` schema settled it the other way: ``type`` is one of ``market``,
``limit``, ``stop_market``, ``stop_limit``, and there is no bracket, OCO, OTO,
or attach-stop parameter anywhere in the schema. So Robinhood declares
``can_place_attached_stop=False`` — and it stays false — while
``can_place_standalone_gtc_stop=True`` is the capability that actually carries
the protection.

That is not a downgrade of the rule, it is a relocation of the risk. An
attached stop has no window; a standalone stop has one, between the entry
filling and the stop being read back from the broker. Spec L §5.1 assigns that
window to the execution service (fill → place stop with its own ``ref_id`` →
read it back through ``get_equity_orders`` → only then is the position
``protected``), and Phase 1 places nothing at all.

:func:`gate_intent` is what a caller runs *before* forming an order. It refuses
on the declaration, not on a failed placement — a capability check that happens
after the money has moved is not a check.

**Why this lives in ``portfolio/`` and not in ``execution/brokers/``.** Phase 6's
``propose_order`` must run this gate before it forms a proposal, and that tool is
reachable from the MCP surface, which may never import ``execution/`` (Spec L
§6.1). The module moved here so there is one gate rather than two copies of the
same three refusals; ``execution/brokers/capabilities.py`` re-exports it and
every adapter still imports from there. The declaration a proposal gates on is
the one recorded on ``brokerage_accounts.capabilities_json`` at the daily probe,
rebuilt with :meth:`BrokerCapabilities.from_dict` — so the check needs no live
broker connection, which is the other half of why it can sit on this side of the
boundary at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: A protective exit must outlive our process. Either capability satisfies
#: that; an in-process watcher satisfies neither and is not listed.
PROTECTIVE_EXIT_CAPABILITIES = ("can_place_attached_stop", "can_place_standalone_gtc_stop")


@dataclass(frozen=True)
class BrokerCapabilities:
    """What one adapter can actually do. Declared, recorded, and re-probed daily.

    Stored on ``brokerage_accounts.capabilities_json`` so a caller does not need
    a live broker connection to know what the account supports, and so a
    capability that changed under us is visible as a diff rather than as a
    rejected order.
    """

    #: Reads.
    can_read_positions: bool = False
    can_read_tax_lots: bool = False
    can_read_orders: bool = False
    can_read_cash: bool = False
    #: Writes. Nothing in Phase 1 consults these to *do* anything; they are
    #: declared so the gate below can refuse an intent before Phase 6 exists.
    can_place_equity_market: bool = False
    can_place_equity_limit: bool = False
    can_place_attached_stop: bool = False
    can_place_standalone_gtc_stop: bool = False
    can_place_bracket: bool = False
    #: Shape constraints the schema imposes (Spec L §5.1).
    supports_fractional: bool = False
    #: `stop_market`/`stop_limit` are regular-hours and whole-share only, so a
    #: protected entry is always a whole-share order.
    stops_regular_hours_only: bool = True
    stops_whole_shares_only: bool = True
    market_hours_only: bool = True
    #: Specified-lot selling, `get_equity_tax_lots` -> the sell order.
    supports_specified_lot_sale: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "BrokerCapabilities":
        """Rebuild from a stored ``capabilities_json`` blob.

        Unknown keys are dropped rather than raising: a row written by an older
        revision must still be readable, and a capability this build does not
        know about is one it cannot act on anyway.
        """
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: bool(v) for k, v in (raw or {}).items() if k in known})

    @property
    def can_protect_a_position(self) -> bool:
        """True when *some* exit that outlives our process is available."""
        return any(getattr(self, name) for name in PROTECTIVE_EXIT_CAPABILITIES)


class CapabilityRefused(RuntimeError):
    """An intent refused on the adapter's declaration, before placement."""

    def __init__(self, reason: str, missing: tuple[str, ...] = ()):
        self.reason = reason
        self.missing = tuple(missing)
        super().__init__(reason)


@dataclass(frozen=True)
class OrderIntent:
    """What a caller wants, before it becomes an order.

    ``requires_protective_exit`` is the flag that matters: an unattended entry
    without one may not be opened at all (Spec L §5, Spec Q §12).
    """

    symbol: str
    side: str = "buy"
    order_type: str = "market"
    quantity: float | None = None
    dollar_amount: float | None = None
    requires_protective_exit: bool = True
    extended_hours: bool = False


def gate_intent(capabilities: BrokerCapabilities, intent: OrderIntent) -> None:
    """Raise :class:`CapabilityRefused` if the adapter cannot serve ``intent``.

    Called before an order is formed, never after one is rejected.
    """
    if intent.requires_protective_exit and not capabilities.can_protect_a_position:
        raise CapabilityRefused(
            "capability_refused: this adapter declares neither "
            "can_place_attached_stop nor can_place_standalone_gtc_stop, so a "
            "protective exit that outlives this process cannot be placed. No "
            "unattended entry may be opened on it (Spec L §5).",
            PROTECTIVE_EXIT_CAPABILITIES,
        )

    if intent.order_type == "market" and not capabilities.can_place_equity_market:
        raise CapabilityRefused(
            "capability_refused: this adapter does not declare "
            "can_place_equity_market.",
            ("can_place_equity_market",),
        )
    if intent.order_type == "limit" and not capabilities.can_place_equity_limit:
        raise CapabilityRefused(
            "capability_refused: this adapter does not declare "
            "can_place_equity_limit.",
            ("can_place_equity_limit",),
        )

    fractional = _is_fractional(intent)
    if fractional and not capabilities.supports_fractional:
        raise CapabilityRefused(
            "capability_refused: this adapter does not support fractional "
            "quantities.",
            ("supports_fractional",),
        )
    # A protected entry is a whole-share order wherever stops are whole-share
    # only: sizing to 1.5 shares and then protecting one of them is the failure
    # this refuses (Spec L §5.1, §6.6 rounds down).
    if (
        intent.requires_protective_exit
        and capabilities.stops_whole_shares_only
        and (fractional or intent.dollar_amount is not None)
    ):
        raise CapabilityRefused(
            "capability_refused: protective stops on this adapter are "
            "whole-share only, so a protected entry may not carry a fractional "
            "quantity or a dollar_amount.",
            ("stops_whole_shares_only",),
        )
    if (
        intent.requires_protective_exit
        and intent.extended_hours
        and capabilities.stops_regular_hours_only
    ):
        raise CapabilityRefused(
            "capability_refused: protective stops on this adapter are rejected "
            "outside regular hours, so a protected entry may not be an "
            "extended-hours order.",
            ("stops_regular_hours_only",),
        )


def _is_fractional(intent: OrderIntent) -> bool:
    return intent.quantity is not None and float(intent.quantity) != int(intent.quantity)
