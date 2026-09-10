"""Spec Q §5: `strategy_lab/` cannot reach a broker, a session, or a model.

"No strategy module may import a broker, database session, Telegram client, or
LLM client. It receives an immutable snapshot and returns a decision." That is a
structural claim, so it is tested structurally — the same shape as
`tests/test_no_execute_scope.py` and `tests/test_comparables_import_graph.py`.

Two boundaries, not one:

* the **package** may reach `strategy_lab`, `utils`, and — from `registry.py`
  alone — `database`. Nothing else, first-party or third-party;
* `domain.py` may reach **nothing** first-party at all. It is the module every
  Phase 2 strategy will import, and a strategy that cannot reach a session
  cannot be tempted to open one.

`config` is on the forbidden list deliberately. Spec Q §12 invariant 11 and
shared rule 13 require execution mode to be an immutable input carried by the
experiment arm; a module that can read a global setting can infer a mode from
one, and the fastest way to make that impossible is to make the import fail.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "strategy_lab"

#: First-party packages the whole Strategy Lab package may reach.
ALLOWED_FIRST_PARTY = {"strategy_lab", "utils"}

#: The one module allowed to reach `backtest`. Phase 3's replay adapter must
#: reuse `backtest/simulator.py` rather than reinterpret its fill semantics
#: (Spec Q §15 PR 3), and confining the import to one file is what makes "there
#: is one simulator, called from one place" checkable rather than asserted.
SIMULATOR_ALLOWED = {"replay.py"}

#: The two modules allowed a database session. `registry.py` is the service
#: boundary for every experiment-table write; `snapshot_builder.py` is Phase 2's
#: deliberate pure/impure split — `snapshots.py` states the point-in-time rules
#: and holds no session, the builder holds the session and hands it normalized
#: value objects (Spec Q §6, PR 2 requirement 1).
SESSION_ALLOWED = {"registry.py", "snapshot_builder.py"}

#: The pure modules: Phase 2's SDK plus Phase 3's `metrics.py`. Every one of
#: them is pure — a strategy receives an immutable snapshot and returns
#: decisions, a measurement receives observations and returns numbers, and a
#: helper either reaches must be equally unable to open a session. Named
#: explicitly, on top of the whole-package sweeps below, so that deleting one
#: from the package is a visible test failure rather than a silently narrower
#: guarantee.
PURE_SDK_MODULES = {
    "execution_policy.py",
    "indicators.py",
    "metrics.py",
    "snapshots.py",
    "universe.py",
    "validation.py",
}

#: Phase 3's modules that take a session as an argument but must never import
#: one. Every write they make goes through `registry.py`, which stays the only
#: module in the package holding `database`. Naming them here means adding a
#: Phase 3 module that opens its own session is a visible test failure.
SESSION_TAKING_BUT_NOT_HOLDING = {"runner.py", "shadow.py"}

#: Pure stdlib, and staying that way: this is what a strategy imports.
NO_FIRST_PARTY_AT_ALL = {"domain.py"}

#: Every first-party package the Strategy Lab must not reach. `config` is here
#: for the reason in the module docstring. `backtest` is neither forbidden nor
#: globally allowed: Phase 3 widened the allowlist for exactly one module, named
#: in `SIMULATOR_ALLOWED` above.
FORBIDDEN_FIRST_PARTY = {
    "agents", "bot", "comparables", "config", "data", "evals", "execution",
    "filings", "main", "memo", "orchestrator", "portfolio", "research",
    "research_workspace", "scanning", "scoring", "screening", "scripts",
    "tools", "tracking", "workspace",
}

#: Brokers, model clients, messaging, and network transports, by import name.
FORBIDDEN_THIRD_PARTY = {
    "aiohttp", "alpaca", "alpaca_trade_api", "anthropic", "cohere", "finnhub",
    "firecrawl", "fredapi", "google", "httpx", "langfuse", "mcp", "openai",
    "requests", "telegram", "urllib3", "yfinance",
}


def _module_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _strategy_files() -> list[Path]:
    return sorted(
        p for p in (PACKAGE / "strategies").rglob("*.py") if "__pycache__" not in p.parts
    )


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: stays inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _first_party_roots() -> set[str]:
    return {
        entry
        for entry in os.listdir(REPO_ROOT)
        if (REPO_ROOT / entry / "__init__.py").exists() or (
            (REPO_ROOT / entry).is_dir() and not entry.startswith(".")
            and any((REPO_ROOT / entry).glob("*.py"))
        )
    }


class StrategyLabImportGraphTests(unittest.TestCase):
    def test_the_package_reaches_no_broker_session_or_model_client(self):
        offenders = []
        for path in _module_files():
            for root in sorted(_imported_roots(path)):
                if root in FORBIDDEN_FIRST_PARTY or root in FORBIDDEN_THIRD_PARTY:
                    offenders.append(f"{path.name} imports {root}")
        self.assertEqual(
            offenders,
            [],
            "strategy_lab/ must not import a broker, the bot, the orchestrator, "
            "a model client, or config — Spec Q §5, §12 invariant 11.",
        )

    def test_the_only_first_party_dependencies_are_the_allowlisted_ones(self):
        first_party = _first_party_roots()
        offenders = {}
        for path in _module_files():
            reached = _imported_roots(path) & first_party
            allowed = set(ALLOWED_FIRST_PARTY)
            if path.name in SESSION_ALLOWED:
                allowed.add("database")
            if path.name in SIMULATOR_ALLOWED:
                allowed.add("backtest")
            unexpected = sorted(reached - allowed)
            if unexpected:
                offenders[path.name] = unexpected
        self.assertEqual(
            offenders,
            {},
            "widening the Strategy Lab's first-party imports is a deliberate "
            "act; add the package to ALLOWED_FIRST_PARTY with a reason.",
        )

    def test_only_the_registry_can_reach_a_database_session(self):
        offenders = []
        for path in _module_files():
            if path.name in SESSION_ALLOWED:
                continue
            reached = _imported_roots(path)
            if "database" in reached or "sqlalchemy" in reached:
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "registry.py is the service boundary; no other Strategy Lab module "
            "may hold a database session (Spec Q §5, §6).",
        )

    def test_only_the_replay_adapter_reaches_the_simulator(self):
        """Spec Q §15 PR 3: one simulator, and one caller of it."""
        present = {p.name for p in _module_files()}
        self.assertEqual(
            sorted(SIMULATOR_ALLOWED - present), [],
            "the module allowed to import backtest has left the package",
        )
        offenders = sorted(
            path.name
            for path in _module_files()
            if path.name not in SIMULATOR_ALLOWED and "backtest" in _imported_roots(path)
        )
        self.assertEqual(
            offenders,
            [],
            "backtest/simulator.py is reached from strategy_lab/replay.py alone; "
            "a second caller is a second place for fill semantics to drift "
            "(Spec Q §15 PR 3).",
        )

    def test_the_phase_three_modules_take_a_session_but_never_import_one(self):
        """runner.py and shadow.py delegate every write to the registry."""
        present = {p.name for p in _module_files()}
        self.assertEqual(
            sorted(SESSION_TAKING_BUT_NOT_HOLDING - present), [],
            "a Phase 3 module named here has disappeared from the package",
        )
        offenders = []
        for path in _module_files():
            if path.name not in SESSION_TAKING_BUT_NOT_HOLDING:
                continue
            for root in sorted(_imported_roots(path) & {"database", "sqlalchemy"}):
                offenders.append(f"{path.name} imports {root}")
        self.assertEqual(
            offenders,
            [],
            "the runner and the shadow executor receive a session and hand "
            "every write to registry.py; importing database here would make a "
            "second service boundary (Spec Q §5, §6).",
        )

    def test_the_pure_sdk_and_every_strategy_hold_no_session(self):
        """Spec Q §5, named module by module rather than only by sweep."""
        present = {p.name for p in _module_files()}
        missing = sorted(PURE_SDK_MODULES - present)
        self.assertEqual(
            missing, [], "a pure SDK module named here has disappeared from the package"
        )
        strategies = _strategy_files()
        self.assertGreaterEqual(
            len(strategies), 5, "the V1 roster is four strategies plus its __init__"
        )
        offenders = []
        for path in sorted(
            [p for p in _module_files() if p.name in PURE_SDK_MODULES] + strategies
        ):
            reached = _imported_roots(path)
            for root in sorted(reached & ({"database", "sqlalchemy"} | FORBIDDEN_FIRST_PARTY | FORBIDDEN_THIRD_PARTY)):
                offenders.append(f"{path.name} imports {root}")
        self.assertEqual(
            offenders,
            [],
            "the Phase 2 SDK and every strategy stay pure: no session, no "
            "broker, no Telegram, no model client, no config (Spec Q §5, §6).",
        )

    def test_every_strategy_reaches_only_the_strategy_lab(self):
        first_party = _first_party_roots()
        offenders = {}
        for path in _strategy_files():
            unexpected = sorted((_imported_roots(path) & first_party) - {"strategy_lab"})
            if unexpected:
                offenders[path.name] = unexpected
        self.assertEqual(
            offenders,
            {},
            "a strategy module imports its snapshot, its indicators and its "
            "execution policy from strategy_lab and nothing else.",
        )

    def test_domain_imports_nothing_first_party(self):
        first_party = _first_party_roots()
        for name in NO_FIRST_PARTY_AT_ALL:
            with self.subTest(name):
                reached = sorted(_imported_roots(PACKAGE / name) & first_party)
                self.assertEqual(
                    reached,
                    [],
                    f"{name} is what every strategy imports; it stays stdlib-only.",
                )

    def test_importing_the_domain_pulls_in_no_runtime_dependency(self):
        """A fresh interpreter, because the rest of the suite imports these."""
        program = (
            "import strategy_lab.domain, sys;"
            "bad=sorted({m.split('.')[0] for m in sys.modules} & "
            "{'sqlalchemy','anthropic','openai','google','langfuse','firecrawl',"
            "'finnhub','alpaca','telegram','yfinance','httpx','aiohttp','mcp'});"
            "print(','.join(bad))"
        )
        out = subprocess.run(
            [sys.executable, "-c", program], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            out.stdout.strip(), "",
            "importing strategy_lab.domain pulled in a runtime dependency",
        )

    def test_the_guard_would_catch_a_real_violation(self):
        """A negative control: the checker sees an import when there is one."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write("from execution.order_manager import OrderManager\nimport anthropic\n")
            path = Path(handle.name)
        self.addCleanup(path.unlink)
        roots = _imported_roots(path)
        self.assertIn("execution", roots & FORBIDDEN_FIRST_PARTY)
        self.assertIn("anthropic", roots & FORBIDDEN_THIRD_PARTY)

    def test_no_strategy_lab_module_names_an_order_placement_call(self):
        """Belt and braces: the words, not only the imports."""
        forbidden = ("place_order", "submit_order", "place_equity_order", "create_order")
        offenders = [
            f"{path.name}: {needle}"
            for path in _module_files()
            for needle in forbidden
            if needle in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
