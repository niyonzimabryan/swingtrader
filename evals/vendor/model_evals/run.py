"""run — the harness. Records + a candidate → a scored, costed, decided Result.

evaluate() is app-agnostic: it takes replay records, a TaskSpec, and a way to get
the candidate's output per record (offline lookup or a live replay callable). It
scores each item, bootstraps a CI, computes $/run for both models from registry
prices, and asks decide() for a verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Callable, Optional

from . import decide as _decide
from .catalog import Catalog, estimate_tokens
from .schema import ReplayRecord
from .spec import TaskSpec
from .stats import bootstrap_ci


@dataclass
class Result:
    task: str
    mode: str
    candidate_model: str
    incumbent_model: str
    primary_metric: str
    value: float
    ci_low: float
    ci_high: float
    n: int
    n_excluded: int
    cost_candidate: Optional[float]
    cost_incumbent: Optional[float]
    cost_estimated: bool
    free_upgrade: bool
    verdict: str
    reason: str
    threshold: float


def _run_cost(catalog, model, in_tok, out_tok):
    try:
        return catalog.cost_per_run(model, in_tok, out_tok)
    except Exception:
        return None


def evaluate(
    records: list[ReplayRecord],
    spec: TaskSpec,
    candidate_model: str,
    candidate_output_for: Callable[[ReplayRecord], dict],
    catalog: Catalog,
    seed: int = 0,
) -> Result:
    """Score `candidate_model` against the incumbent over `records` for `spec`.

    candidate_output_for(record) returns the candidate's structured output for that
    record (offline: a precomputed lookup; live: a replay call). A candidate output
    may carry an optional "_usage": {"in","out"} for exact cost; otherwise cost is
    estimated from serialized length.
    """
    catalog.require_eligible(candidate_model)   # refuse a swap onto a rotten id

    scores = []
    # exact token usage (when logged) and estimated tokens (always), kept separate so
    # cost is only ever compared on a *consistent* basis for both models (never
    # exact-candidate vs estimated-incumbent, which would bias the $/run comparison).
    ex_cand_in, ex_cand_out, ex_inc_in, ex_inc_out = [], [], [], []
    es_in, es_cand_out, es_inc_out = [], [], []
    cand_exact_all = inc_exact_all = True
    excluded = 0
    for r in records:
        cand = candidate_output_for(r)
        s = spec.score_item(r, cand)
        if s is None:
            excluded += 1
            continue
        scores.append(float(s))

        cu = (cand or {}).get("_usage")
        if cu:
            ex_cand_in.append(cu.get("in", 0)); ex_cand_out.append(cu.get("out", 0))
        else:
            cand_exact_all = False
        if r.incumbent_usage:
            ex_inc_in.append(r.incumbent_usage.get("in", 0)); ex_inc_out.append(r.incumbent_usage.get("out", 0))
        else:
            inc_exact_all = False
        # estimates (shared input basis; strip candidate's _usage before sizing output)
        cand_body = {k: v for k, v in (cand or {}).items() if k != "_usage"}
        es_in.append(estimate_tokens(r.input))
        es_cand_out.append(estimate_tokens(cand_body))
        es_inc_out.append(estimate_tokens(r.incumbent_output))

    n = len(scores)
    value, lo, hi = bootstrap_ci(scores, seed=seed)

    # Use exact usage only if BOTH models have it for every item; else estimate both.
    if n and cand_exact_all and inc_exact_all:
        estimated = False
        cost_cand = _run_cost(catalog, candidate_model, median(ex_cand_in), median(ex_cand_out))
        cost_inc = _run_cost(catalog, spec.incumbent_model, median(ex_inc_in), median(ex_inc_out))
    elif n:
        estimated = True
        cost_cand = _run_cost(catalog, candidate_model, median(es_in), median(es_cand_out))
        cost_inc = _run_cost(catalog, spec.incumbent_model, median(es_in), median(es_inc_out))
    else:
        estimated = True
        cost_cand = cost_inc = None

    # free same-price upgrade? (equal in & out price → cost tie allowed to promote)
    free_upgrade = False
    try:
        pc, pi = catalog.price(candidate_model), catalog.price(spec.incumbent_model)
        free_upgrade = (pc["in"] == pi["in"] and pc["out"] == pi["out"]
                        and _normalise_ne(catalog, candidate_model, spec.incumbent_model))
    except Exception:
        pass

    verdict, reason = _decide.decide(spec, value, lo, hi, n, cost_cand, cost_inc, free_upgrade)
    return Result(
        task=spec.name, mode=spec.mode, candidate_model=candidate_model,
        incumbent_model=spec.incumbent_model, primary_metric=spec.primary_metric,
        value=value, ci_low=lo, ci_high=hi, n=n, n_excluded=excluded,
        cost_candidate=cost_cand, cost_incumbent=cost_inc, cost_estimated=estimated,
        free_upgrade=free_upgrade, verdict=verdict, reason=reason, threshold=spec.threshold,
    )


def _normalise_ne(catalog, a, b) -> bool:
    from .catalog import normalise
    return normalise(a) != normalise(b)
