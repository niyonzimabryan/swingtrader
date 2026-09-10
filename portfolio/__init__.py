"""The portfolio ledger — Spec L.

Deliberately **free of any import of ``execution``, ``bot``, ``orchestrator``
or ``agents``**. The workspace service reads this package, and Spec L §6's
first invariant is that no agent-reachable code path can reach a broker
placement method. Keeping the dependency arrow one-way — ``execution`` imports
``portfolio``, never the reverse — is what makes that assertion cheap to keep
true, and ``tests/test_portfolio_import_graph.py`` fails if it is ever reversed.

The sync job therefore takes its broker as an argument (any object with
``fetch_ledger_snapshot()``); the wiring that constructs a real one lives in
``scripts/portfolio_sync.py``, which the workspace does not import.

This module stays empty on purpose: a re-export here would drag whatever it
names into the import closure of everything that touches the package.
"""
