"""``propose_order``: everything that happens before a human sees a card.

Spec L §6 in one sentence: *an agent session proposes, the owner approves out of
band, and code — not an agent — executes*. This module is the first third of
that. It creates a row and sends a card. **It places nothing, and it cannot: it
is inside the workspace's import closure and that closure may not reach a broker
adapter** (Spec L §6.1, asserted by ``tests/test_no_execute_scope.py`` and
``tests/test_portfolio_import_graph.py``, not by this docstring).

The order of the guards is the order they run in, and it is chosen so that the
most structural refusals happen first and the most expensive last:

1. **Freshness** (:func:`portfolio.freshness.require_fresh`). A stale ledger
   refuses rather than serves on any path that feeds a proposal — a stale answer
   is a bad answer, a stale *position size* is a real trade against a book that
   no longer exists.
2. **The account.** ``agent_placeable=False`` means read-only to this workspace.
   Robinhood enforces this too, but a proposal that fails at placement has
   already been shown to a human as though it were actionable.
3. **Capabilities**, from the account's recorded declaration
   (:func:`portfolio.capabilities.gate_intent`) — checked before intent, never
   after a rejected order.
4. **The kill switch, and an unprotected position** (:mod:`portfolio.killswitch`).
   A courtesy here; the control is at approval-to-placement.
5. **The citation rule and §6.6's evidence scaling**
   (:mod:`portfolio.evidence`). In advisory mode nothing is suppressed for lack
   of evidence; it is re-labelled ``discretionary`` and the evidence is printed
   in full, negative lower bound included.
6. **Sizing** (:mod:`portfolio.sizing`): the fraction validation, the two
   budgets' per-trade caps, concentration and sector over the **combined** book,
   the budget's own remaining daily notional, settled cash, whole-share rounding
   and the zero-share refusal.
7. **Settled cash** (:func:`portfolio.guards.check_settled_cash`), which refuses
   with the settlement date rather than with "insufficient funds".

A refusal is not an error. A ``risk_rejected`` proposal is written down, given a
reason, and **returned** — so the agent can say why an idea is not actionable
rather than silently dropping the name from a list (Spec L §6.4). What it never
gets is an approval reference, so there is nothing to approve.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from database.models import (
    PROPOSAL_OPEN_STATUSES,
    BrokerageAccount,
    Proposal,
    Ticker,
)
from portfolio import approvals as approvals_mod
from portfolio import evidence as evidence_mod
from portfolio import killswitch
from portfolio.capabilities import BrokerCapabilities, CapabilityRefused, OrderIntent, gate_intent
from portfolio.exposure import aggregate_exposure
from portfolio.freshness import DEFAULT_FRESHNESS_BUDGET_MINUTES, budget_from_settings
from portfolio.guards import (
    RiskRejection,
    check_account_placeable,
    check_ledger_fresh,
    check_settled_cash,
)
from portfolio.ledger import (
    current_accounts,
    current_cash,
    current_holdings,
    exposure_tags,
    ledger_as_of,
)
from portfolio.settlement import settlement_view
from portfolio.sizing import Caps, SizingRefused, compute_size, validate_risk_fraction
from utils.timeutils import utcnow_naive

PROPOSED = "proposed"
RISK_REJECTED = "risk_rejected"

#: Additional refusal codes this module owns. The rest come from
#: :mod:`portfolio.guards`, :mod:`portfolio.sizing` and
#: :mod:`portfolio.capabilities`.
NO_PLACEABLE_ACCOUNT = "no_agent_placeable_account"
CAPABILITY_REFUSED = "capability_refused"
UNSUPPORTED_SIDE = "unsupported_side"
INVALID_TICKER = "invalid_ticker"
EVIDENCE_GATE_REFUSED = "evidence_gate_refused"


class ProposalRefused(Exception):
    """A refusal raised before a row could be written at all.

    Reserved for inputs that are not a proposal — a missing ticker, a
    ``quantity`` argument, a short side. A refusal *about the book* is a
    ``risk_rejected`` row instead, because that one is a finding worth keeping.
    """

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = dict(detail or {})
        super().__init__(f"{code}: {message}")


# --------------------------------------------------------------------------- #
# The portfolio context a proposal is sized against
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PortfolioContext:
    """One read of the combined book, and its hash (Spec L §6.2).

    Every figure spans **every** account the ledger knows about, including
    read-only ones: a name held in the primary account is counted when sizing an
    Agentic-account proposal in the same name, or the concentration cap is
    blind to half the book (Spec L §5.1).
    """

    as_of_utc: datetime | None
    equity: float
    settled_cash: float
    unsettled_cash: float
    pending_settlements: tuple
    symbol_values: dict
    sector_values: dict
    sectors: dict
    context_hash: str
    account_id: int | None = None
    account_label: str = ""
    account_type: str = "cash"
    capabilities: BrokerCapabilities = field(default_factory=BrokerCapabilities)
    accounts_included: tuple = ()

    def symbol_value(self, symbol: str) -> float:
        return float(self.symbol_values.get(symbol.upper(), 0.0))

    def sector_of(self, symbol: str) -> str:
        return self.sectors.get(symbol.upper(), "unknown")

    def sector_value(self, sector: str) -> float:
        return float(self.sector_values.get(sector, 0.0))


def _sector_map(session, symbols) -> dict:
    """Symbol -> sector from the local reference table.

    A symbol with no row is ``"unknown"`` rather than dropped, and every unknown
    shares one bucket — which makes the sector cap conservative for names the
    reference table has never seen, not permissive.
    """
    wanted = {s.upper() for s in symbols if s}
    if not wanted:
        return {}
    rows = session.query(Ticker).filter(Ticker.symbol.in_(sorted(wanted))).all()
    return {row.symbol.upper(): (row.sector or "unknown") for row in rows}


def read_context(session, *, account=None, now: datetime | None = None, symbol: str = "") -> PortfolioContext:
    """Read the combined book once, and hash it.

    ``account`` is the account an order would be placed in; when omitted the
    first ``agent_placeable`` enabled account is used, which is the Agentic
    account in every deployment this phase targets.
    """
    now = now or utcnow_naive()
    accounts = current_accounts(session, enabled_only=True)
    labels = {a.id: (a.label or f"{a.broker}:{a.external_account_id}") for a in accounts}
    account_ids = [a.id for a in accounts]

    holdings = current_holdings(session, account_ids)
    cash_rows = current_cash(session, account_ids)
    tags = exposure_tags(session)
    as_of = ledger_as_of(accounts, holdings, cash_rows)

    symbols = {h.symbol.upper() for h in holdings}
    if symbol:
        symbols.add(symbol.upper())
    sectors = _sector_map(session, symbols)

    cash_total = sum(
        float(row.settled_cash or 0.0) + float(row.unsettled_cash or 0.0) for row in cash_rows
    )
    exposure = aggregate_exposure(
        holdings, cash_total=cash_total, sectors=sectors, tags=tags, account_labels=labels
    )

    if account is None:
        account = next((a for a in accounts if a.agent_placeable), None)

    # Settled cash is the placement account's own, not the book's: T+1
    # settlement is a property of that account (Spec L §5.1), and another
    # account's settled cash cannot fund an Agentic-account order.
    placement_cash = [row for row in cash_rows if account is not None and row.account_id == account.id]
    settled = sum(float(row.settled_cash or 0.0) for row in placement_cash)
    unsettled = sum(float(row.unsettled_cash or 0.0) for row in placement_cash)
    pending: list = []
    for row in placement_cash:
        pending.extend(row.pending_settlements)

    symbol_values = {row.symbol: float(row.value) for row in exposure.by_symbol}
    sector_values = dict(exposure.by_sector)

    capabilities = BrokerCapabilities.from_dict(getattr(account, "capabilities", {}) if account else {})

    return PortfolioContext(
        as_of_utc=as_of,
        equity=float(exposure.total_value),
        settled_cash=settled,
        unsettled_cash=unsettled,
        pending_settlements=tuple(pending),
        symbol_values=symbol_values,
        sector_values=sector_values,
        sectors=sectors,
        context_hash=context_hash(
            as_of=as_of,
            equity=exposure.total_value,
            settled_cash=settled,
            symbol_values=symbol_values,
        ),
        account_id=getattr(account, "id", None),
        account_label=getattr(account, "label", "") or "",
        account_type=getattr(account, "account_type", "cash") or "cash",
        capabilities=capabilities,
        accounts_included=tuple(labels[a.id] for a in accounts),
    )


def context_hash(*, as_of, equity: float, settled_cash: float, symbol_values: dict) -> str:
    """A stable hash of the state a proposal was computed against.

    Rounded to cents before hashing so that a re-read of an unchanged book
    produces the same hash: an unrounded float that differs in the fifteenth
    decimal would make "the book changed" true on every call and therefore
    meaningless.
    """
    payload = {
        "as_of_utc": as_of.replace(microsecond=0).isoformat() if as_of else None,
        "equity": round(float(equity), 2),
        "settled_cash": round(float(settled_cash), 2),
        "positions": {k: round(float(v), 2) for k, v in sorted(symbol_values.items())},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


def budget_caps(settings, budget: str) -> tuple[float, float | None]:
    """``(per_trade_risk_cap, daily_notional)`` for one budget.

    The two budgets never share a number. ``None`` for the daily notional means
    "not configured", and an unconfigured cap does not bind — it is recorded as
    absent on the row rather than silently treated as zero, which would refuse
    every proposal on a fresh deployment.
    """
    if budget == evidence_mod.EVIDENCED:
        cap = float(getattr(settings, "evidenced_risk_cap", 0.01) or 0.01)
        daily = float(getattr(settings, "evidenced_daily_notional", 0.0) or 0.0)
    else:
        cap = float(getattr(settings, "discretionary_risk_cap", 0.0025) or 0.0025)
        daily = float(getattr(settings, "discretionary_daily_notional", 0.0) or 0.0)
    return cap, (daily if daily > 0 else None)


def notional_used_today(session, budget: str, *, on_date: date) -> float:
    """Notional this budget has already committed today.

    Counts every proposal in a state where an order exists or may exist, which
    is what makes concurrent approvals unable to overspend the cap (Spec Q §12
    invariant 6): a proposal reserves its notional the moment it is approved,
    not when the fill comes back.
    """
    start = datetime(on_date.year, on_date.month, on_date.day)
    rows = (
        session.query(Proposal)
        .filter(Proposal.budget == budget)
        .filter(Proposal.status.in_(PROPOSAL_OPEN_STATUSES))
        .filter(Proposal.created_at >= start)
        .filter(Proposal.created_at < start + timedelta(days=1))
        .all()
    )
    return sum(float(row.notional or 0.0) for row in rows)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass
class Evaluation:
    """What every guard said, whether or not the proposal survived."""

    ticker: str
    entry: float
    stop: float
    risk_fraction: float
    expected_hold_sessions: int | None
    context: PortfolioContext
    assessment: evidence_mod.EvidenceAssessment
    size = None
    rejection: RiskRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None and self.size is not None


def evaluate(
    session,
    *,
    ticker: str,
    entry: float,
    stop: float,
    risk_fraction,
    cohort_answer_id: str = "",
    expected_hold_sessions: int | None = None,
    settings,
    now: datetime | None = None,
    account=None,
    resolver=None,
    quantity=None,
) -> Evaluation:
    """Run every guard and, if they all pass, compute the size.

    Called twice in the life of one order: once by ``propose_order`` to build
    the card, and again by the execution service **from fresh state** at
    approval time. It never reuses the proposal's stored numbers, which is
    Spec L §6.2's second invariant, and the reason this function takes a session
    rather than a context.
    """
    now = now or utcnow_naive()
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise ProposalRefused(INVALID_TICKER, "a proposal names a ticker.")
    if quantity is not None:
        raise ProposalRefused(
            "quantity_not_accepted",
            "propose_order does not take a quantity. Size is computed by code "
            "from entry, stop and risk_fraction under the caps — an agent never "
            "chooses a quantity (Spec L §6.6).",
        )

    context = read_context(session, account=account, now=now, symbol=symbol)
    on_date = (context.as_of_utc or now).date()

    def refuse(rejection: RiskRejection) -> Evaluation:
        return Evaluation(
            ticker=symbol,
            entry=float(entry) if _is_number(entry) else 0.0,
            stop=float(stop) if _is_number(stop) else 0.0,
            risk_fraction=float(risk_fraction) if _is_number(risk_fraction) else 0.0,
            expected_hold_sessions=expected_hold_sessions,
            context=context,
            assessment=assessment,
            rejection=rejection,
        )

    # The evidence assessment runs early and unconditionally: the card prints it
    # whichever way the guards go, so a refused proposal still shows the owner
    # what the evidence said (Spec L §6.6 — print the evidence either way).
    assessment = evidence_mod.assess(
        ticker=symbol,
        cohort_answer_id=cohort_answer_id,
        expected_hold_sessions=expected_hold_sessions,
        on_date=on_date,
        settings=settings,
        resolver=resolver,
    )

    stale = check_ledger_fresh(
        context.as_of_utc, now, budget_minutes=budget_from_settings(settings, DEFAULT_FRESHNESS_BUDGET_MINUTES)
    )
    if stale is not None:
        return refuse(stale)

    if context.account_id is None:
        return refuse(
            RiskRejection(
                NO_PLACEABLE_ACCOUNT,
                "no enabled account is marked agent_placeable, so there is "
                "nowhere a proposal could be placed. Placement is confined to "
                "the Robinhood Agentic account (Spec L §5.1).",
            )
        )
    account_row = session.get(BrokerageAccount, context.account_id)
    not_placeable = check_account_placeable(account_row)
    if not_placeable is not None:
        return refuse(not_placeable)

    try:
        gate_intent(
            context.capabilities,
            OrderIntent(
                symbol=symbol,
                side="buy",
                order_type="limit",
                quantity=1.0,
                requires_protective_exit=True,
                extended_hours=False,
            ),
        )
    except CapabilityRefused as exc:
        return refuse(
            RiskRejection(
                CAPABILITY_REFUSED,
                str(exc),
                {"missing_capabilities": list(exc.missing), "account": context.account_label},
            )
        )

    blocked = killswitch.entry_block(session)
    if blocked is not None:
        code, reason = blocked
        return refuse(RiskRejection(code, reason))

    if assessment.refused:
        return refuse(
            RiskRejection(
                EVIDENCE_GATE_REFUSED,
                f"EVIDENCE_GATE_MODE=strict and the citation rule refused this "
                f"proposal: {assessment.reason}",
                assessment.as_dict(),
            )
        )

    hard_cap = float(getattr(settings, "risk_fraction_hard_cap", 0.01) or 0.01)
    floor = float(getattr(settings, "risk_fraction_percentage_floor", 0.05) or 0.05)
    try:
        fraction = validate_risk_fraction(risk_fraction, hard_cap=hard_cap, percentage_floor=floor)
    except SizingRefused as exc:
        return refuse(RiskRejection(exc.code, exc.message, exc.detail))

    per_trade_cap, daily_notional = budget_caps(settings, assessment.budget)
    remaining = None
    if daily_notional is not None:
        remaining = max(0.0, daily_notional - notional_used_today(session, assessment.budget, on_date=on_date))

    sector = context.sector_of(symbol)
    caps = Caps(
        budget_risk_cap=per_trade_cap,
        hard_cap=hard_cap,
        equity=context.equity,
        concentration_pct=_optional_pct(settings, "proposal_max_position_pct"),
        existing_symbol_value=context.symbol_value(symbol),
        sector_pct=_optional_pct(settings, "proposal_max_sector_pct"),
        sector=sector,
        existing_sector_value=context.sector_value(sector),
        daily_notional_remaining=remaining,
        # Settled cash is deliberately NOT a sizing cap. On the cash Agentic
        # account it is a *refusal* gate (`check_settled_cash` below): a
        # proposal whose notional would need T+1 proceeds is refused with the
        # settlement date, not silently shrunk to whatever has settled — a
        # shrunk order hides the fact that the size the risk math produced could
        # not actually be funded today (Spec L §5.1, §8 `test_unsettled_cash_rejected`).
        settled_cash=None,
    )

    try:
        size = compute_size(
            entry=entry,
            stop=stop,
            risk_fraction=fraction,
            multiplier=assessment.multiplier,
            caps=caps,
            sizes_to_zero=assessment.sizes_to_zero,
        )
    except SizingRefused as exc:
        return refuse(RiskRejection(exc.code, exc.message, exc.detail))

    unsettled = check_settled_cash(
        size.notional,
        settled_cash=context.settled_cash,
        unsettled_cash=context.unsettled_cash,
        pending_settlements=context.pending_settlements,
        on_date=on_date,
    )
    if unsettled is not None:
        return refuse(unsettled)

    evaluation = Evaluation(
        ticker=symbol,
        entry=float(entry),
        stop=float(stop),
        risk_fraction=fraction,
        expected_hold_sessions=expected_hold_sessions,
        context=context,
        assessment=assessment,
    )
    evaluation.size = size
    return evaluation


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _optional_pct(settings, name: str) -> float | None:
    value = getattr(settings, name, None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- #
# Creating the row and the card
# --------------------------------------------------------------------------- #


def create_proposal(
    session,
    *,
    ticker: str,
    entry: float,
    stop: float,
    risk_fraction,
    cohort_answer_id: str = "",
    expected_hold_sessions: int | None = None,
    side: str = "long",
    settings,
    requester_token_label: str = "",
    owner_id: str = "",
    now: datetime | None = None,
    resolver=None,
    quantity=None,
) -> Proposal:
    """Evaluate, write the row, mint the approval, send the card. Place nothing.

    A ``risk_rejected`` proposal is written and returned like any other, and is
    given **no** approval reference — there is nothing to approve, and the row
    exists so the reason can be shown and counted later.
    """
    now = now or utcnow_naive()
    if (side or "long").strip().lower() != "long":
        raise ProposalRefused(
            UNSUPPORTED_SIDE,
            f"side={side!r}: this path is long only. Spec L §6 describes no "
            "protective-stop shape for a short entry, so one is refused rather "
            "than placed unprotected.",
        )

    evaluation = evaluate(
        session,
        ticker=ticker,
        entry=entry,
        stop=stop,
        risk_fraction=risk_fraction,
        cohort_answer_id=cohort_answer_id,
        expected_hold_sessions=expected_hold_sessions,
        settings=settings,
        now=now,
        resolver=resolver,
        quantity=quantity,
    )

    context = evaluation.context
    assessment = evaluation.assessment
    size = evaluation.size

    row = Proposal(
        proposal_uid=str(uuid.uuid4()),
        ticker=evaluation.ticker,
        side="long",
        entry=evaluation.entry,
        stop=evaluation.stop,
        expected_hold_sessions=expected_hold_sessions,
        risk_fraction=evaluation.risk_fraction,
        risk_fraction_effective=(size.risk_fraction_effective if size else 0.0),
        cohort_answer_id=(assessment.cohort_answer_id or None),
        budget=assessment.budget,
        evidence_lower_bound=assessment.lower_bound,
        evidence_point_estimate=assessment.point_estimate,
        evidence_horizon_sessions=assessment.horizon_sessions,
        evidence_multiplier=assessment.multiplier,
        evidence_gate_mode=assessment.gate_mode,
        evidence_reason=f"{assessment.reason_code}: {assessment.reason}",
        quantity=(size.quantity if size else 0),
        notional=(size.notional if size else 0.0),
        risk_dollars=(size.risk_dollars if size else 0.0),
        equity_at_proposal=context.equity,
        caps_json=json.dumps(size.caps if size else {}, sort_keys=True),
        account_id=context.account_id,
        account_label=context.account_label,
        execution_mode=str(getattr(settings, "execution_mode", "paper") or "paper").lower(),
        portfolio_context_hash=context.context_hash,
        ledger_as_of_utc=context.as_of_utc,
        requester_token_label=requester_token_label or "",
        status=RISK_REJECTED if evaluation.rejection is not None else PROPOSED,
        rejection_code=(evaluation.rejection.code if evaluation.rejection else ""),
        rejection_reason=(evaluation.rejection.reason if evaluation.rejection else ""),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()

    reference = None
    if evaluation.ok:
        reference = approvals_mod.mint(
            proposal_id=row.id,
            proposal_uid=row.proposal_uid,
            owner_id=owner_id,
            now=now,
            settings=settings,
        )
        row.approval_nonce = reference.nonce
        row.approval_signature = reference.signature
        row.approval_expires_at = reference.expires_at
        row.approval_owner_id = reference.owner_id

    card = build_card(row, evaluation, reference)
    row.card_md = card.body_md
    session.flush()
    approvals_mod.send_card(card)
    return row


def build_card(row: Proposal, evaluation: Evaluation, reference) -> approvals_mod.ApprovalCard:
    """The approval card. Spec L §6.6: print the evidence either way.

    Every field §6.6 names is on it — ``risk_fraction``, ``m``, ``LB``, ``PE``,
    the horizon used, and every cap that bound the size — plus the budget the
    proposal draws from and, when it was refused, the reason. A negative lower
    bound is printed as a negative lower bound.
    """
    assessment = evaluation.assessment
    size = evaluation.size
    lines = [
        f"*Proposal {row.id} — {row.ticker} long*",
        "",
        f"entry ${row.entry:,.2f}  ·  stop ${row.stop:,.2f}  ·  risk/share ${row.entry - row.stop:,.2f}",
    ]
    if row.expected_hold_sessions:
        lines.append(f"expected hold: {row.expected_hold_sessions} sessions")
    lines.append("")
    lines.append(f"*budget:* `{row.budget}`")
    lines.append(f"*risk_fraction:* {row.risk_fraction:.5f} requested")

    def _fmt(value):
        return "unknown" if value is None else f"{value:+.4f}"

    lines.append(
        "*evidence:* "
        f"LB={_fmt(assessment.lower_bound)}  PE={_fmt(assessment.point_estimate)}  "
        f"horizon={assessment.horizon_sessions or 'n/a'}  "
        f"m={'n/a' if assessment.multiplier is None else f'{assessment.multiplier:.4f}'}"
    )
    lines.append(f"_{assessment.reason_code}: {assessment.reason}_")
    lines.append(f"_gate mode: {assessment.gate_mode}_")
    lines.append("")

    if size is not None:
        lines.append(
            f"*size:* {size.quantity} whole shares  ·  "
            f"${size.notional:,.2f} notional  ·  "
            f"${size.risk_dollars:,.2f} at risk"
        )
        lines.append(f"*effective risk_fraction:* {size.risk_fraction_effective:.5f}")
        lines.append("*caps:*")
        for name, record in sorted(size.caps.items()):
            marker = "**BOUND**" if record.get("bound") else "ok"
            limit = record.get("limit_notional", record.get("limit"))
            allowed = record.get("shares_allowed")
            detail = f"limit={limit}"
            if allowed is not None:
                detail += f", allows {allowed} shares"
            lines.append(f"  · `{name}` {marker} — {detail}")
    else:
        lines.append("*size:* none — this proposal was refused before sizing.")

    lines.append("")
    lines.append(
        f"_book: ${row.equity_at_proposal:,.2f} equity, "
        f"account {row.account_label or 'unknown'}, "
        f"ledger as of {row.ledger_as_of_utc.isoformat() if row.ledger_as_of_utc else 'never'}_"
    )
    lines.append(f"_context hash {row.portfolio_context_hash[:16]}_")

    if row.status == RISK_REJECTED:
        lines.append("")
        lines.append(f"*REFUSED — {row.rejection_code}*")
        lines.append(row.rejection_reason)
        lines.append("")
        lines.append(
            "_Nothing to approve. Risk is re-evaluated from fresh state at "
            "approval time in any case, so a refusal here is not a reason to "
            "work around this card._"
        )
    else:
        lines.append("")
        lines.append(
            "_Approving places a live entry and then a `gtc` `stop_market` "
            "protective exit. Risk is re-computed from fresh state first; this "
            "approval is single-use, expiring and bound to you._"
        )

    return approvals_mod.ApprovalCard(
        proposal_id=row.id,
        proposal_uid=row.proposal_uid,
        ticker=row.ticker,
        status=row.status,
        body_md="\n".join(lines),
        approve_callback=(reference.approve_callback if reference else None),
        reject_callback=(reference.reject_callback if reference else None),
        expires_at=(reference.expires_at if reference else None),
        detail=proposal_payload(row),
    )


def proposal_payload(row: Proposal) -> dict:
    """The row as an MCP/REST response. Same numbers as the card, by construction."""
    return {
        "proposal_id": row.id,
        "proposal_uid": row.proposal_uid,
        "status": row.status,
        "ticker": row.ticker,
        "side": row.side,
        "entry": row.entry,
        "stop": row.stop,
        "expected_hold_sessions": row.expected_hold_sessions,
        "budget": row.budget,
        "risk_fraction": row.risk_fraction,
        "risk_fraction_effective": row.risk_fraction_effective,
        "quantity": row.quantity,
        "notional": round(float(row.notional or 0.0), 2),
        "risk_dollars": round(float(row.risk_dollars or 0.0), 2),
        "evidence": {
            "cohort_answer_id": row.cohort_answer_id,
            "lower_bound": row.evidence_lower_bound,
            "point_estimate": row.evidence_point_estimate,
            "horizon_sessions": row.evidence_horizon_sessions,
            "m": row.evidence_multiplier,
            "gate_mode": row.evidence_gate_mode,
            "reason": row.evidence_reason,
        },
        "caps": row.caps,
        "account": row.account_label,
        "execution_mode": row.execution_mode,
        "equity_at_proposal": round(float(row.equity_at_proposal or 0.0), 2),
        "portfolio_context_hash": row.portfolio_context_hash,
        "ledger_as_of_utc": row.ledger_as_of_utc.isoformat() if row.ledger_as_of_utc else None,
        "rejection_code": row.rejection_code or None,
        "rejection_reason": row.rejection_reason or None,
        "approval_expires_at": (
            row.approval_expires_at.isoformat() if row.approval_expires_at else None
        ),
        "card_md": row.card_md,
        "placed": False,
        "note": (
            "This created a proposal row and sent an approval card out of band. "
            "It placed nothing: no MCP tool, token scope, or route in this "
            "service can reach a broker (Spec L §6.1). The owner approves on "
            "the out-of-band channel and code executes."
        ),
    }


def settlement_snapshot(context: PortfolioContext, *, on_date: date) -> dict:
    """The settled/unsettled split behind a cash refusal, for the card."""
    view = settlement_view(
        settled_cash=context.settled_cash,
        unsettled_cash=context.unsettled_cash,
        pending_settlements=context.pending_settlements,
        as_of_date=on_date,
    )
    return {
        "settled": round(view.settled, 2),
        "unsettled": round(view.unsettled, 2),
        "pending_by_date": {d.isoformat(): round(v, 2) for d, v in sorted(view.pending_by_date.items())},
    }
