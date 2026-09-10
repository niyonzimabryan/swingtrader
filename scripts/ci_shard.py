"""Deterministic, load-balanced assignment of test modules to CI shards.

CI needs to split ``tests/test_*.py`` across N parallel jobs without a manifest
that silently goes stale: a module added after the manifest was last generated
must still run *somewhere*, not be skipped because nobody remembered to list it.

The assignment is a greedy longest-processing-time bin pack: modules are
weighted by measured wall-clock seconds from ``scripts/.timing_weights.json``
(produced by ``scripts/measure_test_timing.py``), sorted by weight descending
so the biggest jobs are placed first, and each one goes to whichever shard
currently has the smallest total. A module with no recorded weight (new, or
the weights file is absent) gets the median weight of the modules that do have
one, so it lands in a normal-sized shard rather than always the first one.

Ties break on module name, so the assignment is stable across runs given the
same weights file and the same set of modules -- re-running this script twice
produces identical output, which is what "deterministic" means here.

Usage::

    python -m scripts.ci_shard --shard-index 0 --shard-count 4
    python -m scripts.ci_shard --shard-index 0 --shard-count 4 --weights scripts/.timing_sqlite.json

Prints one dotted module name (``tests.test_foo``) per line: exactly the
modules assigned to that shard. The CI step feeds this into
``python -m unittest`` as explicit module arguments, which collects tests from
each named module the same way ``unittest discover`` would -- a module with no
``unittest.TestCase`` subclasses (a pytest-style file, say) legitimately
contributes zero tests either way.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
DEFAULT_WEIGHTS = REPO_ROOT / "scripts" / "test_shard_weights.json"


def discover_modules() -> list[str]:
    return sorted(p.stem for p in TESTS_DIR.glob("test_*.py"))


def load_weights(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    # Accept either a bare {module: seconds} map or measure_test_timing.py's
    # {"results": [{"module": ..., "seconds": ...}, ...]} shape.
    if "results" in data:
        return {r["module"]: float(r["seconds"]) for r in data["results"]}
    return {k: float(v) for k, v in data.items()}


def assign_shards(modules: list[str], weights: dict[str, float], shard_count: int) -> list[list[str]]:
    known = [weights[m] for m in modules if m in weights and weights[m] > 0]
    default_weight = statistics.median(known) if known else 1.0

    ordered = sorted(
        modules,
        key=lambda m: (-weights.get(m, default_weight), m),
    )

    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for module in ordered:
        target = min(range(shard_count), key=lambda i: (totals[i], i))
        shards[target].append(module)
        totals[target] += weights.get(module, default_weight)

    for shard in shards:
        shard.sort()
    return shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--show-totals", action="store_true",
        help="print each shard's module count and estimated weight to stderr",
    )
    args = parser.parse_args()

    if not (0 <= args.shard_index < args.shard_count):
        parser.error("--shard-index must be in [0, --shard-count)")

    modules = discover_modules()
    weights = load_weights(args.weights)
    shards = assign_shards(modules, weights, args.shard_count)

    if args.show_totals:
        import sys

        default_weight = statistics.median(
            [w for m, w in weights.items() if m in modules and w > 0]
        ) if weights else 1.0
        for i, shard in enumerate(shards):
            total = sum(weights.get(m, default_weight) for m in shard)
            print(f"shard {i}: {len(shard)} modules, ~{total:.1f}s", file=sys.stderr)

    for module in shards[args.shard_index]:
        print(f"tests.{module}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
