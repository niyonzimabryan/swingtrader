#!/usr/bin/env python3
"""Record real SEC Form 4 / Schedule 13 responses into ``tests/fixtures/filings/``.

The Phase 4 test suite runs offline against fixtures. They should be real
responses, fetched once and committed; see ``tests/fixtures/filings/README.md``
for why the committed set is currently synthetic and what this script fixes.

    export SEC_USER_AGENT="Your Name your.address@example.com"
    python -m scripts.record_filings_fixtures --tickers AAPL MSFT --limit 6

Writes, per ticker: ``submissions_CIK<cik>.json``, the ownership XML for the
most recent Form 4s and 4/As, and the structured cover page for the most recent
Schedules 13D/G (named ``schedule13d_<accession>.xml`` because EDGAR calls them
all ``primary_doc.xml``), plus a ``schedule13_index.json`` mapping.

Runs through ``filings.client``, so it is throttled and identified like every
other SEC access in this repository. Existing files are left alone unless
``--overwrite`` is passed.
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
from filings import ownership  # noqa: E402
from filings.client import SECClientError, client_from_settings  # noqa: E402
from filings.sec_minimal import load_ticker_map, parse_submissions  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "filings"

FORM4_FORMS = {"4", "4/A"}


def _write(path: Path, content: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        print(f"  skipped {path.name} (exists; --overwrite to replace)")
        return False
    path.write_text(content, encoding="utf-8")
    print(f"  wrote {path.name} ({path.stat().st_size:,} bytes)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--out", default=str(FIXTURE_DIR))
    parser.add_argument("--limit", type=int, default=6, help="documents per form family")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    index: dict[str, str] = {}

    with client_from_settings(settings) as client:
        ticker_map = load_ticker_map(client)
        for ticker in args.tickers:
            symbol = ticker.upper()
            cik = ticker_map.get(symbol)
            if cik is None:
                print(f"{symbol}: no CIK in company_tickers.json")
                continue

            print(f"{symbol} (CIK {cik})")
            payload = client.get_json(sec_client.submissions_url(cik))
            _write(
                out_dir / f"submissions_CIK{cik}.json",
                json.dumps(payload, indent=1) + "\n",
                overwrite=args.overwrite,
            )

            company = parse_submissions(payload)
            filings = sorted(company.filings, key=lambda f: f.acceptance, reverse=True)

            recorded = 0
            for filing in filings:
                if recorded >= args.limit:
                    break
                if filing.form.strip().upper() not in FORM4_FORMS:
                    continue
                url = sec_client.filing_document_url(
                    cik, filing.accession, filing.primary_document
                )
                try:
                    text = client.get_text(url)
                except SECClientError as exc:
                    print(f"  {filing.accession}: {exc}")
                    continue
                name = f"form4_{filing.accession}.xml"
                _write(out_dir / name, text, overwrite=args.overwrite)
                recorded += 1

            recorded = 0
            for filing in filings:
                if recorded >= args.limit:
                    break
                form = filing.form.strip().upper()
                if not ownership.is_schedule_13(form):
                    continue
                url = sec_client.filing_document_url(cik, filing.accession, "primary_doc.xml")
                text = client.get_text_or_none(url)
                if text is None:
                    print(f"  {filing.accession}: no structured cover page (pre-2024)")
                    continue
                prefix = "schedule13g" if "13G" in form else "schedule13d"
                name = f"{prefix}_{filing.accession}.xml"
                _write(out_dir / name, text, overwrite=args.overwrite)
                index[f"{prefix}_{recorded}"] = filing.accession
                recorded += 1

    if index:
        _write(
            out_dir / "schedule13_index_recorded.json",
            json.dumps(index, indent=1) + "\n",
            overwrite=True,
        )
    print(
        "\nKeep the synthetic fixtures alongside these: they carry the edge "
        "cases (every transaction code, both 10b5-1 mechanisms, an unflagged "
        "sale, a reused ticker) that a handful of real filings will not."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
