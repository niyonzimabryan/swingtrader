"""CUSIP → ticker, with the failures surfaced instead of dropped.

Spec O section 3.2: "with unmapped **and ambiguous** rows surfaced rather than
dropped — share classes and ADRs map to several FIGIs. Silent drops are how a
'top holdings' view quietly lies."

Three outcomes, and the second and third are the point of this module:

``mapped``
    Exactly one US-listed ticker. Use it.
``unmapped``
    OpenFIGI returned no data for the CUSIP, or returned only non-US venues.
    The holding is **kept** with ``ticker=None`` and lands in the warnings
    list. A position you cannot name is still a position, and dropping it
    understates a portfolio in a way nothing downstream can detect.
``ambiguous``
    Several distinct US tickers came back — a dual-class issuer, an ADR beside
    its ordinary, a units/warrants structure. **Every candidate is returned**
    and no pick is made. Picking the first result is the failure mode: it is
    right most of the time, which is exactly what makes the wrong times
    invisible.

Nothing here writes to the ledger. It resolves identifiers; the caller decides
what to do with a resolution it cannot use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from filings.openfigi_client import US_EXCHANGE_CODES, OpenFIGIClient
from utils.logger import get_logger

log = get_logger("cusip")

STATUS_MAPPED = "mapped"
STATUS_UNMAPPED = "unmapped"
STATUS_AMBIGUOUS = "ambiguous"

WARN_CUSIP_UNMAPPED = "cusip_unmapped"
WARN_CUSIP_AMBIGUOUS = "cusip_ambiguous"


@dataclass(frozen=True)
class Candidate:
    ticker: str
    figi: str
    name: str = ""
    exchange_code: str = ""
    security_type: str = ""

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "figi": self.figi,
            "name": self.name,
            "exchange_code": self.exchange_code,
            "security_type": self.security_type,
        }


@dataclass(frozen=True)
class CusipResolution:
    cusip: str
    status: str
    ticker: str | None = None
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""

    @property
    def warning(self) -> str | None:
        if self.status == STATUS_UNMAPPED:
            return WARN_CUSIP_UNMAPPED
        if self.status == STATUS_AMBIGUOUS:
            return WARN_CUSIP_AMBIGUOUS
        return None

    def as_dict(self) -> dict:
        return {
            "cusip": self.cusip,
            "status": self.status,
            "ticker": self.ticker,
            "candidates": [c.as_dict() for c in self.candidates],
            "detail": self.detail,
        }


def normalise_cusip(raw: str) -> str:
    return (raw or "").strip().upper()


def is_well_formed(cusip: str) -> bool:
    """Nine alphanumerics. Shape only — the check digit is not validated here."""
    value = normalise_cusip(cusip)
    return len(value) == 9 and value.isalnum()


def _candidates_from_result(result: dict) -> list[Candidate]:
    rows = result.get("data") or []
    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        out.append(
            Candidate(
                ticker=ticker,
                figi=str(row.get("figi") or ""),
                name=str(row.get("name") or ""),
                exchange_code=str(row.get("exchCode") or "").strip().upper(),
                security_type=str(row.get("securityType") or ""),
            )
        )
    return out


def _resolve_one(cusip: str, result: dict) -> CusipResolution:
    if not isinstance(result, dict):
        return CusipResolution(
            cusip, STATUS_UNMAPPED, detail="OpenFIGI returned a non-object result"
        )
    if "error" in result or "warning" in result:
        return CusipResolution(
            cusip,
            STATUS_UNMAPPED,
            detail=str(result.get("error") or result.get("warning")),
        )

    candidates = _candidates_from_result(result)
    if not candidates:
        return CusipResolution(cusip, STATUS_UNMAPPED, detail="no data rows")

    us = [c for c in candidates if c.exchange_code in US_EXCHANGE_CODES]
    if not us:
        return CusipResolution(
            cusip,
            STATUS_UNMAPPED,
            candidates=tuple(sorted(candidates, key=lambda c: (c.ticker, c.figi))),
            detail="mapped, but to no US-listed venue",
        )

    tickers = sorted({c.ticker for c in us})
    ordered = tuple(sorted(us, key=lambda c: (c.ticker, c.figi)))
    if len(tickers) > 1:
        return CusipResolution(
            cusip,
            STATUS_AMBIGUOUS,
            candidates=ordered,
            detail=f"{len(tickers)} distinct US tickers: {', '.join(tickers)}",
        )
    return CusipResolution(cusip, STATUS_MAPPED, ticker=tickers[0], candidates=ordered)


@dataclass
class ResolutionBatch:
    """Every input, resolved. Nothing is missing from ``resolutions``."""

    resolutions: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def mapped(self) -> int:
        return sum(1 for r in self.resolutions.values() if r.status == STATUS_MAPPED)

    @property
    def unmapped(self) -> int:
        return sum(1 for r in self.resolutions.values() if r.status == STATUS_UNMAPPED)

    @property
    def ambiguous(self) -> int:
        return sum(1 for r in self.resolutions.values() if r.status == STATUS_AMBIGUOUS)

    def as_dict(self) -> dict:
        return {
            "requested": len(self.resolutions),
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "ambiguous": self.ambiguous,
            "warnings": [w.as_dict() for w in self.warnings],
        }


def resolve_cusips(
    client: OpenFIGIClient, cusips: Iterable[str], *, exchange_code: str | None = None
) -> ResolutionBatch:
    """Map every CUSIP given, in batches the client's limits allow.

    The returned batch has one entry per distinct input, always. A caller that
    iterates ``resolutions`` cannot accidentally skip the failures, which is
    the difference between this and a dict comprehension over the successes.
    """
    wanted = []
    seen = set()
    batch = ResolutionBatch()
    for raw in cusips:
        value = normalise_cusip(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        if not is_well_formed(value):
            batch.resolutions[value] = CusipResolution(
                value, STATUS_UNMAPPED, detail="not a nine-character CUSIP"
            )
            continue
        wanted.append(value)

    for start in range(0, len(wanted), client.max_jobs):
        chunk = wanted[start : start + client.max_jobs]
        jobs = []
        for value in chunk:
            job = {"idType": "ID_CUSIP", "idValue": value}
            if exchange_code:
                job["exchCode"] = exchange_code
            jobs.append(job)
        for value, result in zip(chunk, client.map_jobs(jobs)):
            batch.resolutions[value] = _resolve_one(value, result)

    batch.warnings = [
        resolution
        for _, resolution in sorted(batch.resolutions.items())
        if resolution.warning is not None
    ]
    log.info("cusip_resolution", **batch.as_dict() | {"warnings": len(batch.warnings)})
    return batch
