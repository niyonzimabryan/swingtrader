#!/usr/bin/env python3
"""Backfill the SEC minimal plane into ``source_observations``.

    python -m scripts.sec_backfill --tickers AAPL MSFT --since 2020-01-01
    python -m scripts.sec_backfill --coverage-universe --since 2015-01-01 --report out.json

``--since`` is a **knowledge** cutoff, not a period cutoff: it selects filings
*accepted* on or after that date. That is the bitemporal reading — the ledger
records when we could have known something — and it is why a re-run with an
earlier ``--since`` adds older rows without disturbing the newer ones.

**Idempotent.** Every observation's natural key is the hash of its identity plus
its value, so a second run over the same window inserts nothing and reports the
rows as duplicates. A restatement is a different value, therefore a different
hash, therefore a new row beside the original — never an overwrite.

Writing requires ``PLANE_SEC_MINIMAL_ENABLED=true``. ``--dry-run`` fetches,
normalises and reports coverage without opening a write transaction.

``--fixtures DIR`` replays recorded responses from a directory instead of
calling SEC, so the coverage report can be reproduced offline and in CI. It
resolves tickers through ``company_tickers.json`` in that directory.
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
from filings.client import SECClientError, client_from_settings  # noqa: E402
from filings.coverage_universe import COVERAGE_UNIVERSE_50  # noqa: E402
from filings.errors import AdapterSchemaError, PlaneDisabled  # noqa: E402
from filings.sec_minimal import CompanyCoverage, ingest_company, load_ticker_map  # noqa: E402
from filings.xbrl_aliases import CORE_FACT_TYPES  # noqa: E402


@dataclass
class BackfillReport:
    """The Phase 3a checkpoint numbers, in the shape they are reported in."""

    since: str | None
    tickers_requested: int = 0
    companies_ingested: int = 0
    companies_failed: int = 0
    rows_written: int = 0
    rows_duplicate: int = 0
    companies_with_continuous_revenue_and_eps: int = 0
    companies_with_share_count: int = 0
    companies_with_8k_item_202: int = 0
    eight_k_total: int = 0
    eight_k_item_202: int = 0
    unresolved_accessions: int = 0
    alert_counts: dict = field(default_factory=dict)
    per_company: list = field(default_factory=list)

    @property
    def continuous_series_rate(self) -> float:
        return _rate(self.companies_with_continuous_revenue_and_eps, self.companies_ingested)

    @property
    def share_count_rate(self) -> float:
        return _rate(self.companies_with_share_count, self.companies_ingested)

    @property
    def item_202_company_rate(self) -> float:
        return _rate(self.companies_with_8k_item_202, self.companies_ingested)

    @property
    def item_202_filing_rate(self) -> float:
        return _rate(self.eight_k_item_202, self.eight_k_total)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _record(report: BackfillReport, coverage: CompanyCoverage) -> None:
    if not coverage.ok:
        report.companies_failed += 1
        report.per_company.append(
            {"cik": coverage.cik, "ticker": coverage.ticker, "error": coverage.error}
        )
        return

    report.companies_ingested += 1
    report.rows_written += coverage.written
    report.rows_duplicate += coverage.duplicates
    report.eight_k_total += coverage.eight_k_total
    report.eight_k_item_202 += coverage.eight_k_item_202
    report.unresolved_accessions += coverage.unresolved_accessions
    if coverage.has_core_series:
        report.companies_with_continuous_revenue_and_eps += 1
    if coverage.share_count_rows:
        report.companies_with_share_count += 1
    if coverage.eight_k_item_202:
        report.companies_with_8k_item_202 += 1
    for alert in coverage.alerts:
        report.alert_counts[alert.kind] = report.alert_counts.get(alert.kind, 0) + 1
    report.per_company.append(
        {
            "cik": coverage.cik,
            "ticker": coverage.ticker,
            "written": coverage.written,
            "duplicates": coverage.duplicates,
            "continuous": {k: bool(v) for k, v in coverage.continuous_series.items()},
            "share_count_rows": coverage.share_count_rows,
            "latest_share_count": (
                coverage.latest_share_count.isoformat()
                if coverage.latest_share_count
                else None
            ),
            "eight_k_total": coverage.eight_k_total,
            "eight_k_item_202": coverage.eight_k_item_202,
            "unresolved_accessions": coverage.unresolved_accessions,
            "alerts": [
                {"kind": a.kind, "fact_type": a.fact_type, "detail": a.detail}
                for a in coverage.alerts
            ],
        }
    )


def run_backfill(session, client, tickers, *, settings, since, ticker_map=None) -> BackfillReport:
    """Ingest each ticker and accumulate the coverage report."""
    report = BackfillReport(
        since=since.isoformat() if since else None, tickers_requested=len(tickers)
    )
    resolved = ticker_map if ticker_map is not None else load_ticker_map(client)

    for ticker in tickers:
        symbol = ticker.upper()
        cik = resolved.get(symbol)
        if cik is None:
            report.companies_failed += 1
            report.per_company.append(
                {"ticker": symbol, "error": "no CIK in company_tickers.json"}
            )
            continue
        try:
            coverage = ingest_company(session, client, cik, settings=settings, since=since)
        except PlaneDisabled:
            raise
        except (AdapterSchemaError, SECClientError, ValueError) as exc:
            coverage = CompanyCoverage(cik=cik, ticker=symbol, error=f"{type(exc).__name__}: {exc}")
        _record(report, coverage)

    return report


def format_report(report: BackfillReport) -> str:
    lines = [
        "SEC minimal plane — coverage",
        f"  since (acceptance date)      {report.since or 'all history'}",
        f"  tickers requested            {report.tickers_requested}",
        f"  companies ingested           {report.companies_ingested}",
        f"  companies failed             {report.companies_failed}",
        f"  observations written         {report.rows_written}",
        f"  observations already present {report.rows_duplicate}",
        "",
        "  continuous revenue AND EPS   "
        f"{report.companies_with_continuous_revenue_and_eps}/{report.companies_ingested}"
        f"  ({report.continuous_series_rate:.1%})",
        "  share-count coverage         "
        f"{report.companies_with_share_count}/{report.companies_ingested}"
        f"  ({report.share_count_rate:.1%})",
        "  8-K Item 2.02, companies     "
        f"{report.companies_with_8k_item_202}/{report.companies_ingested}"
        f"  ({report.item_202_company_rate:.1%})",
        "  8-K Item 2.02, filings       "
        f"{report.eight_k_item_202}/{report.eight_k_total}"
        f"  ({report.item_202_filing_rate:.1%})",
        f"  facts skipped, no acceptance {report.unresolved_accessions}",
    ]
    if report.alert_counts:
        lines.append("")
        lines.append("  coverage alerts")
        for kind, count in sorted(report.alert_counts.items()):
            lines.append(f"    {kind:<20} {count}")
    lines.append("")
    lines.append(f"  core fact types checked for continuity: {', '.join(CORE_FACT_TYPES)}")
    return "\n".join(lines)


def _fixture_client(directory: Path):
    """A client whose transport serves recorded JSON from ``directory``."""
    import httpx

    from filings import client as sec_client
    from filings.client import SECClient

    root = Path(directory)

    def handler(request: httpx.Request) -> httpx.Response:
        path = root / sec_client.fixture_name_for_url(str(request.url))
        if not path.exists():
            return httpx.Response(404, json={"error": "not recorded", "path": str(path)})
        return httpx.Response(200, content=path.read_bytes(), headers={"content-type": "application/json"})

    return SECClient(
        "SwingTrader Fixture Replay fixtures@example.invalid",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tickers", nargs="+")
    group.add_argument(
        "--coverage-universe",
        action="store_true",
        help="the frozen 50-ticker list in filings/coverage_universe.py",
    )
    parser.add_argument("--since", help="YYYY-MM-DD acceptance-date floor")
    parser.add_argument("--fixtures", help="replay recorded responses from this directory")
    parser.add_argument("--report", help="write the coverage report as JSON to this path")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = parser.parse_args(argv)

    tickers = list(COVERAGE_UNIVERSE_50) if args.coverage_universe else list(args.tickers)
    since = date.fromisoformat(args.since) if args.since else None

    settings = Settings()
    init_db(settings.database_url)

    client = _fixture_client(Path(args.fixtures)) if args.fixtures else client_from_settings(settings)
    try:
        with get_session() as session:
            report = run_backfill(session, client, tickers, settings=settings, since=since)
            if args.dry_run:
                session.rollback()
    finally:
        client.close()

    print(format_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
        print(f"\n  report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
