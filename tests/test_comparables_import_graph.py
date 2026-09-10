"""`comparables/` may not import the bot runtime, and the split is by module.

Phase 3b's engine was pure computation over in-memory dataclasses and reached
nothing but `backtest/simulator.py`. Phase 3c has to read stored facts, so the
package now has two halves and the rule is stated per half rather than relaxed
for the whole package:

**The computation core stays exactly as pure as it was.** `setup_spec`,
`outcomes`, `inference`, `report`, `balance`, `config`, `fixtures` and every
module under `setups/` reach nothing outside `comparables` and `backtest`.
That is what keeps "a model may never produce a statistic" (Spec N §9) a
structural property rather than a promise: there is no model client on the path
that produces a number.

**The persistence half reaches stored rows, through named seams and no others.**
`cohort`, `query`, `registry`, `lookahead` and `citations` may reach `data.prices`,
`filings.observations`, `database` and `sqlalchemy` — and nothing else. In
particular they may not reach `filings.sec_minimal` or any Phase 4 plane, which
is `test_sec_minimal_contract_in_phase3` (Spec N §10) in its structural form.

Neither half may import a model client, the bot, the orchestrator, the agents,
or `execution/`. That has not moved.
"""

from __future__ import annotations

import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "comparables")

#: First-party packages the comparables engine must never reach, in either half.
FORBIDDEN_ROOTS = {
    "bot", "orchestrator", "agents", "execution", "tools", "scanning",
    "screening", "scoring", "tracking", "memo", "evals", "scripts", "main",
}
#: Model clients, by whatever name they are imported under.
FORBIDDEN_MODULES = {
    "anthropic", "openai", "google", "google.genai", "langfuse", "firecrawl",
    "finnhub", "alpaca", "yfinance", "telegram", "mlfinlab",
}

#: Modules that compute. Unchanged from Phase 3b, deliberately.
PURE_MODULES = {
    "__init__.py", "balance.py", "config.py", "fixtures.py", "inference.py",
    "outcomes.py", "report.py", "setup_spec.py",
}
PURE_ALLOWED_FIRST_PARTY = {"comparables", "backtest"}

#: Modules that persist. Their extra reach is enumerated, not open-ended.
PERSISTENCE_MODULES = {
    "citations.py", "cohort.py", "lookahead.py", "query.py", "registry.py",
}
PERSISTENCE_ALLOWED_FIRST_PARTY = {
    "comparables", "backtest", "data", "database", "filings",
}

#: The whole of the filings package `comparables/` may address (Spec N §10,
#: `test_sec_minimal_contract_in_phase3`). `filings.sec_minimal` is *not* on
#: this list: reads that need it go through `filings.observations`, which is
#: where the `known_at_utc <= t` rule is enforced.
ALLOWED_FILINGS_MODULES = {"filings.observations"}

#: The `data/` modules the persistence half may address.
ALLOWED_DATA_MODULES = {
    "data.prices", "data.prices.base", "data.prices.derived", "data.prices.store",
}
#: `data.analog_ranker` is a *generator*, never a selector (Spec N §4.5), so it
#: is reached from the caller, never from inside the engine.
FORBIDDEN_DATA_MODULES = {"data.analog_ranker", "data.market_data", "data.event_outcomes"}


def _module_files(package_dir: str) -> list[str]:
    """Every `.py` in the package, including the `setups/` subpackage."""
    out: list[str] = []
    for root, _dirs, names in os.walk(package_dir):
        out.extend(
            os.path.join(root, name) for name in names if name.endswith(".py")
        )
    return sorted(out)


def _relative(path: str) -> str:
    return os.path.relpath(path, PACKAGE).replace(os.sep, "/")


def _imports(path: str) -> tuple[set[str], set[str]]:
    """`(roots, dotted)` — the top-level packages and the full module names."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    roots: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
                dotted.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # a relative import stays inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
                dotted.add(node.module)
    return roots, dotted


class ComparablesImportGraphTests(unittest.TestCase):
    def test_every_module_is_classified(self):
        """A new module has to choose a half. Neither list is a default."""
        present = {
            _relative(p) for p in _module_files(PACKAGE)
            if "/" not in _relative(p)
        }
        unclassified = present - PURE_MODULES - PERSISTENCE_MODULES
        self.assertEqual(
            unclassified, set(),
            "a new comparables/ module must be listed as pure or as persistence; "
            "leaving it out would let it import anything",
        )

    def test_comparables_imports_no_runtime(self):
        offenders: list[str] = []
        for path in _module_files(PACKAGE):
            name = _relative(path)
            roots, _dotted = _imports(path)
            for root in roots:
                if root in FORBIDDEN_ROOTS or root in FORBIDDEN_MODULES:
                    offenders.append(f"{name} imports {root}")
        self.assertEqual(offenders, [], "comparables/ must not import the runtime")

    def test_the_computation_core_stays_pure(self):
        """The half that produces numbers reaches nothing that could supply one."""
        for path in _module_files(PACKAGE):
            name = _relative(path)
            if name not in PURE_MODULES and not name.startswith("setups/"):
                continue
            with self.subTest(module=name):
                roots, _ = _imports(path)
                first_party = {
                    r for r in roots
                    if os.path.isdir(os.path.join(REPO_ROOT, r))
                }
                self.assertTrue(
                    first_party <= PURE_ALLOWED_FIRST_PARTY,
                    f"{name} reaches {sorted(first_party - PURE_ALLOWED_FIRST_PARTY)}; "
                    f"the computation core stays pure",
                )
                self.assertNotIn("sqlalchemy", roots, f"{name} must not reach a database")

    def test_the_persistence_half_reaches_only_the_named_seams(self):
        for path in _module_files(PACKAGE):
            name = _relative(path)
            if name not in PERSISTENCE_MODULES:
                continue
            with self.subTest(module=name):
                roots, dotted = _imports(path)
                first_party = {
                    r for r in roots
                    if os.path.isdir(os.path.join(REPO_ROOT, r))
                }
                self.assertTrue(
                    first_party <= PERSISTENCE_ALLOWED_FIRST_PARTY,
                    f"{name} reaches "
                    f"{sorted(first_party - PERSISTENCE_ALLOWED_FIRST_PARTY)}",
                )
                for module in dotted:
                    if module.startswith("data."):
                        self.assertIn(module, ALLOWED_DATA_MODULES, f"{name}: {module}")
                        self.assertNotIn(module, FORBIDDEN_DATA_MODULES)
                    if module.startswith("filings"):
                        self.assertIn(
                            module, ALLOWED_FILINGS_MODULES,
                            f"{name} imports {module}; comparables/ reaches the "
                            f"filings package only through filings.observations",
                        )

    def test_sec_minimal_contract_in_phase3(self):
        """Spec N §10: the filings package is reached through one module only.

        The §10 row names `filings/sec_minimal.py`. Phase 3a put the
        point-in-time read rule in `filings/observations.py` — `sec_minimal` is
        the *ingest* adapter and its read helpers bypass nothing, but importing
        it from here would put the Phase 4 planes one attribute away. The
        stricter reading is the one that survives Phase 4, so this asserts the
        seam is `filings.observations` and nothing else.
        """
        reached: set[str] = set()
        for path in _module_files(PACKAGE):
            _roots, dotted = _imports(path)
            reached |= {m for m in dotted if m == "filings" or m.startswith("filings.")}
        self.assertEqual(
            reached, ALLOWED_FILINGS_MODULES,
            f"comparables/ reaches {sorted(reached)} in the filings package; "
            f"the sanctioned seam is {sorted(ALLOWED_FILINGS_MODULES)}",
        )

    def test_the_one_first_party_dependency_is_also_clean(self):
        """`backtest/simulator.py` is the only application module we reuse."""
        path = os.path.join(REPO_ROOT, "backtest", "simulator.py")
        roots, _ = _imports(path)
        for root in roots:
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
            "comparables.setup_spec, comparables.setups, comparables.cohort, "
            "comparables.registry, comparables.lookahead, comparables.citations, "
            "comparables.query, "
            "sys;"
            "bad=sorted({m.split('.')[0] for m in sys.modules} & "
            "{'anthropic','openai','google','langfuse','firecrawl','finnhub',"
            "'alpaca','telegram','yfinance','mlfinlab'});"
            "print(','.join(bad))"
        )
        out = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "",
                         "importing comparables/ pulled in a runtime dependency")

    def test_the_computation_core_alone_pulls_in_no_database(self):
        """Importing only the pure half must not drag SQLAlchemy in with it.

        This is the property the split exists to keep: the modules that produce
        statistics remain runnable, and testable, with no database anywhere.
        """
        import subprocess
        import sys

        program = (
            "import comparables.balance, comparables.config, comparables.fixtures, "
            "comparables.inference, comparables.outcomes, comparables.report, "
            "comparables.setup_spec, comparables.setups, sys;"
            "print('sqlalchemy' if 'sqlalchemy' in sys.modules else '')"
        )
        out = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "",
                         "the comparables computation core reached a database")


if __name__ == "__main__":
    unittest.main()
