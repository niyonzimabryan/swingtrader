"""``filings_recent`` — the stable read API for the filings plane.

Spec K section 4.2 names this as an MCP read tool. **There is no
``workspace/`` service package on ``main`` at the time this phase was built**
(Phase 0b has not merged), so this ships as a plain Python function with a
stable signature and a provenance block, and registering it as a tool is the
ten-line follow-up documented in ``docs/FILINGS_PLANE.md``:

    from filings.api import filings_recent
    @server.tool()
    def filings_recent_tool(ticker: str | None = None, ...) -> dict:
        with get_session() as session:
            return filings_recent(session, ticker=ticker, ...)

It is a **read**. It cannot write, it cannot place an order, and it calls no
model. 13F is out of scope for Phase 4, so nothing here returns one — and
therefore nothing here can render a 13F position without its staleness and
portfolio share, which is the Spec O section 6 rule for that form.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from filings import entities, provenance
from filings.eight_k import FACT_TYPE_EIGHT_K_ITEM, SOURCE_EIGHT_K_ITEMS
from filings.form4 import (
    ALL_FACT_TYPES as FORM4_FACT_TYPES,
    OPEN_MARKET_FACT_TYPES,
    SOURCE_FORM4,
    insider_purchase_clusters,
    net_open_market_shares,
)
from filings.observations import current_view, observations_known_at
from filings.ownership import (
    FACT_TYPE_13D_EVENT,
    FACT_TYPE_13G_EVENT,
    SOURCE_SCHEDULE_13,
)

#: Every fact type this plane produces, so a caller can ask for "everything".
FILINGS_FACT_TYPES: tuple[str, ...] = tuple(
    sorted(
        set(FORM4_FACT_TYPES)
        | {FACT_TYPE_EIGHT_K_ITEM, FACT_TYPE_13D_EVENT, FACT_TYPE_13G_EVENT}
    )
)

SOURCE_LABELS = {
    SOURCE_FORM4: "SEC EDGAR Form 4 ownership XML",
    SOURCE_SCHEDULE_13: "SEC EDGAR Schedule 13D/G",
    SOURCE_EIGHT_K_ITEMS: "SEC EDGAR 8-K item index",
}


def _row_dict(row) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "fact_type": row.fact_type,
        "entity_cik": row.entity_cik,
        "ticker_at_time": row.ticker_at_time,
        "value_numeric": row.value_numeric,
        "value_text": row.value_text,
        "unit": row.unit,
        "valid_at": row.valid_at.isoformat(),
        "known_at_utc": row.known_at_utc.isoformat(),
        "precision": row.precision,
        "provenance_class": row.provenance_class,
        "replay_eligible": row.replay_eligible,
        "accession": row.accession,
        "source_url": row.source_url,
        "superseded_observation_id": row.superseded_observation_id,
        "warnings": row.warnings,
        "payload": row.payload,
    }


def filings_recent(
    session,
    *,
    ticker: str | None = None,
    entity_cik: str | None = None,
    as_of: datetime | None = None,
    fact_types: Sequence[str] | None = None,
    limit: int = 50,
    include_superseded: bool = True,
) -> dict:
    """Filings facts knowable at ``as_of``, newest first, with provenance.

    ``include_superseded`` defaults to **True** and that is deliberate: an
    amendment supersedes an original, but the original is what was known
    before the amendment landed, and hiding it by default would make the
    ledger answer a bitemporal question unitemporally. Set it False for a
    "current state" view.
    """
    cutoff = as_of or datetime.now(timezone.utc)
    resolved_cik = entity_cik
    resolution = None
    if resolved_cik is None and ticker:
        resolution = entities.cik_for_ticker(session, ticker, cutoff)
        resolved_cik = resolution.value

    rows = observations_known_at(
        session,
        fact_type=list(fact_types) if fact_types else list(FILINGS_FACT_TYPES),
        cutoff=cutoff,
        entity_cik=resolved_cik,
        ticker_at_time=None if resolved_cik else (ticker.upper() if ticker else None),
    )
    if not include_superseded:
        superseded = {
            row.superseded_observation_id for row in rows if row.superseded_observation_id
        }
        rows = [row for row in rows if row.id not in superseded]

    rows = sorted(rows, key=lambda r: (r.known_at_utc, r.id), reverse=True)[: max(0, limit)]

    notes = []
    if ticker and resolution is not None and not resolution.resolved:
        notes.append(
            f"ticker {ticker!r} did not resolve to a CIK at {cutoff.date()} "
            f"({resolution.warning}); the answer falls back to a ticker match on "
            "the stored rows, which is a snapshot join, not a point-in-time one."
        )
    if resolution is not None and resolution.candidates and len(resolution.candidates) > 1:
        notes.append(f"ambiguous ticker: candidates {list(resolution.candidates)}")

    latest = max((r.known_at_utc for r in rows), default=None)
    return {
        "ticker": ticker,
        "entity_cik": resolved_cik,
        "rows": [_row_dict(row) for row in rows],
        "provenance": provenance.build(
            as_of=cutoff,
            rows=rows,
            sources={
                label: SOURCE_LABELS.get(label, label)
                for label in sorted({r.source for r in rows})
            },
            staleness={
                "days_since_latest_filing": provenance.staleness_days(cutoff, latest)
            },
            notes=notes,
        ),
    }


def insider_activity(
    session,
    *,
    entity_cik: str,
    as_of: datetime | None = None,
    window_days: int = 90,
    cluster_window_days: int = 30,
    min_cluster_insiders: int = 2,
) -> dict:
    """Open-market insider totals and clusters, with the pooling rule enforced.

    Only ``P``/``S`` rows reach :func:`net_open_market_shares`; awards, option
    exercises, tax-withholding sales and gifts are counted **separately** and
    reported beside the totals rather than inside them (Spec O section 3.1).
    """
    cutoff = as_of or datetime.now(timezone.utc)
    rows = observations_known_at(
        session,
        fact_type=list(FORM4_FACT_TYPES),
        cutoff=cutoff,
        entity_cik=entity_cik,
    )
    window_start = cutoff.timestamp() - window_days * 86400
    in_window = [
        row
        for row in current_view(rows)
        if row.known_at_utc.replace(tzinfo=timezone.utc).timestamp() >= window_start
    ]
    open_market = [r for r in in_window if r.fact_type in OPEN_MARKET_FACT_TYPES]
    other = [r for r in in_window if r.fact_type not in OPEN_MARKET_FACT_TYPES]

    totals = net_open_market_shares(open_market)
    other_counts: dict[str, int] = {}
    for row in other:
        other_counts[row.fact_type] = other_counts.get(row.fact_type, 0) + 1

    clusters = insider_purchase_clusters(
        open_market, window_days=cluster_window_days, min_insiders=min_cluster_insiders
    )
    return {
        "entity_cik": entity_cik,
        "window_days": window_days,
        "open_market": {
            "purchased_shares": totals.purchased_shares,
            "sold_shares": totals.sold_shares,
            "net_shares": totals.net_shares,
            "purchase_notional": totals.purchase_notional,
            "sale_notional": totals.sale_notional,
            "distinct_insiders": totals.distinct_insiders,
            "planned_10b5_1_sales": totals.planned_sales,
            "planned_10b5_1_sale_shares": totals.planned_sale_shares,
            "rows_with_unknown_plan_flag": totals.unknown_plan_flag,
        },
        "not_pooled_with_open_market": other_counts,
        "purchase_clusters": [
            {
                "window_start": c.window_start.isoformat(),
                "window_end": c.window_end.isoformat(),
                "distinct_insiders": c.distinct_insiders,
                "operating_insiders": c.operating_insiders,
                "total_shares": c.total_shares,
                "total_notional": c.total_notional,
                "known_at_utc": c.known_at_utc.isoformat(),
            }
            for c in clusters
        ],
        "provenance": provenance.build(
            as_of=cutoff,
            rows=in_window,
            sources={SOURCE_FORM4: SOURCE_LABELS[SOURCE_FORM4]},
            staleness={
                "days_since_latest_form4": provenance.staleness_days(
                    cutoff, max((r.known_at_utc for r in in_window), default=None)
                )
            },
            notes=[
                "Open-market P/S totals never include A/M/F/G rows; those are "
                "counted in not_pooled_with_open_market (Spec O section 3.1).",
                "Amended filings are counted once: a row a 4/A supersedes is "
                "dropped from the aggregate, and stays queryable in the ledger.",
            ],
        ),
    }
