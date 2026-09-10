"""Strategy Lab — versioned strategies, experiments, and their evidence (Spec Q).

The package is deliberately import-light. ``domain`` is pure stdlib; only
``registry`` and ``snapshot_builder`` may open a database session — the latter
is the impure half of the Phase 2 pure/impure split, and the point-in-time
rules it feeds live in ``snapshots``, which cannot reach a session at all. Nothing here imports a broker, the
Telegram client, an LLM client, or ``config`` — execution mode is carried by an
immutable experiment arm and is never inferred from a mutable global setting
(Spec Q §11, §12 invariant 11). ``tests/test_strategy_lab_import_graph.py``
enforces that statically.
"""
