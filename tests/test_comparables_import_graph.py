"""`comparables/` may not import the bot runtime (Spec N, Phase 3b scope).

The engine is pure computation over in-memory dataclasses. It reuses
`backtest/simulator.py` for exit semantics and nothing else from the
application; in particular no model client is importable from it, which is what
makes "a model may never produce a statistic" (Spec N §9) a structural property
rather than a promise.
"""

from __future__ import annotations

import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "comparables")

#: First-party packages the comparables engine must never reach.
FORBIDDEN_ROOTS = {
    "bot", "orchestrator", "agents", "execution", "tools", "scanning",
    "screening", "scoring", "tracking", "memo", "database", "data", "evals",
    "config", "utils", "scripts", "main",
}
#: Model clients, by whatever name they are imported under.
FORBIDDEN_MODULES = {
    "anthropic", "openai", "google", "google.genai", "langfuse", "firecrawl",
    "finnhub", "alpaca", "yfinance", "telegram", "mlfinlab",
}
ALLOWED_FIRST_PARTY = {"comparables", "backtest"}


def _module_files(package_dir: str) -> list[str]:
    return sorted(
        os.path.join(package_dir, name)
        for name in os.listdir(package_dir)
        if name.endswith(".py")
    )


def _imported_roots(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # a relative import stays inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


class ComparablesImportGraphTests(unittest.TestCase):
    def test_comparables_imports_no_runtime(self):
        offenders: list[str] = []
        first_party: set[str] = set()
        for path in _module_files(PACKAGE):
            for root in _imported_roots(path):
                if root in FORBIDDEN_ROOTS or root in FORBIDDEN_MODULES:
                    offenders.append(f"{os.path.basename(path)} imports {root}")
                if os.path.isdir(os.path.join(REPO_ROOT, root)):
                    first_party.add(root)
        self.assertEqual(offenders, [], "comparables/ must not import the runtime")
        self.assertTrue(
            first_party <= ALLOWED_FIRST_PARTY,
            f"comparables/ reaches unexpected first-party packages: "
            f"{sorted(first_party - ALLOWED_FIRST_PARTY)}",
        )

    def test_the_one_first_party_dependency_is_also_clean(self):
        """`backtest/simulator.py` is the only application module we reuse."""
        path = os.path.join(REPO_ROOT, "backtest", "simulator.py")
        for root in _imported_roots(path):
            self.assertNotIn(root, FORBIDDEN_ROOTS)
            self.assertNotIn(root, FORBIDDEN_MODULES)

    def test_no_model_client_reachable(self):
        """A fresh interpreter that imports the whole package pulls in no model
        client. Run in a subprocess because the rest of the suite imports them
        for its own reasons, and `sys.modules` is shared."""
        import subprocess
        import sys

        program = (
            "import comparables.balance, comparables.config, comparables.fixtures, "
            "comparables.inference, comparables.outcomes, comparables.report, "
            "comparables.setup_spec, sys;"
            "bad=sorted({m.split('.')[0] for m in sys.modules} & "
            "{'anthropic','openai','google','langfuse','firecrawl','finnhub',"
            "'alpaca','telegram','yfinance','mlfinlab','sqlalchemy'});"
            "print(','.join(bad))"
        )
        out = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "",
                         "importing comparables/ pulled in a runtime dependency")


if __name__ == "__main__":
    unittest.main()
