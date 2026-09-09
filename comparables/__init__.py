"""Comparable-setups engine (Spec N) — the pure-computation core.

This package is deliberately free of the bot runtime, of any model client, and
(for now) of the database. Every entry point takes in-memory dataclasses and
returns dataclasses; persistence and point-in-time cohort construction from
stored facts land in a later phase.

`tests/test_comparables_import_graph.py` asserts the isolation.
"""
