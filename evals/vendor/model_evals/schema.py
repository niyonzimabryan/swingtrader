"""Replay-record schema — the one shape both apps write and the lib reads.

A ReplayRecord freezes a single stage invocation: the exact input, the incumbent
model's structured output, and any cheap labels (e.g. "this item was an eventual
winner"). Candidate models are replayed on `input` and scored against
`incumbent_output` / `labels`. Stored as JSONL (one record per line).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class ReplayRecord:
    task: str                       # e.g. "top5.stage1"
    item_id: str                    # stable id within the corpus
    input: dict                     # self-contained stage input (for replay)
    incumbent_model: str            # id that produced incumbent_output
    incumbent_output: dict          # structured stage output
    labels: dict = field(default_factory=dict)   # cheap refs: {"winner": true}, {"pnl_pct": 0.04}, ...
    incumbent_usage: dict | None = None           # {"in": tok, "out": tok} if known (exact cost)
    run_id: str = ""
    ts: str = ""                    # ISO8601; caller stamps (kept out of the lib so it stays deterministic)
    context_hash: str = ""          # ties record to upstream state (cross-stage checks)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayRecord":
        known = {f: d.get(f) for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in known.items() if v is not None or k in ("labels",)})


def load_jsonl(path: str) -> list[ReplayRecord]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(ReplayRecord.from_dict(json.loads(line)))
    return out


def dump_jsonl(records: list[ReplayRecord], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r.to_json() + "\n")
