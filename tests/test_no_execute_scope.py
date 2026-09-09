"""``test_no_execute_scope`` (Spec K §8): no token, route, or MCP tool can place an order.

Spec K asks for this to be "enforced by an import-graph assertion, not by
convention", and that is the shape of the test: the first-party import closure
of the workspace service is computed statically from the sources and asserted
never to reach ``execution`` (broker adapters), ``bot`` (the approval channel),
or ``orchestrator``. A module that cannot be imported cannot be called, whatever
a future tool's body says.

Static analysis rather than runtime ``sys.modules`` inspection on purpose: a
runtime check only sees the imports that a particular code path happened to
execute, and the dangerous import is exactly the one inside a function that this
test run never enters.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from workspace import scopes as scope_module
from workspace.tools import REGISTERED_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Top-level first-party packages. Anything else in an import statement is a
#: third-party or standard-library module and is not followed.
FIRST_PARTY = {
    "agents",
    "backtest",
    "bot",
    "comparables",
    "config",
    "data",
    "database",
    "evals",
    "execution",
    "filings",
    "memo",
    "orchestrator",
    "portfolio",
    "scanning",
    "scoring",
    "screening",
    "scripts",
    "tools",
    "tracking",
    "utils",
    "workspace",
}

#: Root-level modules that are entry points rather than packages.
FIRST_PARTY_ROOT_MODULES = {"main"}

#: Packages the workspace service must never be able to reach. ``execution``
#: holds every broker adapter and every order-placement call; ``bot`` and
#: ``orchestrator`` reach it in turn.
FORBIDDEN_ROOTS = {"execution", "bot", "orchestrator", "agents"}

#: The workspace entry points. Everything reachable from these is the surface.
#: Phase 1 added ``portfolio`` to FIRST_PARTY above, so the walker follows the
#: ledger package the tool surface now imports; without it the closure would
#: stop at ``workspace/`` and this assertion would be vacuous.
WORKSPACE_ENTRY_POINTS = (
    "workspace.app",
    "workspace.server",
    "workspace.tools",
    "workspace.auth",
    "workspace.tokens",
    "workspace.scopes",
    "workspace.ratelimit",
    "workspace.oauth",
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _source_modules() -> dict[str, Path]:
    modules = {}
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
            continue
        if len(rel.parts) == 1:
            if rel.stem not in FIRST_PARTY_ROOT_MODULES:
                continue
        elif rel.parts[0] not in FIRST_PARTY:
            continue
        modules[_module_name(path)] = path
    return modules


def _imports_of(path: Path, modules: dict[str, Path]) -> set[str]:
    """First-party modules this file imports, at any nesting depth.

    ``ast.walk`` rather than a top-level scan, because a deferred import inside
    a function is still an import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    def record(dotted: str) -> None:
        root = dotted.split(".")[0] if dotted else ""
        if not root or (root not in FIRST_PARTY and root not in FIRST_PARTY_ROOT_MODULES):
            return
        # `from database import db` names a module; `from database.schema import
        # classify` names a symbol. Record whichever of the two exists.
        parts = dotted.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in modules:
                found.add(candidate)
                return
            parts.pop()
        found.add(dotted.split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against this module
                base = _module_name(path).split(".")
                base = base[: len(base) - node.level + 1]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            record(prefix)
            for alias in node.names:
                record(f"{prefix}.{alias.name}" if prefix else alias.name)
    return found


def import_closure(roots) -> dict[str, list[str]]:
    """Every first-party module reachable from ``roots``, with one path to it."""
    modules = _source_modules()
    paths: dict[str, list[str]] = {}
    queue = []
    for root in roots:
        if root in modules:
            paths[root] = [root]
            queue.append(root)
    while queue:
        current = queue.pop()
        for imported in sorted(_imports_of(modules[current], modules)):
            if imported in paths or imported not in modules:
                continue
            paths[imported] = paths[current] + [imported]
            queue.append(imported)
    return paths


class NoExecuteScopeTests(unittest.TestCase):
    def test_no_execute_scope_exists(self):
        self.assertNotIn("execute", scope_module.SCOPES)
        self.assertNotIn("execute", scope_module.TOOL_SCOPES.values())
        for scope in scope_module.SCOPES:
            self.assertNotIn("execute", scope)

    def test_no_registered_tool_claims_a_scope_outside_the_model(self):
        for name in REGISTERED_TOOLS:
            self.assertIn(scope_module.TOOL_SCOPES[name], scope_module.SCOPES)

    def test_the_workspace_import_closure_never_reaches_a_broker(self):
        closure = import_closure(WORKSPACE_ENTRY_POINTS)
        offenders = {
            module: " -> ".join(path)
            for module, path in closure.items()
            if module.split(".")[0] in FORBIDDEN_ROOTS
        }
        self.assertEqual(
            offenders,
            {},
            "The workspace service can reach a module that can place an order. "
            "No agent-facing surface may import execution/, bot/, or "
            "orchestrator/ — Spec K §8, Spec L §6.",
        )

    def test_the_guard_would_catch_a_real_violation(self):
        """A negative control: the closure of a module that *does* reach a broker."""
        closure = import_closure(["main"])
        self.assertIn("main", closure, "main.py was not found by the walker")
        reached = {m.split(".")[0] for m in closure} & FORBIDDEN_ROOTS
        self.assertEqual(
            reached,
            FORBIDDEN_ROOTS,
            "the bot entry point should reach every forbidden root; if it does "
            "not, the walker is not actually following imports and the "
            "workspace assertion above proves nothing",
        )

    def test_the_bot_does_not_import_the_workspace_either(self):
        """Spec K §4: a workspace deploy must never restart the trading monitor."""
        closure = import_closure(["main"])
        offenders = {
            module: " -> ".join(path)
            for module, path in closure.items()
            if module.split(".")[0] == "workspace"
        }
        self.assertEqual(offenders, {})

    def test_no_workspace_module_names_an_order_placement_call(self):
        """Belt and braces: the words, not just the imports."""
        forbidden = ("place_order", "submit_order", "place_equity_order", "create_order")
        offenders = []
        for path in sorted((REPO_ROOT / "workspace").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
