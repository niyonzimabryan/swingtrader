"""``propose_order`` on the MCP surface, at scope ``propose`` (Spec K §4.2).

This is the only tool in the workspace that is not read-only, and it is worth
being precise about what "not read-only" means here: it writes a ``proposals``
row and sends an approval card to the owner's out-of-band channel. **It places
nothing.** There is no execute scope for a token to carry (Spec K §4.1), no
route that reaches a broker, and no import path from this package to one — the
tool body below calls :mod:`portfolio.proposals`, which lives on the ledger side
of the boundary precisely so that this file can call it
(``tests/test_no_execute_scope.py``, ``tests/test_portfolio_import_graph.py``).

Two consequences of that boundary are visible in the code and are not
incidental:

* the approval card leaves through an **injected callable**
  (:func:`portfolio.approvals.register_card_sender`), because this surface may
  not import ``bot``; and
* the tool takes **no quantity**. Passing one is refused, not ignored: size is
  computed by code from ``entry``, ``stop`` and ``risk_fraction`` under the caps
  (Spec L §6.6). Ignoring the argument would let a caller believe it had been
  honoured.

The tool is registered only when ``PHASE6_EXECUTION_ENABLED`` is true. A tool
that answers "disabled" still tells a client this workspace can propose orders,
and with the flag off it cannot.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import Context, FastMCP

#: Registered when the flag is on. The scope lives in ``workspace.scopes``.
PROPOSAL_TOOLS: tuple[str, ...] = ("propose_order",)

DESCRIPTION = (
    "Propose a long entry for the owner to approve out of band. Creates a "
    "`proposed` row and sends an approval card; it PLACES NOTHING and cannot — "
    "no token scope, route, or import path in this service reaches a broker. "
    "Takes `ticker`, `entry`, `stop`, `risk_fraction` (a FRACTION of equity: "
    "0.005 means half a percent — a value at or above 0.05 is refused as a "
    "percentage typed as a fraction), an optional `cohort_answer_id`, and "
    "`expected_hold_sessions`. It does NOT take a quantity: size is computed "
    "from entry, stop and risk_fraction under the caps, rounded DOWN to whole "
    "shares, and a size that rounds to zero is refused. A citation that "
    "resolves to a depth='full', status='ok' answer for the same ticker within "
    "the last 5 sessions, with a positive lower 90% bound on the "
    "policy-simulated net return, draws on the evidenced budget scaled by "
    "m = LB/PE; everything else is labelled `discretionary` and draws on a "
    "separate, smaller budget with the evidence printed in full, including a "
    "negative lower bound. Nothing is suppressed for lack of evidence. A "
    "proposal that breaches a limit comes back with status `risk_rejected` AND "
    "its reason, so it can be explained rather than silently dropped."
)


def register(mcp: FastMCP, settings, *, authorize_call, ToolRefused) -> tuple[str, ...]:
    """Attach ``propose_order``. Returns the names registered."""

    @mcp.tool(name="propose_order", description=DESCRIPTION)
    async def propose_order(
        ctx: Context,
        ticker: str = "",
        entry: float = 0.0,
        stop: float = 0.0,
        risk_fraction: float = 0.0,
        cohort_answer_id: str = "",
        expected_hold_sessions: int = 0,
        side: str = "long",
        quantity: float | None = None,
    ) -> dict:
        # Every argument carries a default so that the scope check runs before
        # argument validation — the same reason `position_detail` does it. An
        # unauthorised caller gets `insufficient_scope` and a call-log entry,
        # not a schema error that describes the tool's shape.
        identity = authorize_call(
            ctx,
            "propose_order",
            {
                "ticker": ticker,
                "entry": entry,
                "stop": stop,
                "risk_fraction": risk_fraction,
                "cohort_answer_id": cohort_answer_id,
                "expected_hold_sessions": expected_hold_sessions,
                "side": side,
            },
        )
        if quantity is not None:
            raise ToolRefused(
                "quantity_not_accepted: propose_order does not take a quantity. "
                "Size is computed from entry, stop and risk_fraction under the "
                "caps — an agent never chooses a quantity (Spec L §6.6). Pass "
                "the risk_fraction you intend and read the computed size off "
                "the card."
            )
        if not (ticker or "").strip():
            raise ToolRefused("invalid_argument: ticker is required.")

        try:
            return await anyio.to_thread.run_sync(
                _propose,
                settings,
                (ticker or "").strip().upper(),
                float(entry),
                float(stop),
                risk_fraction,
                (cohort_answer_id or "").strip(),
                int(expected_hold_sessions or 0) or None,
                (side or "long"),
                getattr(identity, "label", "") or "",
            )
        except _Refused as exc:
            raise ToolRefused(str(exc)) from exc

    return PROPOSAL_TOOLS


class _Refused(ValueError):
    """Carries a store-side refusal across the worker-thread boundary."""


def _propose(
    settings,
    ticker: str,
    entry: float,
    stop: float,
    risk_fraction,
    cohort_answer_id: str,
    expected_hold_sessions: int | None,
    side: str,
    token_label: str,
) -> dict:
    """Synchronous body, run on a worker thread: the session is blocking."""
    from database.db import get_session
    from portfolio import proposals
    from portfolio.approvals import ApprovalRefused

    owner_id = str(getattr(settings, "telegram_chat_id", "") or "")
    try:
        with get_session() as session:
            row = proposals.create_proposal(
                session,
                ticker=ticker,
                entry=entry,
                stop=stop,
                risk_fraction=risk_fraction,
                cohort_answer_id=cohort_answer_id,
                expected_hold_sessions=expected_hold_sessions,
                side=side,
                settings=settings,
                requester_token_label=token_label,
                owner_id=owner_id,
                now=None,
            )
            payload = proposals.proposal_payload(row)
            session.commit()
            return payload
    except proposals.ProposalRefused as exc:
        raise _Refused(f"{exc.code}: {exc.message}") from exc
    except ApprovalRefused as exc:
        raise _Refused(f"{exc.code}: {exc.message}") from exc
