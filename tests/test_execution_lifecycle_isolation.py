"""The boundary Phase 6 must not breach (Spec L §6.1, Spec K §8).

Phase 6 adds the one write tool on the MCP surface (``propose_order``) and the
one module that reaches a broker after a human approves
(``execution/lifecycle.py``). The whole safety argument is that the first cannot
reach the second, and neither can any other agent-facing surface. These are the
static assertions that hold it — reusing the same import-graph walker
``tests/test_no_execute_scope.py`` and ``tests/test_portfolio_import_graph.py``
already trust, extended to the Phase 6 entry points.

The assertions:

* ``propose_order``'s tool module and the whole workspace closure still reach no
  broker placement, with the flag on;
* nothing reachable from the workspace names ``place_equity_order`` /
  ``place_stop`` / the option and crypto write tools;
* ``execution/lifecycle.py`` is *not* reachable from the workspace, and the bot
  handler that calls it is not either — the approval callback can be triggered
  only from the Telegram path, never from an MCP tool or a REST route.

Phase 5 (Spec Q §12) adds a second caller of ``on_approval``:
``execution/strategy_lifecycle.py``, which runs a Strategy Lab arm through this
same service. That does not widen the boundary and the test below does not
pretend otherwise — it *narrows* the claim to the one that actually matters and
then asserts more of it. The property is not "one file calls this method"; it is
"nothing an agent can reach calls this method". So:

* a caller outside ``execution/`` must be the out-of-band Telegram handler and
  nothing else — the exact-list assertion, unchanged in force;
* a caller inside ``execution/`` is allowed, and is separately asserted to be
  unreachable from the workspace closure, which is the same guarantee
  ``execution/lifecycle.py`` itself relies on;
* the workspace still may not so much as *name* ``on_approval``.

A new file under ``execution/`` therefore cannot quietly become an agent-facing
approval path: it would have to first appear in the workspace closure, and the
closure assertion would fail.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.test_no_execute_scope import (
    FORBIDDEN_ROOTS,
    WORKSPACE_ENTRY_POINTS,
    import_closure,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The Phase 6 additions to the workspace surface. If either escapes the
#: closure test below, the boundary is broken.
PHASE6_WORKSPACE_MODULES = ("workspace.proposal_tools", "workspace.proposal_card")

#: Names that place an order or its protective exit. None may appear in a module
#: the workspace can reach. The option/crypto write tools are deliberately left
#: to ``tests/test_portfolio_import_graph.py``, which scans them repo-wide;
#: naming them here too would trip that scan's own no-reference assertion.
PLACEMENT_NAMES = (
    "place_equity_order",
    "place_order(",  # the adapter method call, not the Protocol declaration
    "place_stop(",
)


def _workspace_closure():
    # Include the Phase 6 modules explicitly: they are only imported when the
    # flag is on, and the walker follows static imports, so naming them makes
    # the assertion hold regardless of the flag's default.
    return import_closure(tuple(WORKSPACE_ENTRY_POINTS) + PHASE6_WORKSPACE_MODULES)


class WorkspaceStillReachesNoBrokerTests(unittest.TestCase):
    def test_the_walker_reaches_the_proposal_tool_and_service(self):
        """A control: the closure actually includes the Phase 6 tool module."""
        closure = _workspace_closure()
        self.assertIn("workspace.proposal_tools", closure)
        self.assertIn(
            "portfolio.proposals",
            closure,
            "the proposal tool should reach the ledger-side proposal service; "
            "if it does not, the walker is not following the imports this test "
            "is about.",
        )

    def test_the_workspace_closure_never_reaches_a_broker(self):
        closure = _workspace_closure()
        offenders = {
            module: " -> ".join(path)
            for module, path in closure.items()
            if module.split(".")[0] in FORBIDDEN_ROOTS
        }
        self.assertEqual(
            offenders,
            {},
            "a module reachable from the workspace (with propose_order "
            "registered) can reach execution/, bot/, orchestrator/ or agents/. "
            "propose_order proposes; it must not be able to place (Spec L §6.1).",
        )

    def test_no_workspace_module_names_a_placement_call(self):
        closure = _workspace_closure()
        offenders = []
        for module in sorted(closure):
            path = REPO_ROOT / (module.replace(".", "/") + ".py")
            if not path.exists():
                path = REPO_ROOT / (module.replace(".", "/") + "/__init__.py")
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for needle in PLACEMENT_NAMES:
                if needle in text:
                    offenders.append(f"{module}: {needle}")
        self.assertEqual(offenders, [], f"placement names reachable from the workspace: {offenders}")


class ExecutionServiceIsUnreachableTests(unittest.TestCase):
    def test_lifecycle_is_not_in_the_workspace_closure(self):
        """The execution service the callback drives is off-limits to the workspace."""
        closure = _workspace_closure()
        self.assertNotIn(
            "execution.lifecycle",
            closure,
            "execution/lifecycle.py is reachable from the workspace surface; "
            "the approval-to-placement path must be callable only from the "
            "out-of-band channel (Spec L §6.1).",
        )

    def _on_approval_callers(self) -> list[str]:
        callers = []
        for path in sorted(REPO_ROOT.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            if rel.parts[0] in ("tests", "evals"):
                continue
            text = path.read_text(encoding="utf-8")
            if ".on_approval(" in text:
                callers.append(rel.as_posix())
        return sorted(callers)

    def test_only_the_bot_handler_calls_on_approval_from_outside_execution(self):
        """Outside `execution/`, the Telegram handler is the only caller.

        A grep-level control on top of the import graph: even if some module
        could import the service, only the bot's proposal handler and the wiring
        in main.py may *call* the entry point into placement. No workspace or
        REST module may.
        """
        outside = [c for c in self._on_approval_callers() if not c.startswith("execution/")]
        self.assertEqual(
            outside,
            ["bot/handlers/proposals.py"],
            "on_approval is called from somewhere other than the bot's "
            "out-of-band approval handler; the approval callback must not be "
            "reachable from an MCP tool or a REST route.",
        )

    def test_every_execution_side_caller_is_unreachable_from_the_workspace(self):
        """The other half, and the one that carries the guarantee.

        A caller inside `execution/` is fine *because* the workspace cannot
        import `execution/` at all — the same argument `execution/lifecycle.py`
        rests on. Asserted here rather than assumed, so that a new file under
        `execution/` cannot become an agent-facing approval path without this
        failing first.
        """
        closure = _workspace_closure()
        inside = [c for c in self._on_approval_callers() if c.startswith("execution/")]
        self.assertNotEqual(inside, [], "the control is stale: no execution-side caller found")
        for caller in inside:
            module = caller[: -len(".py")].replace("/", ".")
            with self.subTest(module):
                self.assertNotIn(
                    module,
                    closure,
                    f"{caller} calls on_approval and is reachable from the "
                    "workspace surface; placement must be callable only from "
                    "the out-of-band channel (Spec L §6.1).",
                )
        self.assertIn("execution/strategy_lifecycle.py", inside)

    def test_no_workspace_module_names_on_approval(self):
        """The workspace never even names the placement entry point."""
        for path in sorted((REPO_ROOT / "workspace").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "on_approval",
                text,
                f"{path.name} names on_approval; the workspace may not reach the "
                "execution service (Spec L §6.1).",
            )


if __name__ == "__main__":
    unittest.main()
