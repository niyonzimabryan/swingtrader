"""Per-module timing for the test suite, used to plan CI sharding.

Runs each ``tests/test_*.py`` module in its own ``unittest`` process (so one
slow or hanging module cannot skew another's wall-clock time) and reports
elapsed seconds, test count, and pass/fail status, sorted slowest first.

Usage::

    python -m scripts.measure_test_timing                      # SQLite
    TEST_DATABASE_URL=postgresql+psycopg://... \\
        python -m scripts.measure_test_timing                  # Postgres
    python -m scripts.measure_test_timing --out timing.json

Output is also written as JSON (default ``scripts/.timing_<engine>.json``) so
it can be diffed across runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"


def discover_modules() -> list[str]:
    return sorted(p.stem for p in TESTS_DIR.glob("test_*.py"))


def run_module(module: str, env: dict) -> dict:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", f"tests.{module}", "-v"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - start
    # unittest -v prints one "... ok"/"... FAIL"/"... skipped" line per test to
    # stderr, plus a trailing "Ran N tests in Xs" summary line.
    n_tests = 0
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Ran ") and " test" in line:
            try:
                n_tests = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return {
        "module": module,
        "seconds": round(elapsed, 3),
        "tests": n_tests,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--modules", nargs="*", default=None)
    args = parser.parse_args()

    env = os.environ.copy()
    backend = "postgres" if env.get("TEST_DATABASE_URL") else "sqlite"
    modules = args.modules or discover_modules()

    results = []
    total_start = time.monotonic()
    for module in modules:
        result = run_module(module, env)
        results.append(result)
        status = "ok" if result["ok"] else "FAIL"
        print(f"{result['seconds']:8.2f}s  {result['tests']:4d} tests  {status:5s}  {module}", flush=True)
    total_elapsed = time.monotonic() - total_start

    results.sort(key=lambda r: r["seconds"], reverse=True)
    total_tests = sum(r["tests"] for r in results)
    failures = [r["module"] for r in results if not r["ok"]]

    print("\n" + "=" * 70)
    print(f"engine={backend}  modules={len(modules)}  total_tests={total_tests}  "
          f"wall_seconds={total_elapsed:.1f}")
    if failures:
        print(f"FAILING MODULES: {failures}")
    print("\nSlowest 20 modules:")
    for r in results[:20]:
        print(f"  {r['seconds']:8.2f}s  {r['module']}")

    out_path = args.out or (REPO_ROOT / "scripts" / f".timing_{backend}.json")
    out_path.write_text(json.dumps({
        "engine": backend,
        "total_tests": total_tests,
        "wall_seconds": round(total_elapsed, 1),
        "results": sorted(results, key=lambda r: r["module"]),
    }, indent=2))
    print(f"\nWrote {out_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
