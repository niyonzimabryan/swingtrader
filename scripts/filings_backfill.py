#!/usr/bin/env python3
"""Backfill the Phase 4 filings plane into ``source_observations``.

    python -m scripts.filings_backfill --tickers AAPL MSFT --since 2020-01-01
    python -m scripts.filings_backfill --ciks 0000320193 --resolve-tickers
    python -m scripts.filings_backfill --ciks 0002000001 --fixtures tests/fixtures/filings

Form 4 (with the transaction codes kept apart), Schedules 13D/G, the full 8-K
item index, and entity history — all through the one throttled SEC client and
the one ledger seam.

``--since`` is a **knowledge** cutoff, not a period cutoff: it selects filings
*accepted* on or after that date. **Idempotent**: a second run over the same
window inserts nothing and reports the rows as duplicates.

``--resolve-tickers`` runs the entity-history pass over rows still carrying
Phase 3a's ``ticker_from_current_snapshot`` warning. It is a **dry run** unless
``--apply`` is given, because it rewrites ``ticker_at_time`` on stored rows.

Writing requires ``PLANE_FILINGS_ENABLED=true``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from database.db import get_session, init_db  # noqa: E402
from filings import entities  # noqa: E402
from filings.client import SECClientError, client_from_settings  # noqa: E402
from filings.errors import AdapterSchemaError, PlaneDisabled  # noqa: E402
from filings.plane import FilingsCoverage, ingest_filings  # noqa: E402
from filings.sec_minimal import load_ticker_map  # noqa: E402


@dataclass
class FilingsReport:
    """The Phase 4 filings checkpoint numbers."""

    since: str | None
    requested: int = 0
    companies_ingested: int = 0
    companies_failed: int = 0
    rows_written: int = 0
    rows_duplicate: int = 0
    form4_filings: int = 0
    form4_parsed: int = 0
    form4_missing_document: int = 0
    transaction_codes: dict = field(default_factory=dict)
    amendments_linked: int = 0
    amendments_unlinked: int = 0
    schedule13_filings: int = 0
    schedule13_structured: int = 0
    eight_k_items: dict = field(default_factory=dict)
    companies_with_resolved_ticker: int = 0
    entity_intervals_opened: int = 0
    entity_intervals_closed: int = 0
    entity_coverage: dict = field(default_factory=dict)
    snapshot_resolution: dict = field(default_factory=dict)
    per_company: list = field(default_factory=list)

    @property
    def form4_parse_rate(self) -> float:
        return _rate(self.form4_parsed, self.form4_filings)

    @property
    def schedule13_structured_rate(self) -> float:
        return _rate(self.schedule13_structured, self.schedule13_filings)

    @property
    def ticker_resolution_rate(self) -> float:
        return _rate(self.companies_with_resolved_ticker, self.companies_ingested)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _record(report: FilingsReport, coverage: FilingsCoverage) -> None:
    if coverage.errors:
        report.companies_failed += 1
        report.per_company.append(
            {"cik": coverage.cik, "ticker": coverage.ticker, "errors": coverage.errors}
        )
        return

    report.companies_ingested += 1
    report.rows_written += coverage.written
    report.rows_duplicate += coverage.duplicates
    report.form4_filings += coverage.form4_filings
    report.form4_parsed += coverage.form4_parsed
    report.form4_missing_document += coverage.form4_missing_document
    report.amendments_linked += coverage.amendments_linked
    report.amendments_unlinked += coverage.amendments_unlinked
    report.schedule13_filings += coverage.schedule13_filings
    report.schedule13_structured += coverage.schedule13_structured
    report.entity_intervals_opened += coverage.entity_intervals_opened
    report.entity_intervals_closed += coverage.entity_intervals_closed
    if coverage.ticker_resolved:
        report.companies_with_resolved_ticker += 1
    for code, count in coverage.form4_transactions.items():
        report.transaction_codes[code] = report.transaction_codes.get(code, 0) + count
    for item, count in coverage.eight_k_items.items():
        report.eight_k_items[item] = report.eight_k_items.get(item, 0) + count
    report.per_company.append(coverage.as_dict())


def run_backfill(session, client, ciks, *, settings, since) -> FilingsReport:
    report = FilingsReport(
        since=since.isoformat() if since else None, requested=len(ciks)
    )
    for cik in ciks:
        try:
            coverage = ingest_filings(
                session, client, cik, settings=settings, since=since
            )
        except PlaneDisabled:
            raise
        except (AdapterSchemaError, SECClientError, ValueError) as exc:
            coverage = FilingsCoverage(cik=str(cik), errors=[f"{type(exc).__name__}: {exc}"])
        _record(report, coverage)

    report.entity_coverage = entities.coverage(session).as_dict()
    return report


def format_report(report: FilingsReport) -> str:
    lines = [
        "Filings plane (Phase 4) — coverage",
        f"  since (acceptance date)      {report.since or 'all history'}",
        f"  companies requested          {report.requested}",
        f"  companies ingested           {report.companies_ingested}",
        f"  companies failed             {report.companies_failed}",
        f"  observations written         {report.rows_written}",
        f"  observations already present {report.rows_duplicate}",
        "",
        "  Form 4 documents parsed      "
        f"{report.form4_parsed}/{report.form4_filings}"
        f"  ({report.form4_parse_rate:.1%})",
        f"  Form 4 documents missing     {report.form4_missing_document}",
        f"  amendments linked            {report.amendments_linked}",
        f"  amendments unlinked          {report.amendments_unlinked}",
        "  13D/G structured cover pages "
        f"{report.schedule13_structured}/{report.schedule13_filings}"
        f"  ({report.schedule13_structured_rate:.1%})",
        "",
        "  entity resolution",
        "    companies with a resolved ticker  "
        f"{report.companies_with_resolved_ticker}/{report.companies_ingested}"
        f"  ({report.ticker_resolution_rate:.1%})",
        f"    intervals opened                  {report.entity_intervals_opened}",
        f"    intervals closed                  {report.entity_intervals_closed}",
    ]
    for key, value in sorted(report.entity_coverage.items()):
        lines.append(f"    {key:<33} {value}")

    if report.transaction_codes:
        lines.append("")
        lines.append("  Form 4 transaction codes (never pooled)")
        for code, count in sorted(report.transaction_codes.items()):
            lines.append(f"    {code:<20} {count}")
    if report.eight_k_items:
        lines.append("")
        lines.append("  8-K items")
        for item, count in sorted(report.eight_k_items.items()):
            lines.append(f"    {item:<20} {count}")
    if report.snapshot_resolution:
        lines.append("")
        lines.append("  Phase 3a snapshot-ticker resolution")
        for key, value in sorted(report.snapshot_resolution.items()):
            lines.append(f"    {key:<20} {value}")
    return "\n".join(lines)


def _fixture_client(directory: Path):
    """A client whose transport serves recorded documents from ``directory``.

    Keyed the way EDGAR lays them out: ``submissions_CIK*.json`` by URL, and a
    document by its file name inside the accession folder. Structured Schedule
    13 cover pages are all called ``primary_doc.xml``, so they are looked up by
    the accession folder through ``schedule13_index.json`` when one is present.
    """
    import httpx

    from filings import client as sec_client
    from filings.client import SECClient

    root = Path(directory)
    schedule_map: dict[str, str] = {}
    index_path = root / "schedule13_index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for key, accession in index.items():
            for prefix in ("schedule13d_", "schedule13g_"):
                candidate = root / f"{prefix}{accession}.xml"
                if candidate.exists():
                    schedule_map[accession.replace("-", "")] = candidate.name

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        leaf = url_path.rstrip("/").split("/")[-1]
        if url_path.startswith("/submissions/"):
            target = root / sec_client.fixture_name_for_url(str(request.url))
        elif leaf == "primary_doc.xml":
            folder = url_path.rstrip("/").split("/")[-2]
            name = schedule_map.get(folder)
            target = root / name if name else root / leaf
        else:
            target = root / leaf
        if not target.exists():
            return httpx.Response(404, json={"error": "not recorded", "path": str(target)})
        return httpx.Response(200, content=target.read_bytes())

    return SECClient(
        "SwingTrader Fixture Replay fixtures@example.invalid",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tickers", nargs="+")
    group.add_argument("--ciks", nargs="+")
    parser.add_argument("--since", help="YYYY-MM-DD acceptance-date floor")
    parser.add_argument("--fixtures", help="replay recorded responses from this directory")
    parser.add_argument("--report", help="write the coverage report as JSON to this path")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    parser.add_argument(
        "--resolve-tickers",
        action="store_true",
        help="run entity history over rows carrying ticker_from_current_snapshot",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="with --resolve-tickers, actually rewrite ticker_at_time",
    )
    args = parser.parse_args(argv)

    since = date.fromisoformat(args.since) if args.since else None
    settings = Settings()
    init_db(settings.database_url)

    client = (
        _fixture_client(Path(args.fixtures))
        if args.fixtures
        else client_from_settings(settings)
    )
    try:
        with get_session() as session:
            ciks = list(args.ciks or [])
            if args.tickers:
                ticker_map = load_ticker_map(client)
                for ticker in args.tickers:
                    cik = ticker_map.get(ticker.upper())
                    if cik is None:
                        print(f"  no CIK for {ticker.upper()} in company_tickers.json")
                        continue
                    ciks.append(cik)

            report = run_backfill(session, client, ciks, settings=settings, since=since)
            if args.resolve_tickers:
                report.snapshot_resolution = entities.resolve_snapshot_warnings(
                    session, apply=args.apply
                ).as_dict()
            if args.dry_run:
                session.rollback()
    finally:
        client.close()

    print(format_report(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n  report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
