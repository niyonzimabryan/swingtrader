"""Spec L §6.6: where a quantity comes from, and every cap that bound it.

**An agent never chooses a quantity.** ``propose_order`` carries ``entry``,
``stop`` and a ``risk_fraction`` of equity; this module turns those into whole
shares. That is not a stylistic preference — sizing dominates selection in
realised P&L, and a number a model produced is not a statistic (Spec N §9).

The chain, in order, because the order is what makes the printed card honest:

1. **Validate the fraction.** ``risk_fraction`` is a *fraction*. A value at or
   above :data:`PERCENTAGE_FLOOR` (0.05) is a percentage typed as a fraction —
   ``0.5`` meaning "half a percent" — and is refused, not converted. Above
   ``RISK_FRACTION_HARD_CAP`` it is refused too, **not clamped**: silently
   halving what was asked for teaches the caller that the number does not
   matter.
2. **Scale by the evidence.** ``risk_fraction_effective = risk_fraction × m``
   where ``m = clip(LB / PE, 0, 1)`` for an evidenced proposal, and ``m`` is
   absent for a discretionary one (Spec L §6.6, :mod:`portfolio.evidence`).
3. **Apply the budget's per-trade cap** — the evidenced budget's or the
   discretionary budget's, never the other one's.
4. **Size.** ``shares = floor(risk_dollars / (entry - stop))``.
5. **Apply the notional caps** — concentration over the *combined* book, sector,
   the budget's remaining daily notional, and settled cash — each of which can
   only reduce the share count, and each of which is recorded with the value it
   bound to.
6. **Round down again, and refuse a zero.** Robinhood's ``stop_market`` is
   whole-share and regular-hours only (Spec L §5.1), so a protected entry is a
   whole-share order and a size that rounds to zero is ``risk_rejected`` with
   the reason rather than rounded up to one share.

Every cap that bound the size is returned, not just the binding one, because
"why is this smaller than I asked for" is the question the approval card exists
to answer.

Nothing here reaches a database, a broker, or a model. It is arithmetic over
values the caller has already read, which is what makes it testable and what
keeps the numbers reproducible from the stored row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: A ``risk_fraction`` at or above this is a percentage typed as a fraction.
PERCENTAGE_FLOOR = 0.05

#: Refusal codes. Stable strings; they are printed on the card.
INVALID_RISK_FRACTION = "invalid_risk_fraction"
PERCENTAGE_INPUT = "percentage_input"
RISK_FRACTION_ABOVE_HARD_CAP = "risk_fraction_above_hard_cap"
INVALID_STOP = "invalid_stop"
QUANTITY_NOT_ACCEPTED = "quantity_not_accepted"
ZERO_SHARES = "zero_shares"
EVIDENCE_SIZED_TO_ZERO = "evidence_sized_to_zero"

#: Cap names, in the order they are applied.
CAP_BUDGET_RISK = "budget_risk_cap"
CAP_CONCENTRATION = "concentration_cap"
CAP_SECTOR = "sector_cap"
CAP_DAILY_NOTIONAL = "daily_notional_cap"
CAP_SETTLED_CASH = "settled_cash"


class SizingRefused(Exception):
    """A size that cannot be computed, or that came out at zero shares."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = dict(detail or {})
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Caps:
    """The caps in force for one proposal, all in the same units as declared.

    ``daily_notional_remaining`` is the budget's own remaining headroom, already
    net of what the same budget has spent today. ``None`` on any field means
    "not configured", and an unconfigured cap does not bind — which is why each
    one is recorded on the row with the value it bound to rather than inferred
    later from settings that may since have changed.
    """

    budget_risk_cap: float
    hard_cap: float
    equity: float
    concentration_pct: float | None = None
    existing_symbol_value: float = 0.0
    sector_pct: float | None = None
    sector: str = "unknown"
    existing_sector_value: float = 0.0
    daily_notional_remaining: float | None = None
    settled_cash: float | None = None


@dataclass(frozen=True)
class SizeResult:
    """Whole shares, and the arithmetic that produced them."""

    quantity: int
    notional: float
    risk_dollars: float
    risk_fraction_requested: float
    risk_fraction_effective: float
    multiplier: float | None
    per_share_risk: float
    #: ``{cap_name: {"limit": float, "shares_allowed": int, "bound": bool}}``
    caps: dict = field(default_factory=dict)
    #: Names of the caps that actually reduced the size, in application order.
    binding: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "notional": round(self.notional, 2),
            "risk_dollars": round(self.risk_dollars, 2),
            "risk_fraction": self.risk_fraction_requested,
            "risk_fraction_effective": self.risk_fraction_effective,
            "m": self.multiplier,
            "per_share_risk": round(self.per_share_risk, 4),
            "caps": self.caps,
            "binding_caps": list(self.binding),
        }


def validate_risk_fraction(value, *, hard_cap: float, percentage_floor: float = PERCENTAGE_FLOOR) -> float:
    """Return ``value`` as a fraction, or raise :class:`SizingRefused`.

    Three refusals, deliberately distinct so the card can say which happened:
    a non-positive or non-numeric fraction, a percentage typed as a fraction,
    and a fraction above the hard cap. The third is a refusal and not a clamp
    (Spec L §6.6).
    """
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        raise SizingRefused(
            INVALID_RISK_FRACTION,
            f"risk_fraction={value!r} is not a number. It is a fraction of "
            "equity: 0.005 means half a percent.",
        ) from None
    if not math.isfinite(fraction) or fraction <= 0:
        raise SizingRefused(
            INVALID_RISK_FRACTION,
            f"risk_fraction={fraction!r} must be a positive fraction of equity; "
            "0.005 means half a percent.",
        )
    if fraction >= percentage_floor:
        raise SizingRefused(
            PERCENTAGE_INPUT,
            f"risk_fraction={fraction} looks like a percentage typed as a "
            f"fraction: anything at or above {percentage_floor} would risk "
            f"{fraction:.0%} of equity on one trade. Pass a fraction — "
            f"{fraction / 100:g} for {fraction:g}% — rather than having this "
            "converted for you, because the conversion is exactly the guess "
            "that should not be made on a live order (Spec L §6.6).",
            {"risk_fraction": fraction, "percentage_floor": percentage_floor},
        )
    if fraction > hard_cap:
        raise SizingRefused(
            RISK_FRACTION_ABOVE_HARD_CAP,
            f"risk_fraction={fraction} is above the hard cap of {hard_cap}. "
            "This is refused, not clamped: a size quietly reduced to the cap "
            "would be approved as though it were the size that was asked for.",
            {"risk_fraction": fraction, "hard_cap": hard_cap},
        )
    return fraction


def per_share_risk(entry: float, stop: float) -> float:
    """``entry - stop`` for a long, refusing the shapes that cannot be sized."""
    try:
        entry_price = float(entry)
        stop_price = float(stop)
    except (TypeError, ValueError):
        raise SizingRefused(INVALID_STOP, "entry and stop must both be numbers.") from None
    if not (math.isfinite(entry_price) and math.isfinite(stop_price)):
        raise SizingRefused(INVALID_STOP, "entry and stop must both be finite.")
    if entry_price <= 0:
        raise SizingRefused(INVALID_STOP, f"entry={entry_price} must be positive.")
    if stop_price <= 0:
        raise SizingRefused(INVALID_STOP, f"stop={stop_price} must be positive.")
    if stop_price >= entry_price:
        raise SizingRefused(
            INVALID_STOP,
            f"stop={stop_price} is not below entry={entry_price}. This side is "
            "long only, and a long position's protective stop sits below the "
            "entry; with the two the other way round there is no risk per "
            "share to divide by.",
            {"entry": entry_price, "stop": stop_price},
        )
    return entry_price - stop_price


def compute_size(
    *,
    entry: float,
    stop: float,
    risk_fraction: float,
    multiplier: float | None,
    caps: Caps,
    sizes_to_zero: bool = False,
) -> SizeResult:
    """The §6.6 chain. Raises :class:`SizingRefused` at zero shares.

    ``multiplier`` is ``m`` for an evidenced proposal and ``None`` for a
    discretionary one — ``None`` and ``1.0`` are not the same statement, and
    only the second one claims the evidence supported full size.

    ``sizes_to_zero`` is ``strict`` mode's answer to ``LB <= 0`` (Spec L §6.6).
    It is honoured here rather than by the caller so that the refusal carries
    the same shape as every other zero-share refusal.
    """
    unit_risk = per_share_risk(entry, stop)
    entry_price = float(entry)

    effective = risk_fraction * (1.0 if multiplier is None else float(multiplier))
    cap_records: dict = {}
    binding: list[str] = []

    if sizes_to_zero:
        raise SizingRefused(
            EVIDENCE_SIZED_TO_ZERO,
            "EVIDENCE_GATE_MODE=strict and the cited answer's lower bound does "
            "not exclude zero, so this proposal sizes to zero shares. In "
            "advisory mode — the default — it would be re-labelled "
            "discretionary and sized from that budget instead.",
            {"risk_fraction_effective": 0.0},
        )

    budget_cap = float(caps.budget_risk_cap)
    if effective > budget_cap:
        cap_records[CAP_BUDGET_RISK] = {
            "limit": budget_cap,
            "requested": effective,
            "bound": True,
        }
        binding.append(CAP_BUDGET_RISK)
        effective = budget_cap
    else:
        cap_records[CAP_BUDGET_RISK] = {
            "limit": budget_cap,
            "requested": effective,
            "bound": False,
        }

    equity = float(caps.equity or 0.0)
    if equity <= 0:
        raise SizingRefused(
            ZERO_SHARES,
            "portfolio equity reads as zero or negative, so a risk fraction of "
            "it is zero dollars. Sync the ledger before proposing.",
            {"equity": equity},
        )
    risk_dollars = effective * equity
    shares = int(math.floor(risk_dollars / unit_risk))

    def _apply(name: str, limit_notional: float | None, extra: dict | None = None) -> None:
        nonlocal shares
        if limit_notional is None:
            return
        allowed = int(math.floor(max(0.0, float(limit_notional)) / entry_price))
        record = {
            "limit_notional": round(float(limit_notional), 2),
            "shares_allowed": allowed,
            "bound": allowed < shares,
        }
        record.update(extra or {})
        cap_records[name] = record
        if allowed < shares:
            binding.append(name)
            shares = allowed

    if caps.concentration_pct is not None:
        headroom = float(caps.concentration_pct) * equity - float(caps.existing_symbol_value or 0.0)
        _apply(
            CAP_CONCENTRATION,
            headroom,
            {
                "limit_pct": float(caps.concentration_pct),
                "existing_value": round(float(caps.existing_symbol_value or 0.0), 2),
                "note": (
                    "counted over the combined book across every account, "
                    "including read-only ones (Spec L §5.1)."
                ),
            },
        )
    if caps.sector_pct is not None:
        headroom = float(caps.sector_pct) * equity - float(caps.existing_sector_value or 0.0)
        _apply(
            CAP_SECTOR,
            headroom,
            {
                "limit_pct": float(caps.sector_pct),
                "sector": caps.sector,
                "existing_value": round(float(caps.existing_sector_value or 0.0), 2),
            },
        )
    if caps.daily_notional_remaining is not None:
        _apply(
            CAP_DAILY_NOTIONAL,
            caps.daily_notional_remaining,
            {"note": "this budget's own remaining daily notional; the two budgets never share."},
        )
    if caps.settled_cash is not None:
        _apply(
            CAP_SETTLED_CASH,
            caps.settled_cash,
            {"note": "cash account: only settled cash may be committed (Spec L §5.1)."},
        )

    if shares <= 0:
        raise SizingRefused(
            ZERO_SHARES,
            "the size rounds down to zero whole shares. A protective "
            "`stop_market` is whole-share only, so this cannot be rounded up "
            "and the proposal is refused with the arithmetic shown "
            "(Spec L §6.6).",
            {
                "risk_dollars": round(risk_dollars, 2),
                "per_share_risk": round(unit_risk, 4),
                "caps": cap_records,
            },
        )

    return SizeResult(
        quantity=shares,
        notional=shares * entry_price,
        risk_dollars=shares * unit_risk,
        risk_fraction_requested=float(risk_fraction),
        risk_fraction_effective=effective,
        multiplier=None if multiplier is None else float(multiplier),
        per_share_risk=unit_risk,
        caps=cap_records,
        binding=tuple(binding),
    )
