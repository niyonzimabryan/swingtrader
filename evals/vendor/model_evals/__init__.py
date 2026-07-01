"""model_evals — Problem B: the LLM model-evaluation suite.

Thin, provider-agnostic library that answers "is a cheaper/different model good
enough (or better) for this task?" and turns a passing answer into a config change.

Consumes the Problem-A registry (models.json) as its model catalog + price source.
Core is stdlib-only so it vendors into each app the same way check_models.py does;
live model calls (providers.py) import the Anthropic/Gemini SDKs lazily.

Public surface:
    catalog.Catalog          — models.json loader, price → $/run, rot checks
    schema.ReplayRecord      — one (input, incumbent_output) replay item
    spec.TaskSpec            — declarative per-task eval definition
    scorers                  — golden_overlap, agreement, recall, field_match, rank_corr, ...
    stats.bootstrap_ci       — pure-python percentile bootstrap
    decide.decide            — PROMOTE / HOLD / REJECT / UNDERPOWERED
    report.render            — cost/quality markdown table (self-dating)
    run.evaluate             — the harness: records + candidate → Result
"""

__version__ = "0.1.0"
