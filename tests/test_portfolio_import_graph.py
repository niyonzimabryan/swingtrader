"""Spec L §8: test_no_agent_path_to_broker and test_no_option_or_crypto_write_tools.

Two assertions, both static, both about code that does *not* exist rather than
code that does — which is why neither can be satisfied by a docstring or a
prompt instruction.

``test_no_agent_path_to_broker``
    nothing reachable from the workspace's MCP surface imports a module that
    can place an order, and nothing reachable from it names one. The import
    closure is computed by the same walker ``tests/test_no_execute_scope.py``
    uses, extended to follow ``portfolio`` — the package Phase 1 added between
    the tool surface and the database. Without that extension the walker would
    stop at ``portfolio.ledger`` and the assertion would be vacuous.

``test_no_option_or_crypto_write_tools``
    no module anywhere in the repository names ``place_option_order``,
    ``place_crypto_order``, or their review and cancel tools. Options are
    readable and *tradeable* through the Robinhood MCP (Spec L §5.1); the
    workspace never uses the write half, and this is what keeps that true when
    someone reaches for the obvious tool name in a hurry.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.test_no_execute_scope import (
    FIRST_PARTY,
    FORBIDDEN_ROOTS,
    WORKSPACE_ENTRY_POINTS,
    import_closure,
)
from workspace.tools import REGISTERED_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The Robinhood option and crypto **write** tools. Read tools
#: (``get_option_positions``) are used and are deliberately absent from this
#: list: the rule is about placing, not about seeing.
FORBIDDEN_TOOL_NAMES = (
    "place_option_order",
    "review_option_order",
    "cancel_option_order",
    "replace_option_order",
    "place_crypto_order",
    "review_crypto_order",
    "cancel_crypto_order",
    "replace_crypto_order",
)

#: Equity placement tool names, which may appear only in the execution package.
EQUITY_WRITE_TOOL_NAMES = (
    "place_equity_order",
    "review_equity_order",
    "cancel_equity_order",
)

#: Files that are allowed to contain the forbidden strings, because naming them
#: is their entire job. Kept to exactly this test.
NAMING_EXEMPT = {"tests/test_portfolio_import_graph.py"}


def _python_sources():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
            continue
        if rel.as_posix() in NAMING_EXEMPT:
            continue
        yield rel, path.read_text(encoding="utf-8")


class NoAgentPathToBrokerTests(unittest.TestCase):
    def test_the_walker_follows_the_portfolio_package(self):
        """A negative control for the control.

        If ``portfolio`` were missing from the walker's first-party set, the
        closure below would stop at the workspace's own modules and the
        assertion would pass no matter what ``portfolio/`` imported.
        """
        self.assertIn(
            "portfolio",
            FIRST_PARTY,
            "the import-graph walker does not follow `portfolio`, so the "
            "assertion below proves nothing about it.",
        )
        closure = import_closure(WORKSPACE_ENTRY_POINTS)
        self.assertIn(
            "portfolio.ledger",
            closure,
            "the workspace tool surface should reach the ledger reader; if it "
            "does not, the walker is not following the imports this test is about.",
        )

    def test_no_agent_path_to_broker(self):
        """Nothing reachable from the MCP surface imports a placement method."""
        closure = import_closure(WORKSPACE_ENTRY_POINTS)
        offenders = {
            module: " -> ".join(path)
            for module, path in closure.items()
            if module.split(".")[0] in FORBIDDEN_ROOTS
        }
        self.assertEqual(
            offenders,
            {},
            "a module reachable from the workspace can reach an order "
            "placement call (Spec L §6.1).",
        )

        # And the words, not only the imports: a tool body that reached a
        # broker through a string, a getattr, or a subprocess would not show up
        # in the import graph at all.
        reachable = sorted(closure)
        named = []
        for module in reachable:
            path = REPO_ROOT / (module.replace(".", "/") + ".py")
            if not path.exists():
                path = REPO_ROOT / (module.replace(".", "/") + "/__init__.py")
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for needle in EQUITY_WRITE_TOOL_NAMES + FORBIDDEN_TOOL_NAMES:
                if needle in text:
                    named.append(f"{module}: {needle}")
        self.assertEqual(named, [])

    def test_the_phase_1_tools_are_registered_and_all_read_scoped(self):
        from workspace import scopes as scope_module

        for tool in ("portfolio_overview", "position_detail", "orders_open"):
            self.assertIn(tool, REGISTERED_TOOLS)
            self.assertEqual(scope_module.TOOL_SCOPES[tool], scope_module.READ)

    def test_the_portfolio_package_never_imports_execution(self):
        """The one-way dependency arrow that makes the assertion above cheap."""
        closure = import_closure(
            [
                f"portfolio.{name}"
                for name in (
                    "ledger",
                    "exposure",
                    "freshness",
                    "guards",
                    "settlement",
                    "wash_sale",
                    "dividends",
                    "metrics",
                    "sync",
                    "records",
                    "reconcile",
                    "paging",
                    "schedule",
                )
            ]
        )
        offenders = {
            module: " -> ".join(path)
            for module, path in closure.items()
            if module.split(".")[0] in FORBIDDEN_ROOTS
        }
        self.assertEqual(
            offenders,
            {},
            "portfolio/ must not import execution/, bot/, orchestrator/ or "
            "agents/ — the workspace reads this package.",
        )


class NoOptionOrCryptoWriteToolsTests(unittest.TestCase):
    def test_no_option_or_crypto_write_tools(self):
        """No module in the repository references an option or crypto write tool."""
        offenders = []
        for rel, text in _python_sources():
            for needle in FORBIDDEN_TOOL_NAMES:
                if needle in text:
                    offenders.append(f"{rel}: {needle}")
        self.assertEqual(
            offenders,
            [],
            "Options and crypto are readable through the Robinhood MCP and "
            "this system never writes them (Spec L §5.1).",
        )

    def test_the_scan_would_catch_a_real_reference(self):
        """A negative control: the same scan finds the equity write tools."""
        found = [
            rel.as_posix()
            for rel, text in _python_sources()
            if any(needle in text for needle in EQUITY_WRITE_TOOL_NAMES)
        ]
        self.assertIn(
            "execution/brokers/robinhood.py",
            found,
            "the scan found no reference to the equity placement tools "
            "anywhere, which means it is not actually reading the sources and "
            "the assertion above proves nothing.",
        )

    def test_the_equity_write_tools_are_named_only_where_they_belong(self):
        """`place_equity_order` may be named by the adapter and the schema dumper.

        Not by the workspace, not by the ledger, and not by any other script.
        ``scripts/dump_robinhood_tool_schemas.py`` is the one exception and it
        is listed by name rather than by directory: it *prints* the schema of a
        write tool, which is how Spec L §5.1's order-type facts were verified,
        and printing a schema is not placing an order.
        """
        allowed_outside_execution = {"scripts/dump_robinhood_tool_schemas.py"}
        offenders = [
            rel.as_posix()
            for rel, text in _python_sources()
            if any(needle in text for needle in EQUITY_WRITE_TOOL_NAMES)
            and rel.parts[0] not in {"execution", "tests", "docs"}
            and rel.as_posix() not in allowed_outside_execution
        ]
        self.assertEqual(offenders, [])

    def test_neither_the_workspace_nor_the_ledger_names_an_equity_write_tool(self):
        offenders = [
            f"{rel.as_posix()}: {needle}"
            for rel, text in _python_sources()
            if rel.parts[0] in {"workspace", "portfolio"}
            for needle in EQUITY_WRITE_TOOL_NAMES
            if needle in text
        ]
        self.assertEqual(offenders, [])

    def test_the_ledger_read_tools_are_all_reads(self):
        from execution.brokers.robinhood import LEDGER_READ_TOOLS

        for name in LEDGER_READ_TOOLS:
            self.assertTrue(name.startswith("get_"), name)

    def test_no_ast_call_in_the_workspace_names_a_broker_tool(self):
        """Belt and braces on the string scan: no call node under workspace/."""
        forbidden = set(FORBIDDEN_TOOL_NAMES + EQUITY_WRITE_TOOL_NAMES)
        offenders = []
        for path in sorted((REPO_ROOT / "workspace").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    target = node.func
                    name = getattr(target, "attr", None) or getattr(target, "id", None)
                    if name in forbidden:
                        offenders.append(f"{path.name}: {name}")
                if isinstance(node, ast.Constant) and node.value in forbidden:
                    offenders.append(f"{path.name}: literal {node.value!r}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
