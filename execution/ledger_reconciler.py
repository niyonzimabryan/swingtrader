"""Read-only coverage for a live position at a broker no monitor speaks.

**The gap.** With ``EXECUTION_MODE=live`` and ``BROKER_PRIMARY=robinhood`` an
owner-approved entry becomes a real Robinhood position, and nothing watched it.
``OrderMonitor`` filters ``Trade.broker == "alpaca"`` by design — it speaks the
Alpaca order API — and ``PositionMonitor`` holds an Alpaca client by
construction. Its docstring records the decision this module honours: *rather
than teach it a second broker, it takes injected reconcilers.* Nothing was
injected, so the documented mitigation was documentation.

**What this is.** A zero-argument callable — the contract
``PositionMonitor.execution_reconcilers`` takes — that on each tick asks one
broker what it holds, compares that to the ``trades`` ledger, and pages on
divergence. That is all it does.

**Read-only, deliberately.** It calls exactly one broker method,
``get_positions_detail``. It does not place, modify or cancel anything
(non-negotiable 1), and it writes no database row either. The alternative —
active management, checking that the protective stop is still live and
re-placing a vanished one — was rejected for now because it would be built on
``gtc stop_market`` behaviour the Spec L §5.1 probe has never verified. Nobody
has observed a Robinhood standalone stop survive a session or trigger.
Automating on top of that would produce a position that believes it is
protected and is not, which is worse than one nobody claims is protected. When
the probe passes, that is a separate, stated change.

**Absence of data is never absence of exposure.** A broker call that raises, an
adapter that returns ``None``, or an adapter configured with no account number
all page and re-raise. None of them is allowed to resolve to "the account is
flat", which would read as "every ledger row is missing at the broker" — the
loudest possible wrong answer.
"""

from __future__ import annotations

from database.db import get_session
from portfolio import paging
from tracking import position_reconciliation as recon
from utils.logger import get_logger

log = get_logger("ledger_reconciler")

#: Stable page events — an alert rule matches on these strings.
LEDGER_MISMATCH = "broker_ledger_mismatch"
LEDGER_UNREACHABLE = "broker_ledger_unreachable"

_RECOVERY_SUFFIX = (
    "Compare the position in the broker's own app and correct the side that is "
    "wrong. Do not place a compensating order from here."
)


class BrokerPositionsUnavailable(RuntimeError):
    """The broker could not be asked what it holds, so nothing was concluded."""


class LedgerReconciler:
    """One read-only pass over one broker: ask, compare, page, report.

    ``broker`` is the concrete adapter, never the :class:`BrokerRouter`. The
    monitors bind to a concrete broker for the same reason — a ``/mode`` switch
    must not silently re-point an in-flight position's coverage at a different
    account.

    Pages are de-duplicated against the previous pass: a divergence that is
    still there in sixty seconds is the same divergence, and a page that repeats
    every minute is a page that gets muted. A finding that clears and later
    recurs pages again.
    """

    def __init__(self, broker, *, broker_name: str | None = None, pager=None, session_factory=None):
        self.broker = broker
        self.broker_name = str(broker_name or getattr(broker, "name", "") or "").lower()
        self.pager = pager or paging.log_pager
        self.session_factory = session_factory or get_session
        self._paged: set[tuple] = set()
        self._unreachable_paged = False

    def __call__(self) -> recon.ExecutionReconciliation:
        positions = self._positions()
        self._unreachable_paged = False
        with self.session_factory() as session:
            report = recon.reconcile_ledger_positions(
                session,
                positions=positions,
                broker=self.broker_name,
                broker_account_id=self._account_id(),
            )
        log.info(
            "ledger_reconciled",
            broker=self.broker_name,
            findings=len(report.findings),
            mismatches=len(report.mismatches),
        )
        self._page_mismatches(report)
        return report

    # -- broker ------------------------------------------------------------- #

    def _account_id(self) -> str | None:
        # Only used to narrow the ledger query, and never put in a page: an
        # account number does not belong in a notification or a log line.
        return str(getattr(self.broker, "account_number", "") or "") or None

    def _positions(self):
        """The broker's positions, or an exception. Never a silent empty list."""
        if hasattr(self.broker, "account_number") and not self._account_id():
            raise self._unreachable(
                "the adapter has no account number configured, so it reports no "
                "positions whether or not the account holds any."
            )
        try:
            positions = self.broker.get_positions_detail()
        except Exception as exc:
            raise self._unreachable(f"{type(exc).__name__}: {exc}") from exc
        if positions is None:
            raise self._unreachable("the adapter returned None rather than a position list.")
        return positions

    def _unreachable(self, detail: str) -> BrokerPositionsUnavailable:
        if not self._unreachable_paged:
            self._unreachable_paged = True
            self._send(
                LEDGER_UNREACHABLE,
                {
                    "broker": self.broker_name,
                    "reason": detail,
                    "recovery": (
                        f"{self.broker_name} could not be asked what it holds, so this "
                        "tick concluded nothing about the live account — it is NOT a "
                        "report that the account is flat. Any live position is "
                        "unwatched until this clears. Check the adapter's "
                        "connectivity and credentials."
                    ),
                },
            )
        return BrokerPositionsUnavailable(
            f"{self.broker_name} positions are unavailable: {detail}"
        )

    # -- paging ------------------------------------------------------------- #

    def _page_mismatches(self, report: recon.ExecutionReconciliation) -> None:
        seen: set[tuple] = set()
        for finding in report.mismatches:
            key = (
                finding.kind,
                finding.ticker,
                finding.trade_id,
                round(finding.expected_quantity, 6),
                round(finding.broker_quantity, 6),
            )
            seen.add(key)
            if key in self._paged:
                continue
            self._send(
                LEDGER_MISMATCH,
                {
                    "broker": self.broker_name,
                    "trade_id": finding.trade_id,
                    "ticker": finding.ticker,
                    "reason_code": finding.kind,
                    "expected_quantity": finding.expected_quantity,
                    "broker_quantity": finding.broker_quantity,
                    "recovery": f"{finding.detail} {_RECOVERY_SUFFIX}",
                },
            )
        self._paged = seen

    def _send(self, event: str, detail: dict) -> None:
        try:
            self.pager(event, detail)
        except Exception as exc:  # pragma: no cover - channel-specific
            # A page is a report about something that already went wrong; a
            # delivery failure inside it must not become the failure.
            log.error("ledger_reconciler_page_failed", event=event, error=str(exc))


def live_ledger_reconcilers(pipeline, settings, *, pager=None) -> tuple:
    """The reconcilers ``PositionMonitor`` should run, for this execution mode.

    Empty in paper mode, where the router's active broker *is* the paper broker
    the monitor already polls: injecting anything there would change paper
    behaviour, and there is nothing it is not already watching.

    Non-empty exactly when ``EXECUTION_MODE`` selects a different primary broker
    — today, live Robinhood. The reconciler is bound to that concrete adapter,
    resolved once at startup, so coverage does not follow a runtime ``/mode``
    switch. Neither do the monitors; a restart is what re-points either.
    """
    paper = getattr(pipeline, "paper_broker", None)
    active = getattr(getattr(pipeline, "broker", None), "active", None)
    if paper is None or active is None or active is paper:
        return ()
    if pager is None:
        pager = paging.pager_for(settings)
    return (LedgerReconciler(active, pager=pager),)
