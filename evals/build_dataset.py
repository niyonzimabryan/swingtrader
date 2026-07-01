"""Build the swingtrader eval corpus.

Sources: (1) `from_traces` — pull scoring calls already captured by OTEL as
Langfuse traces (no prod change; this is the live path); (2) `from_jsonl` — a
local snapshot; (3) `from_langfuse` — a curated dataset if one is created.
Forward-accumulate only — no backfill (a scoring input is market-state at a moment).
"""
from __future__ import annotations

import json

from evals import _bootstrap  # noqa: F401
from model_evals.schema import ReplayRecord

_ACT = {"proceed", "reduce_size"}


def from_jsonl(path: str) -> list[ReplayRecord]:
    from model_evals.schema import load_jsonl
    return load_jsonl(path)


def _parse_opus_json(output) -> dict | None:
    """Pull the opus_evaluation JSON out of a captured assistant message.
    Output shape (Langfuse/OTEL): [{"role":"assistant","parts":[{"type":"text",
    "content":"{...json...}"}, ...]}] — find the part whose content parses as JSON
    carrying a recommendation."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except Exception:
            return None
    msgs = output if isinstance(output, list) else [output]
    for m in msgs:
        if not isinstance(m, dict):
            continue
        parts = m.get("parts")
        chunks = [p.get("content") for p in parts if isinstance(p, dict)] if parts else [m.get("content")]
        for c in chunks:
            if not isinstance(c, str) or "recommendation" not in c:
                continue
            d = _loads_loose(c)
            if d is None:
                continue
            ev = d.get("opus_evaluation", d)
            if isinstance(ev, dict) and "recommendation" in ev:
                return ev
    return None


def _loads_loose(c: str):
    """Parse JSON that may be wrapped in a ```json fence or surrounded by prose:
    try raw, then the first {...last } span."""
    try:
        return json.loads(c)
    except Exception:
        pass
    i, j = c.find("{"), c.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(c[i:j + 1])
        except Exception:
            return None
    return None


def from_traces(from_ts: str, incumbent_default: str = "claude-opus-4-6") -> list[ReplayRecord]:
    """Pull the scoring corpus straight from Langfuse traces already captured by
    OTEL (every scoring call is a trace tagged 'scoring'). input = the exact opus
    messages (replayable to a candidate); incumbent_output = the parsed decision.
    Skips traces whose output can't be parsed."""
    from evals import langfuse_api
    recs = []
    for t in langfuse_api.traces_by_tag("scoring", from_ts):
        tags = t.get("tags") or []
        ticker = next((x for x in tags if x not in ("scoring", "scheduled_scan", "test_analyze")), "?")
        try:
            full = langfuse_api.trace(t["id"])
        except Exception:
            continue                # flaky endpoint: skip a trace we can't fetch, don't abort the pull
        for o in full.get("observations", []):
            ev = _parse_opus_json(o.get("output"))
            if not ev or not o.get("input"):
                continue
            rec = (ev.get("recommendation") or "").lower()
            recs.append(ReplayRecord(
                task="swingtrader.scoring",
                item_id=f"{ticker}:{o.get('startTime') or t.get('timestamp') or t['id']}",
                input={"messages": o["input"]},
                incumbent_model=o.get("model") or incumbent_default,
                incumbent_output={"decision": "act" if rec in _ACT else "skip",
                                  "recommendation": ev.get("recommendation"),
                                  "conviction": ev.get("conviction"),
                                  "final_score": ev.get("final_score")},
                labels={"ticker": ticker},
            ))
            break   # one scoring generation per trace
    return recs


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
