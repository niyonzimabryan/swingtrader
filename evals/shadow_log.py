"""Shadow-logging — turn scoring invocations into replay-records so the corpus
accumulates forward (swingtrader is not offline-backfillable; design §3).

In prod, call `record_from_scoring(...)` right after the scoring engine runs and
append the record to a Langfuse dataset (materialize_langfuse). The eval later
replays candidates over these frozen inputs. Until the corpus clears N_min the
scoring eval is UNDERPOWERED — this module exists to make that wait shrink.
"""
from __future__ import annotations

from evals import _bootstrap  # noqa: F401
from model_evals.schema import ReplayRecord

# recommendation → binary act/skip (the buy/skip parity proxy)
_ACT = {"proceed", "reduce_size"}


def decision_of(scoring_result: dict) -> str:
    rec = ((scoring_result.get("opus_evaluation") or {}).get("recommendation") or "").lower()
    return "act" if rec in _ACT else "skip"


def record_from_scoring(ticker: str, scoring_input: dict, scoring_result: dict,
                        incumbent_model: str, usage: dict | None = None) -> ReplayRecord:
    """Freeze one scoring call. `scoring_input` must be self-contained enough to
    replay a candidate (the agent outputs + regime + portfolio context the engine saw)."""
    ev = scoring_result.get("opus_evaluation") or {}
    return ReplayRecord(
        task="swingtrader.scoring", item_id=f"{ticker}:{scoring_input.get('as_of', '')}",
        input=scoring_input, incumbent_model=incumbent_model,
        incumbent_output={
            "decision": decision_of(scoring_result),
            "recommendation": ev.get("recommendation"),
            "conviction": ev.get("conviction"),
            "final_score": scoring_result.get("final_score"),
        },
        incumbent_usage=usage,
    )


def materialize_langfuse(records, dataset_name: str = "swingtrader-scoring"):
    """Append records to a Langfuse dataset (live; lazy import). Don't invent a
    parallel store — reuse the Langfuse the app already has wired."""
    from langfuse import get_client  # lazy
    client = get_client()
    for r in records:
        client.create_dataset_item(
            dataset_name=dataset_name, id=r.item_id,
            input=r.input, expected_output=r.incumbent_output,
            metadata={"incumbent_model": r.incumbent_model, "task": r.task},
        )
    return len(records)
