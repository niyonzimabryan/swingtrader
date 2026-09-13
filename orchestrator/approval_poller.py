"""The runtime half of the owner control surface: the only thing that acts.

Spec K §10 / Spec L §10, owner ruling 2026-09-13. An MCP owner tool records a
decision into ``owner_actions``; this poller, running in the bot container, is
what turns one into a placement, a cancellation or a tier change. The split is
the whole reason the ruling was safe to make, and it is a property of the import
graph rather than of anyone's intentions: ``workspace/`` cannot import
``execution/``, ``bot/`` or ``orchestrator/``
(``tests/test_no_execute_scope.py``), and this module is in ``orchestrator/``.

**It adds no risk logic of its own, and no second path to placement.** Every
approval goes through ``execution/lifecycle.py::on_approval`` — or, for a
Strategy Lab execution, ``execution/strategy_lifecycle.py::on_approval`` —
called with exactly the arguments ``bot/handlers/proposals.py`` calls it with.
Those services verify the signed, single-use, expiring, owner-bound reference,
check the kill switch, re-run every risk check from fresh state, and only then
place and protect. A second copy of any of that here would be a second copy to
keep in sync, which is how the copies diverge.

**Routing is read off the row, never off the input**, exactly as the Telegram
handler does it: a proposal carrying an ``execution_id`` belongs to a Strategy
Lab arm, and the arm's immutable mode — not the global ``EXECUTION_MODE`` —
decides the venue (Spec Q §12 invariant 11). The ``execution_id`` was written by
``StrategyExecutionService.propose`` inside its own transaction, so nothing a
tool call carries can make a lab execution look like a plain proposal or the
reverse.

**Double-placement.** Three independent things stop it, and they fail
differently:

1. the claim (``portfolio.owner_actions.claim``) is a conditional ``UPDATE``
   whose row count decides the winner, so two pollers cannot both take one
   decision;
2. ``proposals.approval_consumed_at`` is consumed inside ``on_approval``'s own
   transaction, so a claim that somehow double-fired — or a poller racing the
   Telegram button — still places once;
3. ``on_approval`` refuses anything not still ``proposed``.

**A claim is never reaped, and that is deliberate.** If this process dies
between claiming a decision and finishing it, the row stays claimed and no
poller picks it up again — the decision is stuck, not retried. A stale-claim
reaper would re-run an action whose outcome is *unknown*, which is precisely the
case the rest of the system treats as ``reconciliation_required`` rather than
retrying (Spec L §5.1): the entry may already be at the broker. So the failure
direction is stuck, which is visible, over double-placed, which is not. A
claimed row with no outcome shows up in ``proposals_pending``'s
``recorded_decisions`` with its ``claimed_at`` set and nothing else, and the
recovery is the owner's: check the broker, then reject the proposal or let the
reconciliation pass resolve it.

**Both flags, and both default off.** ``PHASE6_EXECUTION_ENABLED`` *and*
``OWNER_ACTION_POLLER_ENABLED``. Turning Phase 6 on must not silently start a
second path to placement in a deployment that never asked for one, and Phase 6
is already on in production — so the second flag is what this PR actually gates
on, and with it unset production behaves exactly as it did.
"""

from __future__ import annotations

import asyncio

from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("approval_poller")

#: Bounds on the poll interval. Below the floor this is a busy loop against the
#: database; above the ceiling an owner watches a recorded approval do nothing
#: for long enough to tap the Telegram button as well, which is fine but makes
#: the tool feel broken.
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 120
DEFAULT_INTERVAL_SECONDS = 20

#: How long one decision may take before the poller stops waiting on it. An
#: approval is the entry, the fill poll, the stop and its read-back; the
#: Telegram handler allows the same 90 seconds.
ACTION_TIMEOUT_S = 120.0


def interval_seconds(settings) -> int:
    try:
        value = int(
            getattr(settings, "owner_action_poll_seconds", DEFAULT_INTERVAL_SECONDS)
            or DEFAULT_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, min(value, MAX_INTERVAL_SECONDS))


def enabled(settings) -> bool:
    return bool(
        getattr(settings, "phase6_execution_enabled", False)
        and getattr(settings, "owner_action_poller_enabled", False)
    )


class ApprovalPoller:
    """Picks up recorded owner decisions and hands each to the right service.

    Every collaborator is injected. ``main.py`` wires the real ones; the tests
    wire fakes and drive :meth:`run_once` directly, so nothing about the
    behaviour under test depends on a loop or a clock.

    ``notify`` is ``callable(event: str, detail: dict) -> None`` — the same shape
    ``portfolio.paging`` and the Phase 6 pager already use, and for the same
    reason: this module must not import ``bot``, and a notifier that is a
    parameter can be the Telegram manager today and an email sender tomorrow
    without a change here. ``main.py`` passes the pager it already built.
    """

    def __init__(
        self,
        *,
        session_factory,
        settings,
        execution_service=None,
        strategy_execution_service=None,
        memo_executor=None,
        promotion_wiring=None,
        adapters=None,
        notify=None,
        instance_id: str = "",
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.execution_service = execution_service
        self.strategy_execution_service = strategy_execution_service
        #: ``callable(memo_id) -> dict``, blocking. The scan path's
        #: ``OrderManager.execute_approved_trade`` is a coroutine, so ``main.py``
        #: wraps it; the poller stays synchronous inside its worker thread.
        self.memo_executor = memo_executor
        #: ``orchestrator.strategy_lab_promotion``, injected so the tests can
        #: substitute a recorder and this module holds no promotion logic.
        self.promotion_wiring = promotion_wiring
        self.adapters = adapters or {}
        self.notify = notify
        self.instance_id = instance_id or f"poller-{id(self):x}"
        self._task = None
        self._stop = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        log.info(
            "approval_poller_started",
            interval_seconds=interval_seconds(self.settings),
            instance_id=self.instance_id,
        )

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        log.info("approval_poller_stopped", instance_id=self.instance_id)

    async def _loop(self) -> None:
        delay = interval_seconds(self.settings)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:  # pragma: no cover - defensive
                # A pass that raises must not kill the loop: the next recorded
                # approval would then sit there with nothing to act on it and no
                # sign of why.
                log.error("approval_poller_pass_failed", error=str(exc)[:300])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    # -- one pass -----------------------------------------------------------

    def run_once(self) -> list[dict]:
        """Expire stale confirmations, then work every claimable decision.

        Synchronous and blocking, and driven directly by the tests. Returns one
        result dict per decision acted on, oldest first.
        """
        from portfolio import owner_actions

        now = utcnow_naive()
        with self.session_factory() as session:
            owner_actions.expire_stale(session, now=now)
            session.commit()
            ids = owner_actions.claimable(session)
            session.commit()

        results = []
        for action_id in ids:
            outcome = self._work_one(action_id)
            if outcome is not None:
                results.append(outcome)
        return results

    def _work_one(self, action_id: int) -> dict | None:
        from database.models import OwnerAction
        from portfolio import owner_actions

        now = utcnow_naive()
        with self.session_factory() as session:
            if not owner_actions.claim(
                session, action_id, claimed_by=self.instance_id, now=now
            ):
                # Somebody else has it. Not an error and not worth a log line at
                # info: two pollers scanning the same queue is the expected
                # shape of a rolling deploy.
                session.commit()
                return None
            session.commit()

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            if row is None:  # pragma: no cover - the claim just read it
                return None
            kind, uid = row.kind, row.action_uid

        handlers = {
            "approve_order": self._approve_order,
            "reject_order": self._reject_order,
            "approve_memo": self._approve_memo,
            "promote_arm": self._tier_change,
            "demote_arm": self._tier_change,
        }
        handler = handlers.get(kind)
        if handler is None:
            # `reject_memo` and a plain `reject_order` are applied by the tool
            # itself and are recorded `executed`, so they are never claimable.
            # Anything else here is a kind this poller does not know, which is a
            # deployment running an older runtime against a newer workspace.
            self._finish(
                action_id,
                status="executed",
                outcome_code="unsupported_kind",
                outcome_detail=(
                    f"this runtime does not handle {kind!r}. Nothing was done. "
                    "The bot and the workspace are built from one repository; "
                    "if they disagree, one of them has not redeployed."
                ),
            )
            return {"action_uid": uid, "kind": kind, "outcome": "unsupported_kind"}

        try:
            return handler(action_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "owner_action_failed", action_uid=uid, kind=kind, error=str(exc)[:300]
            )
            self._finish(
                action_id,
                status="executed",
                outcome_code="unhandled_error",
                outcome_detail=str(exc)[:1000],
            )
            self._page(
                "owner_action_failed",
                {"action_uid": uid, "kind": kind, "recovery": str(exc)[:300]},
            )
            return {"action_uid": uid, "kind": kind, "outcome": "unhandled_error"}

    # -- handlers -----------------------------------------------------------

    def _proposal_of(self, action_id: int):
        """``(proposal_id, proposal_uid, execution_id, signature, owner_id)``.

        Every routing key is read from the **proposal row**, never from the
        action: the action says which proposal the owner decided about, and the
        proposal says what it is. That is the same rule
        ``bot/handlers/proposals.py::_lab_execution_id`` follows, for the same
        reason — a crafted input must not be able to change which service places
        an order.
        """
        from database.models import OwnerAction, Proposal

        with self.session_factory() as session:
            action = session.get(OwnerAction, int(action_id))
            row = (
                session.query(Proposal)
                .filter(Proposal.proposal_uid == action.subject_ref)
                .one_or_none()
            )
            if row is None:
                return None
            return (
                row.id,
                row.proposal_uid,
                row.execution_id or "",
                row.approval_signature or "",
                row.approval_owner_id or "",
                row.status,
            )

    def _approve_order(self, action_id: int) -> dict:
        from execution.lifecycle import ExecutionRefused
        from execution.strategy_lifecycle import ArmExecutionRefused
        from portfolio.approvals import ApprovalRefused

        found = self._proposal_of(action_id)
        if found is None:
            return self._finish(
                action_id,
                status="executed",
                outcome_code="unknown_proposal",
                outcome_detail="the proposal this decision names no longer exists.",
            )
        proposal_id, uid, execution_id, signature, owner_id, _status = found

        service = (
            self.strategy_execution_service if execution_id else self.execution_service
        )
        if service is None:
            # Not the owner's fault and not terminal: the decision stands, the
            # deployment is missing a service. Release the claim so a restart
            # that wires one picks it up, and say so.
            self._release(action_id)
            self._page(
                "owner_action_service_missing",
                {
                    "proposal_id": proposal_id,
                    "recovery": (
                        "the Strategy Lab execution service is not wired in this "
                        "deployment, so this arm's execution cannot be approved."
                        if execution_id
                        else "the execution service is not wired in this "
                        "deployment; nothing was placed and the proposal is "
                        "unchanged."
                    ),
                },
            )
            return {
                "action_uid": uid,
                "kind": "approve_order",
                "outcome": "service_not_wired",
            }

        try:
            if execution_id:
                result = service.on_approval(
                    execution_id=execution_id,
                    presented_signature=signature,
                    owner_id=owner_id,
                )
            else:
                result = service.on_approval(
                    proposal_id=proposal_id,
                    presented_signature=signature,
                    owner_id=owner_id,
                )
        except (ExecutionRefused, ArmExecutionRefused, ApprovalRefused) as exc:
            # A refusal from the guards is an outcome, not a retry. Re-running an
            # approval the risk checks just rejected would be the system arguing
            # with itself.
            self._page(
                "owner_action_refused",
                {
                    "proposal_id": proposal_id,
                    "execution_id": execution_id,
                    "recovery": f"not placed: {exc.code}: {exc.message}",
                },
            )
            return self._finish(
                action_id,
                status="executed",
                outcome_code=exc.code,
                outcome_detail=exc.message,
            )

        detail = f"{getattr(result, 'status', '?')}: {getattr(result, 'message', '')}"
        for label in ("entry_order_id", "stop_order_id"):
            value = getattr(result, label, None)
            if value:
                detail += f"\n{label} {value}"
        self._page(
            "owner_action_placed",
            {
                "proposal_id": proposal_id,
                "execution_id": execution_id,
                "recovery": detail,
            },
        )
        return self._finish(
            action_id,
            status="executed",
            outcome_code=str(getattr(result, "status", "") or "placed"),
            outcome_detail=detail,
        )

    def _reject_order(self, action_id: int) -> dict:
        """Only a Strategy Lab execution reaches here.

        A plain rejection is a ledger write and the tool applies it itself; a lab
        execution has a second row in ``strategy_trades`` whose ``proposed``
        state would otherwise hold its decision's single open-execution slot
        forever, and cancelling it needs ``execution/``.
        """
        found = self._proposal_of(action_id)
        if found is None:
            return self._finish(
                action_id,
                status="executed",
                outcome_code="unknown_proposal",
                outcome_detail="the proposal this decision names no longer exists.",
            )
        proposal_id, uid, execution_id, _sig, _owner, _status = found
        if not execution_id:  # pragma: no cover - the tool applies these itself
            return self._finish(
                action_id,
                status="executed",
                outcome_code="already_applied",
                outcome_detail="a plain rejection is applied when it is recorded.",
            )
        service = self.strategy_execution_service
        if service is None:
            self._release(action_id)
            self._page(
                "owner_action_service_missing",
                {
                    "proposal_id": proposal_id,
                    "execution_id": execution_id,
                    "recovery": (
                        "the Strategy Lab execution service is not wired, so "
                        "this execution cannot be cancelled. Nothing was placed."
                    ),
                },
            )
            return {
                "action_uid": uid,
                "kind": "reject_order",
                "outcome": "service_not_wired",
            }
        try:
            status = service.cancel(
                execution_id=execution_id, by="owner", reason="owner_rejected"
            )
        except Exception as exc:
            return self._finish(
                action_id,
                status="executed",
                outcome_code="cancel_failed",
                outcome_detail=str(exc)[:1000],
            )
        self._page(
            "owner_action_cancelled",
            {
                "proposal_id": proposal_id,
                "execution_id": execution_id,
                "recovery": f"rejected ({status}). Nothing was placed.",
            },
        )
        return self._finish(
            action_id,
            status="executed",
            outcome_code="cancelled",
            outcome_detail=f"execution {execution_id} cancelled: {status}.",
        )

    def _approve_memo(self, action_id: int) -> dict:
        """The older scan path (``execute_approved_trade``).

        The memo carries no signed reference of its own, so the controls here are
        the claim above, the memo's ``pending`` status, and the risk manager
        ``execute_approved_trade`` runs before it places. ``memos`` is moved to
        ``approved`` in the same place the Telegram handler moves it — before the
        call — so a crash between the two leaves a memo that cannot be approved
        again rather than one that can be approved twice.
        """
        from database.models import Memo, OwnerAction

        if self.memo_executor is None:
            self._release(action_id)
            self._page(
                "owner_action_service_missing",
                {
                    "recovery": (
                        "no memo executor is wired in this deployment, so an "
                        "approved memo cannot be placed. Nothing was placed."
                    )
                },
            )
            return {"kind": "approve_memo", "outcome": "service_not_wired"}

        with self.session_factory() as session:
            action = session.get(OwnerAction, int(action_id))
            memo_id = int(action.subject_ref)
            memo = session.get(Memo, memo_id)
            if memo is None:
                session.commit()
                return self._finish(
                    action_id,
                    status="executed",
                    outcome_code="unknown_memo",
                    outcome_detail=f"memo {memo_id} no longer exists.",
                )
            if (memo.status or "") != "pending":
                session.commit()
                return self._finish(
                    action_id,
                    status="executed",
                    outcome_code="not_pending",
                    outcome_detail=(
                        f"memo {memo_id} is {memo.status!r}; a decision acts "
                        "once, on a memo still awaiting one."
                    ),
                )
            memo.status = "approved"
            memo.responded_at = utcnow_naive()
            session.commit()

        try:
            result = self.memo_executor(memo_id)
        except Exception as exc:
            return self._finish(
                action_id,
                status="executed",
                outcome_code="memo_execution_failed",
                outcome_detail=str(exc)[:1000],
            )
        detail = str(result)[:1000]
        self._page(
            "owner_action_memo_executed",
            {"recovery": f"memo {memo_id}: {detail}"},
        )
        return self._finish(
            action_id,
            status="executed",
            outcome_code=("placed" if (result or {}).get("success") else "not_placed"),
            outcome_detail=detail,
        )

    def _tier_change(self, action_id: int) -> dict:
        """Prepare a tier change, or confirm one the owner already said yes to.

        Two passes, because the workspace cannot compute the plan: the gates live
        here and reach ``execution/`` for Phase 6's live checks. The first pass
        builds the plan, renders the card and mints the signed, expiring,
        owner-bound, single-use confirmation; the second, after the owner has
        confirmed through the tool, runs it.
        """
        from database.models import OwnerAction

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            status = row.status
        if status == "requested":
            return self._prepare_tier_change(action_id)
        return self._confirm_tier_change(action_id)

    def _prepare_tier_change(self, action_id: int) -> dict:
        from database.models import OwnerAction
        from portfolio import approvals, owner_actions
        from strategy_lab.domain import ExecutionMode

        wiring = self.promotion_wiring
        if wiring is None:
            wiring = self._default_promotion_wiring()

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            payload = dict(row.payload)
            source_arm_id = int(payload.get("source_arm_id") or 0)
            to_mode = ExecutionMode(str(payload.get("to_mode")))
            reason = payload.get("reason") or f"owner {row.kind} over MCP"
            owner = str(
                getattr(self.settings, "strategy_lab_experiment_owner", "") or "bryan"
            )
            try:
                request = wiring.build_request(
                    session,
                    self.settings,
                    source_arm_id=source_arm_id,
                    to_mode=to_mode,
                    owner=owner,
                    reason=reason,
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                return self._finish(
                    action_id,
                    status="refused",
                    outcome_code="cannot_plan",
                    outcome_detail=str(exc)[:1000],
                )

        plan = wiring.plan(self.settings, request, adapters=self.adapters)

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            payload = dict(row.payload)
            payload.update(
                {
                    "target_arm_id": request.target_arm_id,
                    "evidence_metric_snapshot_id": request.evidence_metric_snapshot_id,
                    "requested_risk_budget": float(request.requested_risk_budget),
                    "to_mode": request.requested_mode.value,
                    "confirmable": bool(plan.confirmable),
                    # Both lists, together. `plan.confirmable` is false if
                    # either is non-empty, so showing only one would let a card
                    # say "not confirmable" without saying why.
                    "refusals": list(plan.refusals) + list(plan.external_refusals),
                    "notes": list(getattr(plan, "notes", ()) or ()),
                    "recommendation": getattr(plan, "recommendation", ""),
                }
            )
            owner_actions.set_payload(row, payload)
            row.card_md = _render_plan(plan, payload)
            if not plan.confirmable:
                owner_actions.finish(
                    session,
                    row,
                    status="refused",
                    outcome_code="not_confirmable",
                    outcome_detail="; ".join(payload["refusals"]) or "not confirmable",
                )
                out = owner_actions.payload_of(row)
                session.commit()
                return {"action_uid": out["action_uid"], "outcome": "refused"}

            owner_actions.mint_confirmation(
                row,
                owner_id=approvals.resolve_owner_id(self.settings),
                settings=self.settings,
            )
            row.status = "prepared"
            # Released rather than finished: the row's next transition is the
            # owner's, and the claim would otherwise stop the second pass from
            # ever taking it.
            owner_actions.release(session, row)
            out = owner_actions.payload_of(row)
            session.commit()
        log.info("tier_change_prepared", action_uid=out["action_uid"])
        return {"action_uid": out["action_uid"], "outcome": "prepared"}

    def _confirm_tier_change(self, action_id: int) -> dict:
        from database.models import OwnerAction
        from strategy_lab.domain import ExecutionMode

        wiring = self.promotion_wiring or self._default_promotion_wiring()

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            payload = dict(row.payload)
            kind = row.kind
            owner = str(
                getattr(self.settings, "strategy_lab_experiment_owner", "") or "bryan"
            )
            try:
                request = wiring.build_request(
                    session,
                    self.settings,
                    source_arm_id=int(payload.get("source_arm_id") or 0),
                    to_mode=ExecutionMode(str(payload.get("to_mode"))),
                    owner=owner,
                    reason=payload.get("reason") or f"owner {kind} over MCP",
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                return self._finish(
                    action_id,
                    status="executed",
                    outcome_code="cannot_plan",
                    outcome_detail=str(exc)[:1000],
                )

        # The confirmation is signed over the *prepared* plan — target arm,
        # evidence snapshot, mode and budget. Rebuilding the request here is
        # what makes the tier change act on current data, and it is also the one
        # way the two could diverge: an evaluator run between the two passes
        # moves the evidence snapshot. So compare, and refuse rather than
        # execute a plan the owner did not see.
        drift = _drift(payload, request)
        if drift:
            self._page(
                "owner_action_refused",
                {
                    "recovery": (
                        "not promoted: the plan changed after it was confirmed "
                        f"({drift}). Ask again against the world as it now is."
                    )
                },
            )
            return self._finish(
                action_id,
                status="executed",
                outcome_code="plan_changed",
                outcome_detail=(
                    "the confirmation was signed over a different plan than the "
                    f"one that would run now: {drift}. Nothing was changed."
                ),
            )

        try:
            result = wiring.confirm(self.settings, request, adapters=self.adapters)
        except Exception as exc:
            self._page(
                "owner_action_refused",
                {"recovery": f"not promoted: {str(exc)[:300]}"},
            )
            return self._finish(
                action_id,
                status="executed",
                outcome_code="promotion_refused",
                outcome_detail=str(exc)[:1000],
            )

        detail = (
            f"{result['kind']} #{result['promotion_id']}: arm "
            f"#{result['source_arm_id']} ({result['from_mode']}) -> arm "
            f"#{result['target_arm_id']} ({result['to_mode']}), risk budget "
            f"{result['new_risk_budget']}. This approved no entry: every "
            "execution the arm proposes still needs its own signed, expiring, "
            "single-use owner approval."
        )
        self._page("owner_action_promoted", {"recovery": detail})
        return self._finish(
            action_id,
            status="executed",
            outcome_code=str(result.get("kind") or "promoted"),
            outcome_detail=detail,
        )

    @staticmethod
    def _default_promotion_wiring():
        from orchestrator import strategy_lab_promotion

        return strategy_lab_promotion

    # -- helpers ------------------------------------------------------------

    def _finish(self, action_id: int, *, status: str, outcome_code: str, outcome_detail: str) -> dict:
        from database.models import OwnerAction
        from portfolio import owner_actions

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            owner_actions.finish(
                session,
                row,
                status=status,
                outcome_code=outcome_code,
                outcome_detail=outcome_detail,
            )
            out = owner_actions.payload_of(row)
            session.commit()
        return {
            "action_uid": out["action_uid"],
            "kind": out["kind"],
            "outcome": outcome_code,
            "detail": outcome_detail,
        }

    def _release(self, action_id: int) -> None:
        from database.models import OwnerAction
        from portfolio import owner_actions

        with self.session_factory() as session:
            row = session.get(OwnerAction, int(action_id))
            owner_actions.release(session, row)
            session.commit()

    def _page(self, event: str, detail: dict) -> None:
        """Tell the owner. Never raises: a channel failure must not lose the row."""
        if self.notify is None:
            log.info("owner_action_not_notified", notice=event, **_safe(detail))
            return
        try:
            self.notify(event, detail)
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error("owner_action_notify_failed", event=event, error=str(exc)[:200])


def _drift(payload: dict, request) -> str:
    """What differs between the confirmed plan and the one that would run now."""
    expected = {
        "target_arm_id": payload.get("target_arm_id"),
        "evidence_metric_snapshot_id": payload.get("evidence_metric_snapshot_id"),
        "to_mode": payload.get("to_mode"),
        "requested_risk_budget": payload.get("requested_risk_budget"),
    }
    actual = {
        "target_arm_id": request.target_arm_id,
        "evidence_metric_snapshot_id": request.evidence_metric_snapshot_id,
        "to_mode": request.requested_mode.value,
        "requested_risk_budget": float(request.requested_risk_budget),
    }
    differing = [
        f"{name}: confirmed {expected[name]!r}, now {actual[name]!r}"
        for name in expected
        if _differs(expected[name], actual[name])
    ]
    return "; ".join(differing)


def _differs(expected, actual) -> bool:
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return abs(float(expected) - float(actual)) > 1e-9
        except (TypeError, ValueError):
            return True
    return expected != actual


def _safe(detail: dict) -> dict:
    return {k: v for k, v in (detail or {}).items() if k != "recovery"}


def _render_plan(plan, payload: dict) -> str:
    """The tier-change card, in plain text.

    Deliberately not ``bot/handlers/strategy_lab.py::render_promotion``: that one
    emits Telegram MarkdownV2, which is unreadable in a terminal and lives behind
    an import this module may not make anyway. The *content* is the same, and it
    is the plan's own lists verbatim — nothing here decides anything, and the
    word "promote" never appears as a recommendation (Spec Q §3).
    """
    lines = [
        f"TIER CHANGE — arm #{payload.get('source_arm_id')} -> "
        f"{payload.get('to_mode')}",
        "",
        f"target arm:        #{payload.get('target_arm_id')} (inactive until confirmed)",
        f"evidence snapshot: #{payload.get('evidence_metric_snapshot_id')}",
        f"risk budget:       {payload.get('requested_risk_budget')}",
        f"recommendation:    {payload.get('recommendation') or 'none'}",
        "",
    ]
    refusals = payload.get("refusals") or []
    notes = payload.get("notes") or []
    lines.append("refusals:" if refusals else "refusals: none")
    lines += [f"  - {r}" for r in refusals]
    lines.append("notes:" if notes else "notes: none")
    lines += [f"  - {n}" for n in notes]
    lines.append("")
    if payload.get("confirmable"):
        lines.append(
            "Confirming appends a promotion_events row and activates the target "
            "arm. It is NOT an approval of any entry: every execution the arm "
            "proposes still needs its own signed, expiring, single-use owner "
            "approval."
        )
    else:
        lines.append(
            "NOT CONFIRMABLE. Nothing was changed and there is nothing to "
            "confirm; the refusals above say why."
        )
    return "\n".join(lines)
