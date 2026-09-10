"""The execution service: what happens *after* a human approves, and only then.

This is the second and third thirds of Spec L §6 — approve, then execute — and
it is the one module in Phase 6 that touches a broker. It lives in
``execution/`` precisely because it must: the workspace's import closure may
never reach it (Spec L §6.1), so an agent, a tool, or a REST route cannot call
what is written here. **It is not an agent.** Its only entry into placement is
:func:`on_approval`, and the only caller of that is the out-of-band approval
channel (``bot/handlers/proposals.py``), which is itself unreachable from any
MCP tool. ``tests/test_execution_lifecycle_isolation.py`` asserts both halves.

The order of operations is Spec L §6 and Spec Q §12, and every step is here
because leaving it out is a specific way to lose money:

1. **Gate.** ``PHASE6_EXECUTION_ENABLED``; and for a *live* placement,
   ``ALLOW_LIVE_TRADING=true`` and ``EXECUTION_MODE=live`` on top. Absence or
   invalidity of a flag never means live (Spec Q §12 invariant 1).
2. **Verify the approval** — signature, single-use, expiry, owner
   (:func:`portfolio.approvals.verify`). A replayed callback is a no-op because
   single-use is enforced by a database column, in the same transaction as the
   status change, not by a variable a restart would forget.
3. **Kill switch and unprotected-position block** (:mod:`portfolio.killswitch`).
   This is the check that actually holds; the one at proposal time was a
   courtesy.
4. **Re-run every risk check from fresh state** (:func:`portfolio.proposals.evaluate`).
   Never the proposal's stored numbers (Spec L §6.2): the book may have moved,
   and a size computed against a book that no longer exists is the failure this
   step exists to prevent. A breach here is terminal for this approval.
5. **Review, then place the entry** — the broker's own ``review_equity_order``
   as the pre-trade snapshot, then ``place_equity_order`` (``market`` or
   ``limit``, ``regular_hours``, whole shares, a ``ref_id`` generated once).
6. **Poll the fill.**
7. **Place the protective stop** — a separate ``stop_market``, ``gtc``,
   ``regular_hours``, whole shares, its own ``ref_id`` — then **read it back**
   from the broker (Spec L §5.1). A placement call returning is a claim; the
   read-back is the evidence.
8. **Protected, or unprotected-and-page.** A fill whose stop cannot be read
   back inside ``PROTECTION_WINDOW_SECONDS`` is marked ``unprotected``, pages
   immediately, and blocks every further entry until a human resolves it.
9. **Journal** the filled decision, with its budget and citation.

Paper mode routes to the Alpaca paper adapter and runs the *same* lifecycle
(Spec Q §11: a paper arm may reach only Alpaca paper). A rehearsal that skipped
the protection verification would be rehearsing a different state machine from
the one live capital runs.

The broker and the page are **injected**, never imported by anything the
workspace can see: ``main.py`` constructs the real ones and the tests pass fakes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from database.models import Proposal
from execution.brokers.base import BrokerOrderRequest
from portfolio import approvals as approvals_mod
from portfolio import killswitch
from portfolio import proposals as proposals_mod
from portfolio.paging import log_pager
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("execution_lifecycle")

#: Page events. Stable strings — an alert rule matches on them.
UNPROTECTED_FILL = "execution_unprotected_fill"
PLACEMENT_UNKNOWN = "execution_placement_unknown"
STOP_REPLACED = "execution_stop_replaced"
STOP_REPLACE_FAILED = "execution_stop_replace_failed"


class ExecutionRefused(Exception):
    """A refusal the approval channel can relay to the owner verbatim."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class ExecutionResult:
    """What one approval-to-placement attempt did. Returned, and logged."""

    proposal_id: int
    status: str
    message: str
    entry_order_id: str = ""
    stop_order_id: str = ""
    paged: bool = False

    def as_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "message": self.message,
            "entry_order_id": self.entry_order_id,
            "stop_order_id": self.stop_order_id,
            "paged": self.paged,
        }


class ExecutionService:
    """Drives a proposal from ``approved`` to ``protected`` (or ``unprotected``).

    Everything it needs from the outside is passed in: a ``session_factory``
    (``callable() -> context manager yielding a session``), the ``broker`` for
    the proposal's execution mode, ``settings``, and a ``pager``. None of these
    is imported here from a module the workspace can reach; that is what keeps
    the import-graph assertion true with this file in the tree.
    """

    def __init__(self, *, session_factory, broker, settings, pager=None, resolver=None):
        self.session_factory = session_factory
        self.broker = broker
        self.settings = settings
        self.pager = pager or log_pager
        self.resolver = resolver

    # -- gating -------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not bool(getattr(self.settings, "phase6_execution_enabled", False)):
            raise ExecutionRefused(
                "phase6_disabled",
                "PHASE6_EXECUTION_ENABLED is false. The proposal tool is not "
                "registered and approval callbacks are refused; nothing "
                "executes until it is turned on.",
            )

    def _require_live_gates(self, proposal: Proposal) -> None:
        """The extra gates a *live* placement needs, on top of everything else.

        Spec Q §12 invariant 1: absence or invalidity of a flag must never mean
        live. So this is an explicit conjunction of positive checks, and a
        proposal whose recorded ``execution_mode`` is not ``live`` never reaches
        a live broker even if the flags happen to be on.
        """
        if proposal.execution_mode != "live":
            return
        if not bool(getattr(self.settings, "allow_live_trading", False)):
            raise ExecutionRefused(
                "live_trading_disabled",
                "ALLOW_LIVE_TRADING is not true, so a live placement is "
                "refused. This is required on top of PHASE6_EXECUTION_ENABLED "
                "for anything that reaches the live broker.",
            )
        if str(getattr(self.settings, "execution_mode", "paper")).lower() != "live":
            raise ExecutionRefused(
                "execution_mode_not_live",
                "EXECUTION_MODE is not 'live'. A proposal recorded as live may "
                "only be placed when the service is in live mode too.",
            )

    # -- the entry point ----------------------------------------------------

    def on_approval(
        self,
        *,
        proposal_id: int,
        presented_signature: str,
        owner_id: str,
        now=None,
    ) -> ExecutionResult:
        """Verify an approval and, if everything holds, place and protect.

        This is the *only* way into placement, and the approval channel is its
        only caller. It raises :class:`ExecutionRefused` for a refusal the owner
        should see, and returns an :class:`ExecutionResult` once an order exists.
        """
        now = now or utcnow_naive()
        self._require_enabled()

        with self.session_factory() as session:
            proposal = session.get(Proposal, int(proposal_id))
            if proposal is None:
                raise ExecutionRefused("unknown_proposal", f"no proposal {proposal_id}.")

            if proposal.status == "risk_rejected":
                raise ExecutionRefused(
                    "not_approvable",
                    f"proposal {proposal_id} was created risk_rejected "
                    f"({proposal.rejection_code}) and carries no approval "
                    "reference. There is nothing to approve.",
                )
            if proposal.status != "proposed":
                raise ExecutionRefused(
                    "not_proposed",
                    f"proposal {proposal_id} is {proposal.status!r}, not "
                    "'proposed'. An approval acts once, on a proposal that is "
                    "still awaiting one.",
                )

            # The four-part approval check (Spec L §6.3). Raises on any failure.
            approvals_mod.verify(
                proposal,
                presented_signature=presented_signature,
                owner_id=owner_id,
                now=now,
                settings=self.settings,
            )

            # Kill switch and unprotected-position block — the real one.
            block = killswitch.entry_block(session)
            if block is not None:
                code, reason = block
                raise ExecutionRefused(code, reason)

            self._require_live_gates(proposal)

            # Re-run every risk check from FRESH state (Spec L §6.2). Never the
            # stored numbers. A breach here is terminal for this approval.
            evaluation = proposals_mod.evaluate(
                session,
                ticker=proposal.ticker,
                entry=proposal.entry,
                stop=proposal.stop,
                risk_fraction=proposal.risk_fraction,
                cohort_answer_id=proposal.cohort_answer_id or "",
                expected_hold_sessions=proposal.expected_hold_sessions,
                settings=self.settings,
                now=now,
                resolver=self.resolver,
            )
            if not evaluation.ok:
                rejection = evaluation.rejection
                proposal.status = "risk_rejected"
                proposal.rejection_code = rejection.code
                proposal.rejection_reason = (
                    "re-evaluated at approval and refused: " + rejection.reason
                )
                proposal.approval_consumed_at = now
                proposal.updated_at = now
                session.commit()
                raise ExecutionRefused(
                    "risk_recomputed_rejected",
                    f"the book changed since this was proposed: {rejection.reason} "
                    "Risk is re-evaluated from fresh state at approval, never "
                    "reused from the proposal (Spec L §6.2). Propose again "
                    "against the current book.",
                )

            fresh_size = evaluation.size

            # Consume the approval in the same transaction as the transition to
            # `approved`, so a replay after a restart finds it already used.
            proposal.approval_consumed_at = now
            proposal.approved_at = now
            proposal.status = "approved"
            proposal.quantity = fresh_size.quantity
            proposal.notional = fresh_size.notional
            proposal.risk_dollars = fresh_size.risk_dollars
            proposal.entry_ref_id = proposal.entry_ref_id or f"p6-entry-{proposal.proposal_uid}"
            proposal.updated_at = now
            session.commit()

            return self._place_and_protect(session, proposal, fresh_size, now)

    # -- placement ----------------------------------------------------------

    def _place_and_protect(self, session, proposal: Proposal, size, now) -> ExecutionResult:
        entry_type = self._entry_order_type()
        entry_request = BrokerOrderRequest(
            symbol=proposal.ticker,
            side="buy",
            order_type=entry_type,
            quantity=int(proposal.quantity),
            limit_price=(proposal.entry if entry_type == "limit" else None),
            time_in_force="gfd",
            market_hours="regular_hours",
            direction="long",
            stop_loss=proposal.stop,
            requested_notional=proposal.notional,
            client_context={"ref_id": proposal.entry_ref_id, "proposal_id": proposal.id},
        )

        review = self.broker.review_order(entry_request)
        if not review.approved:
            proposal.status = "failed"
            proposal.rejection_code = "broker_review_rejected"
            proposal.rejection_reason = " | ".join(review.errors) or "broker review rejected the entry."
            proposal.updated_at = utcnow_naive()
            session.commit()
            raise ExecutionRefused("broker_review_rejected", proposal.rejection_reason)

        result = self.broker.place_order(review)
        if not result.success:
            # An outright placement failure with no order is terminal and safe;
            # an *ambiguous* one is not, and is handled below.
            reconciled = self._reconcile_unknown(proposal, result)
            if reconciled is not None:
                return reconciled
            proposal.status = "failed"
            proposal.rejection_code = "placement_failed"
            proposal.rejection_reason = result.error or "the broker did not accept the entry."
            proposal.updated_at = utcnow_naive()
            session.commit()
            raise ExecutionRefused("placement_failed", proposal.rejection_reason)

        proposal.entry_broker_order_id = result.order_id
        proposal.status = "submitted"
        proposal.submitted_at = utcnow_naive()
        proposal.updated_at = proposal.submitted_at
        session.commit()

        fill = self._poll_fill(result)
        if not fill.get("filled"):
            # Submitted but not yet filled inside the poll window. Not a failure
            # and not terminal: the daily job picks it up. It is left `submitted`
            # so nothing treats it as an open, unprotected position.
            return ExecutionResult(
                proposal_id=proposal.id,
                status="submitted",
                message=(
                    "entry submitted; not filled within the poll window. The "
                    "daily job will place the stop once it fills."
                ),
                entry_order_id=result.order_id,
            )

        proposal.status = "filled"
        proposal.filled_at = utcnow_naive()
        proposal.filled_quantity = fill.get("filled_qty") or proposal.quantity
        proposal.average_fill_price = fill.get("filled_avg_price")
        proposal.updated_at = proposal.filled_at
        session.commit()

        return self._protect(session, proposal, now)

    def _protect(self, session, proposal: Proposal, now) -> ExecutionResult:
        """Place the stop, read it back, and mark protected or page.

        The window is the whole point (Spec L §5.1): between the entry filling
        and the stop being *read back*, the position is uninsured. A stop that
        the placement call "accepted" but that never appears in
        ``read_open_orders`` is exactly the failure this catches — so the state
        that means safe is set only after the read-back, never after the place.
        """
        proposal.stop_ref_id = proposal.stop_ref_id or f"p6-stop-{proposal.proposal_uid}"
        qty = int(proposal.filled_quantity or proposal.quantity)

        place = self.broker.place_stop(
            symbol=proposal.ticker,
            quantity=qty,
            stop_price=proposal.stop,
            ref_id=proposal.stop_ref_id,
            side="sell",
        )
        if place.success:
            proposal.stop_broker_order_id = place.order_id or place.stop_order_id

        deadline = time.monotonic() + self._protection_window()
        interval = float(getattr(self.settings, "protection_poll_interval_seconds", 2.0) or 2.0)
        readable = self._stop_is_readable(proposal)
        while not readable and time.monotonic() < deadline:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            readable = self._stop_is_readable(proposal)

        proposal.protection_deadline_at = now
        if readable:
            proposal.status = "protected"
            proposal.protection_verified_at = utcnow_naive()
            proposal.updated_at = proposal.protection_verified_at
            session.commit()
            self._journal_fill(session, proposal)
            session.commit()
            return ExecutionResult(
                proposal_id=proposal.id,
                status="protected",
                message="entry filled and the gtc stop_market was read back from the broker.",
                entry_order_id=proposal.entry_broker_order_id or "",
                stop_order_id=proposal.stop_broker_order_id or "",
            )

        # Unprotected: page immediately and block every further entry. The
        # position is real and uninsured; that is the loudest state this system
        # has, on purpose.
        proposal.status = "unprotected"
        proposal.updated_at = utcnow_naive()
        session.commit()
        self.pager(
            UNPROTECTED_FILL,
            {
                "proposal_id": proposal.id,
                "ticker": proposal.ticker,
                "quantity": qty,
                "stop_price": proposal.stop,
                "stop_ref_id": proposal.stop_ref_id,
                "stop_place_error": place.error if not place.success else "",
                "recovery": (
                    "The entry filled but its gtc stop_market could not be read "
                    "back from get_equity_orders within the protection window. "
                    "The position is UNPROTECTED and all further entries are "
                    "blocked. Place a protective stop by hand in the Robinhood "
                    "app, confirm it in get_equity_orders, then clear this "
                    "proposal. The daily job will also retry the stop."
                ),
            },
        )
        return ExecutionResult(
            proposal_id=proposal.id,
            status="unprotected",
            message=(
                "entry filled but the protective stop could not be verified at "
                "the broker within the window. Paged; further entries blocked."
            ),
            entry_order_id=proposal.entry_broker_order_id or "",
            paged=True,
        )

    # -- the daily missing-stop job (Spec L §5.1, test_missing_stop_replaced_daily)

    def replace_missing_stops(self, *, now=None) -> list[dict]:
        """Re-read open orders and re-place any stop that has disappeared.

        Robinhood's GTC horizon is unstated, so a twenty-session hold can outlive
        its stop unless something re-reads and re-places. That something is this
        job. It runs over every ``protected`` position: a stop missing from
        ``read_open_orders`` is re-placed with the same ``ref_id`` (so a stop
        that is merely slow to surface is not duplicated), and a re-placement
        that cannot be verified drops the position to ``unprotected`` and pages,
        exactly as the original placement would.
        """
        now = now or utcnow_naive()
        actions: list[dict] = []
        with self.session_factory() as session:
            protected = (
                session.query(Proposal)
                .filter(Proposal.status == "protected")
                .order_by(Proposal.id.asc())
                .all()
            )
            for proposal in protected:
                if self._stop_is_readable(proposal):
                    continue
                qty = int(proposal.filled_quantity or proposal.quantity)
                place = self.broker.place_stop(
                    symbol=proposal.ticker,
                    quantity=qty,
                    stop_price=proposal.stop,
                    ref_id=proposal.stop_ref_id or f"p6-stop-{proposal.proposal_uid}",
                    side="sell",
                )
                proposal.stop_replaced_count = int(proposal.stop_replaced_count or 0) + 1
                if place.success and self._stop_is_readable(proposal):
                    proposal.stop_broker_order_id = place.order_id or place.stop_order_id
                    proposal.updated_at = now
                    session.commit()
                    self.pager(
                        STOP_REPLACED,
                        {
                            "proposal_id": proposal.id,
                            "ticker": proposal.ticker,
                            "recovery": "A missing protective stop was re-placed and verified.",
                        },
                    )
                    actions.append({"proposal_id": proposal.id, "action": "replaced"})
                else:
                    proposal.status = "unprotected"
                    proposal.updated_at = now
                    session.commit()
                    self.pager(
                        STOP_REPLACE_FAILED,
                        {
                            "proposal_id": proposal.id,
                            "ticker": proposal.ticker,
                            "error": place.error,
                            "recovery": (
                                "A missing protective stop could not be "
                                "re-placed or verified; the position is now "
                                "unprotected and entries are blocked. Place a "
                                "stop by hand and confirm it in get_equity_orders."
                            ),
                        },
                    )
                    actions.append({"proposal_id": proposal.id, "action": "unprotected"})
        return actions

    # -- helpers ------------------------------------------------------------

    def _entry_order_type(self) -> str:
        raw = str(getattr(self.settings, "robinhood_order_type", "limit") or "limit").lower()
        return "market" if raw == "market" else "limit"

    def _protection_window(self) -> float:
        try:
            return float(getattr(self.settings, "protection_window_seconds", 120) or 120)
        except (TypeError, ValueError):
            return 120.0

    def _poll_fill(self, result) -> dict:
        """Poll the entry to a fill, within the protection window.

        A ``place_order`` that already reported ``filled`` short-circuits; a
        broker that returns ``submitted`` is polled with ``get_order_status``.
        """
        status = (result.status or "").lower()
        if status == "filled" or result.filled_qty:
            return {
                "filled": True,
                "filled_qty": result.filled_qty,
                "filled_avg_price": result.filled_avg_price,
            }
        order_id = result.order_id
        if not order_id or not hasattr(self.broker, "get_order_status"):
            return {"filled": False}
        deadline = time.monotonic() + self._protection_window()
        interval = float(getattr(self.settings, "protection_poll_interval_seconds", 2.0) or 2.0)
        while time.monotonic() < deadline:
            info = self.broker.get_order_status(order_id) or {}
            if (info.get("status") or "").lower() == "filled" or info.get("filled_qty"):
                return {
                    "filled": True,
                    "filled_qty": info.get("filled_qty"),
                    "filled_avg_price": info.get("filled_avg_price"),
                }
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        return {"filled": False}

    def _stop_is_readable(self, proposal: Proposal) -> bool:
        """Whether this proposal's protective stop is currently at the broker.

        Matches on ``ref_id`` — ours, generated once — first, and falls back to
        a ``stop_market`` for the same symbol at the same stop price for
        adapters (Alpaca paper) that do not echo an idempotency key. Either way
        it is an open, non-terminal order that counts.
        """
        try:
            open_orders = self.broker.read_open_orders(symbol=proposal.ticker)
        except Exception as exc:  # pragma: no cover - adapter-specific
            log.error("read_open_orders_failed", proposal_id=proposal.id, error=str(exc))
            return False
        for order in open_orders:
            if not order.is_open:
                continue
            if (order.order_type or "").lower() != "stop_market":
                continue
            if proposal.stop_ref_id and order.ref_id == proposal.stop_ref_id:
                return True
            if (
                not order.ref_id
                and order.symbol == proposal.ticker.upper()
                and order.stop_price is not None
                and abs(float(order.stop_price) - float(proposal.stop)) < 1e-6
            ):
                return True
        return False

    def _reconcile_unknown(self, proposal: Proposal, result) -> ExecutionResult | None:
        """Handle an ambiguous placement (Spec Q §12 invariant 8).

        An unknown broker outcome is not terminal and may not release its
        reservation: the order might exist. If the ref_id turns up at the
        broker, adopt it; otherwise mark ``reconciliation_required``, which
        blocks new entries until a human resolves it, and page.
        """
        if not getattr(result, "raw", None) or "unknown" not in (result.error or "").lower():
            return None
        found = None
        if hasattr(self.broker, "find_order_by_ref_id"):
            found = self.broker.find_order_by_ref_id(proposal.entry_ref_id)
        if found:
            return None  # the caller's normal path will pick it up on retry
        proposal.status = "reconciliation_required"
        proposal.rejection_code = "placement_unknown"
        proposal.rejection_reason = result.error or "the broker's placement response was unknown."
        proposal.updated_at = utcnow_naive()
        self.pager(
            PLACEMENT_UNKNOWN,
            {
                "proposal_id": proposal.id,
                "ticker": proposal.ticker,
                "entry_ref_id": proposal.entry_ref_id,
                "recovery": (
                    "A placement response was unknown and no order with this "
                    "ref_id could be found. Entries are blocked until this is "
                    "reconciled. Check the Robinhood app for an order carrying "
                    f"ref_id {proposal.entry_ref_id!r}."
                ),
            },
        )
        return ExecutionResult(
            proposal_id=proposal.id,
            status="reconciliation_required",
            message="placement outcome unknown; blocked pending reconciliation.",
            paged=True,
        )

    def _journal_fill(self, session, proposal: Proposal) -> None:
        """Append the filled decision to the journal (Spec L §6, step 9 / Spec M).

        Uses the Phase 2 *service function*, not the MCP tool: the journal write
        that follows an execution is a code action, not an agent turn, and it
        carries the budget and — for an evidenced trade — the citation the
        proposal was built on.
        """
        try:
            from research_workspace.store import journal_append

            cohort_answer = None
            if proposal.budget == "evidenced" and proposal.cohort_answer_id:
                if self.resolver is not None:
                    cohort_answer = self.resolver(proposal.cohort_answer_id)
                else:
                    from research_workspace import citations

                    cohort_answer = citations.resolve(proposal.cohort_answer_id)

            journal_append(
                session,
                decision="opened",
                tickers=[proposal.ticker],
                cohort_answer=cohort_answer,
                budget=proposal.budget,
                sizing_rationale=(
                    f"proposal {proposal.id}: {int(proposal.filled_quantity or proposal.quantity)} "
                    f"shares @ ~{proposal.average_fill_price or proposal.entry}, "
                    f"stop {proposal.stop}, risk_fraction_effective "
                    f"{proposal.risk_fraction_effective:.5f}, {proposal.evidence_reason}"
                ),
                expected_holding_days=proposal.expected_hold_sessions,
                note_md=(
                    f"Executed via Phase 6 lifecycle in {proposal.execution_mode} mode "
                    f"and protected with a gtc stop_market at {proposal.stop}."
                ),
                author="execution_service",
            )
        except Exception as exc:  # pragma: no cover - journal must not unwind a good fill
            log.error("journal_append_failed", proposal_id=proposal.id, error=str(exc))
