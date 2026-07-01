"""swingtrader task definitions.

scoring: does Sonnet agree with Opus on act/skip? (parity; the router question's
first half). filter: does a cheaper filter keep every eventually-traded ticker?
Both are data-blocked until the shadow-logged corpus clears N_min (design §3) —
the harness stamps UNDERPOWERED below the floor rather than shipping a thin verdict.
"""
from __future__ import annotations

from evals import _bootstrap  # noqa: F401
from model_evals import scorers as S
from model_evals.spec import TaskSpec

# Production defaults live in config/settings.py (code defaults, no *_MODEL env in prod).
_DEFAULTS = {"scoring": "claude-opus-4-6", "filter": "claude-haiku-4-5"}


def _incumbent(task: str) -> str:
    try:
        from config.settings import Settings  # app's source of truth, if importable
        s = Settings()
        return {"scoring": s.scoring_model, "filter": s.filter_model}[task]
    except Exception:
        return _DEFAULTS[task]


def scoring_spec(incumbent_model: str | None = None) -> TaskSpec:
    inc = incumbent_model or _incumbent("scoring")

    def score(record, cand):
        return S.agree(record.incumbent_output.get("decision"), (cand or {}).get("decision"))

    return TaskSpec(
        name="swingtrader.scoring", mode="parity", primary_metric="act_skip_agreement",
        threshold=0.90, incumbent_model=inc, min_n=150, score_item=score,
    )


def filter_spec(incumbent_model: str | None = None) -> TaskSpec:
    inc = incumbent_model or _incumbent("filter")

    def score(record, cand):
        if not record.labels.get("traded"):
            return None                         # recall over eventually-traded tickers only
        return 1.0 if (cand or {}).get("decision") in ("pass", "act") else 0.0

    return TaskSpec(
        name="swingtrader.filter", mode="parity", primary_metric="traded_ticker_recall",
        threshold=0.98, incumbent_model=inc, min_n=100, score_item=score,
    )
