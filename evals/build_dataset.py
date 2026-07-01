"""Build the swingtrader eval corpus.

Two sources: (1) a local JSONL of shadow-logged records (evals/corpus/*.jsonl),
(2) live from a Langfuse dataset. Forward-accumulate only — there is no backfill
(a scoring input is market-state at a moment and can't be reconstructed).
"""
from __future__ import annotations

from evals import _bootstrap  # noqa: F401
from model_evals.schema import ReplayRecord, load_jsonl


def from_jsonl(path: str) -> list[ReplayRecord]:
    return load_jsonl(path)


def from_langfuse(dataset_name: str = "swingtrader-scoring") -> list[ReplayRecord]:
    """Live: pull a Langfuse dataset into ReplayRecords (lazy import)."""
    from langfuse import get_client  # lazy
    client = get_client()
    ds = client.get_dataset(dataset_name)
    recs = []
    for item in ds.items:
        exp = item.expected_output or {}
        recs.append(ReplayRecord(
            task="swingtrader.scoring", item_id=str(item.id), input=item.input or {},
            incumbent_model=(item.metadata or {}).get("incumbent_model", "claude-opus-4-6"),
            incumbent_output=exp,
        ))
    return recs
