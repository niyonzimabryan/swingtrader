"""The ``BrokerCapabilities`` contract — re-exported from :mod:`portfolio.capabilities`.

The contract itself now lives in ``portfolio/capabilities.py``, unchanged. It
moved for one reason, and it is the reason this module still exists: Phase 6's
``propose_order`` has to run the capability gate **before** it forms a proposal
(Spec L §5: callers check capabilities before intent), and ``propose_order`` is
reachable from the MCP surface, which may never import ``execution/`` — Spec L
§6.1, asserted by ``tests/test_no_execute_scope.py`` and
``tests/test_portfolio_import_graph.py``.

The alternative was a second copy of the three refusal rules on the ledger
side, kept in sync by hope. One gate with two import paths is the safer of the
two, and the dependency arrow still points the right way: ``execution`` imports
``portfolio``, never the reverse.

Adapters and every existing caller keep importing from here.
"""

from __future__ import annotations

from portfolio.capabilities import (  # noqa: F401
    PROTECTIVE_EXIT_CAPABILITIES,
    BrokerCapabilities,
    CapabilityRefused,
    OrderIntent,
    gate_intent,
)

__all__ = [
    "PROTECTIVE_EXIT_CAPABILITIES",
    "BrokerCapabilities",
    "CapabilityRefused",
    "OrderIntent",
    "gate_intent",
]
