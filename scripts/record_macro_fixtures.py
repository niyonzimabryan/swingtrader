#!/usr/bin/env python3
"""Record real ALFRED vintages into ``tests/fixtures/macro/``.

The Phase 4 macro tests run offline against fixtures. They should be real
recordings; see ``tests/fixtures/macro/README.md`` for why the committed set is
currently synthetic and what this script fixes.

    export FRED_API_KEY=your_key
    python -m scripts.record_macro_fixtures --series CPIAUCSL USREC DGS10

ALFRED uses the same key as FRED. Each series is written as
``alfred_<SERIES>.json`` in the shape ``macro.vintage.FixtureVintageSource``
reads, which is also the shape ``data.macro_data`` returns — so a recording
drops straight in with no conversion.

Only series registered in ``macro/series.py`` are recorded: an unclassified
series cannot be checked against the ``regime_v1`` allowlist, and the point of
the allowlist is that nothing gets in by omission.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from data.macro_data import MacroDataAdapter  # noqa: E402
from macro import series as series_registry  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "macro"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", nargs="+", required=True)
    parser.add_argument("--out", default=str(FIXTURE_DIR))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    if not settings.fred_api_key:
        raise SystemExit("FRED_API_KEY is unset; ALFRED uses the same key.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    adapter = MacroDataAdapter(settings.fred_api_key)

    for series_id in args.series:
        spec = series_registry.spec_for(series_id)
        path = out_dir / f"alfred_{spec.series_id}.json"
        if path.exists() and not args.overwrite:
            print(f"  skipped {path.name} (exists; --overwrite to replace)")
            continue
        rows = adapter.get_series_all_releases(spec.series_id)
        path.write_text(
            json.dumps({"series_id": spec.series_id, "rows": rows}, indent=1) + "\n",
            encoding="utf-8",
        )
        references = {row["reference_date"] for row in rows}
        print(
            f"  wrote {path.name}: {len(rows)} releases over "
            f"{len(references)} reference periods "
            f"({'never revised' if spec.never_revised else 'REVISED'})"
        )
        if spec.never_revised and len(rows) != len(references):
            print(
                f"    NOTE: {spec.series_id} is on the never-revised allowlist but "
                "ALFRED holds more releases than reference periods. Check the "
                "allowlist before trusting regime_v1 on it."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
