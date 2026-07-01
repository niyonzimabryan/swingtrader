"""TaskSpec — a declarative, per-task eval definition.

The lib stays app-agnostic: an app's evals/tasks.py builds TaskSpecs, wiring the
scorer primitives to that app's structured outputs. `score_item` is where the app
knows what "agreement" means for its stage; everything downstream (bootstrap,
verdict, report) is generic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .schema import ReplayRecord

# score_item(record, candidate_output) -> per-item score, or None to exclude the item.
#   PARITY    : score in [0,1] (agreement / recall / overlap / field-match / per-night ρ)
#   DISCOVERY : signed per-item delta (judge_delta in {-1,0,1}, or outcome_delta)
ScoreItem = Callable[[ReplayRecord, dict], Optional[float]]


@dataclass
class TaskSpec:
    name: str                       # "top5.stage1"
    mode: str                       # "parity" | "discovery"
    primary_metric: str             # human label, e.g. "winner_recall"
    threshold: float                # parity: min metric floor; discovery: min effect size (>0)
    score_item: ScoreItem
    incumbent_model: str            # what production currently runs
    min_n: int = 100
    higher_is_better: bool = True   # discovery direction

    def __post_init__(self):
        if self.mode not in ("parity", "discovery"):
            raise ValueError(f"mode must be parity|discovery, got {self.mode!r}")
