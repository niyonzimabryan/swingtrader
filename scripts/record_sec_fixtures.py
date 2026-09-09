#!/usr/bin/env python3
"""Record real SEC responses into ``tests/fixtures/sec/``.

The Phase 3a test suite runs offline against fixtures. They should be real
responses, fetched once and committed; see ``tests/fixtures/sec/README.md`` for
why the committed set is currently synthetic and what this script fixes.

    export SEC_USER_AGENT="Your Name your.address@example.com"
    python -m scripts.record_sec_fixtures --tickers AAPL MSFT KO

Writes ``submissions_CIK<cik>.json`` and ``companyfacts_CIK<cik>.json`` per
ticker. Existing files are left alone unless ``--overwrite`` is passed. Runs
through ``filings.client``, so it is throttled and identified like every other
SEC access in this repository.
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
from filings import client as sec_client  # noqa: E402
from filings.client import client_from_settings  # noqa: E402
from filings.sec_minimal import load_ticker_map  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "sec"


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size:,} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--out", default=str(FIXTURE_DIR))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    with client_from_settings(settings) as client:
        ticker_map = load_ticker_map(client)
        _write(out_dir / "company_tickers_real.json", {
            str(i): {"cik_str": int(cik), "ticker": ticker, "title": ""}
            for i, (ticker, cik) in enumerate(sorted(ticker_map.items()))
            if ticker in {t.upper() for t in args.tickers}
        })

        for ticker in args.tickers:
            symbol = ticker.upper()
            cik = ticker_map.get(symbol)
            if cik is None:
                print(f"{symbol}: not in company_tickers.json", file=sys.stderr)
                continue
            print(f"{symbol} (CIK {cik})")
            for name, url in (
                (f"submissions_CIK{cik}.json", sec_client.submissions_url(cik)),
                (f"companyfacts_CIK{cik}.json", sec_client.companyfacts_url(cik)),
            ):
                path = out_dir / name
                if path.exists() and not args.overwrite:
                    print(f"  {name} exists; pass --overwrite to replace")
                    continue
                payload = client.get_json_or_none(url)
                if payload is None:
                    print(f"  {name}: 404 from SEC, skipped")
                    continue
                _write(path, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
