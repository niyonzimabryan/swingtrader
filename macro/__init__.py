"""Vintage-correct macro — Spec O section 4.

``data/macro_data.py`` reads *current* FRED values, and current values are
*revised* values. Using today's GDP print to characterise a 2024 event is
lookahead, and it silently corrupts every regime label in Spec N.

* ``macro.series``     — the series registry and the never-revised allowlist.
* ``macro.vintage``    — ALFRED vintages, as ``source_observations`` rows.
* ``macro.regime_v1``  — the deterministic, versioned classifier.
* ``macro.api``        — ``macro_state(as_of=...)``.

``macro.regime_v1`` imports nothing but the standard library and
``macro.series``. That is checked (``test_no_llm_in_regime``), because "a model
may narrate the regime; it may not assign it" is only true if it is structural.
"""
