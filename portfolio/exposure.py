"""Exposure aggregation across every account (Spec L §5.1, §3).

One rule shapes this module: **every aggregate spans the combined book.** A
concentration cap that counted only the Agentic account would be blind to the
same name held in the primary account, which is precisely the position that
makes adding to it a bad idea. So nothing here takes an account filter; the
caller who wants one account's rows filters before it gets here, and the risk
caps never do.

The second rule: an instrument this ledger cannot analyse is **surfaced, not
dropped**. A non-equity holding contributes its notional to gross exposure and
raises an ``unsupported_instrument_present`` warning carrying that notional. A
portfolio view that silently omits a short put is the worst failure available
here, and it is a failure that looks exactly like a clean answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from portfolio.records import UNSUPPORTED_INSTRUMENT_TYPES

UNSUPPORTED_INSTRUMENT_WARNING = "unsupported_instrument_present"


def _value_of(holding) -> float:
    """Signed exposure for one holding row, in dollars.

    For an equity that is market value, then notional, then quantity x last
    price. For anything else — an option, notably — it is **notional first**:
    the market value of a short put is a couple of hundred dollars and the
    thing it commits the account to is fourteen thousand, and reporting the
    former as "exposure" is how an option ends up looking harmless in a
    portfolio view.

    Never a cost basis. Exposure is what the position is worth now, and
    substituting basis for market value is how a doubled position reads flat.
    """
    order = (
        ("market_value", "notional")
        if getattr(holding, "instrument_type", "equity") == "equity"
        else ("notional", "market_value")
    )
    for attribute in order:
        value = getattr(holding, attribute, None)
        if value is not None:
            return float(value)
    quantity = getattr(holding, "quantity", None)
    price = getattr(holding, "last_price", None)
    if quantity is not None and price is not None:
        return float(quantity) * float(price)
    return 0.0


@dataclass(frozen=True)
class SymbolExposure:
    symbol: str
    instrument_type: str
    quantity: float
    value: float
    accounts: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "instrument_type": self.instrument_type,
            "quantity": self.quantity,
            "value": round(self.value, 2),
            "accounts": list(self.accounts),
        }


@dataclass(frozen=True)
class ExposureReport:
    by_symbol: tuple[SymbolExposure, ...]
    by_sector: dict
    by_tag: dict
    gross_exposure: float
    net_exposure: float
    long_exposure: float
    short_exposure: float
    total_value: float
    weights: dict
    largest_position_weight: float | None
    concentration_hhi: float | None
    unsupported: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    accounts_included: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "accounts_included": list(self.accounts_included),
            "by_symbol": [row.as_dict() for row in self.by_symbol],
            "by_sector": {k: round(v, 2) for k, v in sorted(self.by_sector.items())},
            "by_tag": {k: round(v, 2) for k, v in sorted(self.by_tag.items())},
            "gross_exposure": round(self.gross_exposure, 2),
            "net_exposure": round(self.net_exposure, 2),
            "long_exposure": round(self.long_exposure, 2),
            "short_exposure": round(self.short_exposure, 2),
            "total_value": round(self.total_value, 2),
            "weights": {k: round(v, 6) for k, v in sorted(self.weights.items())},
            "largest_position_weight": (
                round(self.largest_position_weight, 6)
                if self.largest_position_weight is not None
                else None
            ),
            "concentration_hhi": (
                round(self.concentration_hhi, 6) if self.concentration_hhi is not None else None
            ),
            "unsupported_instruments": [dict(row) for row in self.unsupported],
            "warnings": list(self.warnings),
        }


def aggregate_exposure(
    holdings,
    *,
    cash_total: float = 0.0,
    sectors: dict | None = None,
    tags: dict | None = None,
    account_labels: dict | None = None,
) -> ExposureReport:
    """Combine holdings from every account into one exposure picture.

    ``holdings`` is any iterable of objects with ``symbol``, ``quantity``,
    ``instrument_type``, ``account_id`` and a value field — the ORM rows and the
    :mod:`portfolio.records` dataclasses both qualify.

    ``sectors`` maps symbol -> sector and ``tags`` maps symbol -> iterable of
    exposure tags (Spec L §3: exposure by *narrative* is what a sector code
    cannot express, and is usually where correlated risk actually hides).
    A symbol with no sector is reported under ``"unknown"`` rather than dropped.
    """
    sectors = {k.upper(): v for k, v in (sectors or {}).items()}
    tags = {k.upper(): tuple(v) for k, v in (tags or {}).items()}
    account_labels = account_labels or {}

    combined: dict[tuple[str, str], dict] = {}
    unsupported: list[dict] = []
    accounts_seen: list[str] = []

    for holding in holdings:
        symbol = str(getattr(holding, "symbol", "") or "").upper()
        if not symbol:
            continue
        instrument_type = getattr(holding, "instrument_type", "equity") or "equity"
        account_id = getattr(holding, "account_id", None)
        label = account_labels.get(account_id, str(account_id))
        if label not in accounts_seen:
            accounts_seen.append(label)

        value = _value_of(holding)
        key = (symbol, instrument_type)
        row = combined.setdefault(
            key,
            {"symbol": symbol, "instrument_type": instrument_type, "quantity": 0.0, "value": 0.0, "accounts": []},
        )
        row["quantity"] += float(getattr(holding, "quantity", 0.0) or 0.0)
        row["value"] += value
        if label not in row["accounts"]:
            row["accounts"].append(label)

        if instrument_type in UNSUPPORTED_INSTRUMENT_TYPES:
            unsupported.append(
                {
                    "symbol": symbol,
                    "instrument_type": instrument_type,
                    "quantity": float(getattr(holding, "quantity", 0.0) or 0.0),
                    "notional": round(value, 2),
                    "account": label,
                    "detail": dict(getattr(holding, "instrument_detail", {}) or {}),
                    "note": (
                        "Stored and counted in gross exposure, not modelled for "
                        "analysis. Never omitted from an overview."
                    ),
                }
            )

    by_symbol = tuple(
        SymbolExposure(
            symbol=row["symbol"],
            instrument_type=row["instrument_type"],
            quantity=row["quantity"],
            value=row["value"],
            accounts=tuple(row["accounts"]),
        )
        for row in sorted(combined.values(), key=lambda r: (-abs(r["value"]), r["symbol"]))
    )

    long_exposure = sum(row.value for row in by_symbol if row.value > 0)
    short_exposure = sum(-row.value for row in by_symbol if row.value < 0)
    gross = long_exposure + short_exposure
    net = long_exposure - short_exposure
    total_value = net + float(cash_total or 0.0)

    by_sector: dict[str, float] = {}
    by_tag: dict[str, float] = {}
    for row in by_symbol:
        by_sector[sectors.get(row.symbol, "unknown")] = (
            by_sector.get(sectors.get(row.symbol, "unknown"), 0.0) + row.value
        )
        for tag in tags.get(row.symbol, ()):
            by_tag[tag] = by_tag.get(tag, 0.0) + row.value

    # Weights are of gross exposure, not of total value: a book that is 90%
    # cash still has a most-concentrated position, and dividing by total value
    # would report every weight as small right up to the moment the cash is
    # deployed.
    weights = (
        {row.symbol: abs(row.value) / gross for row in by_symbol} if gross > 0 else {}
    )
    largest = max(weights.values()) if weights else None
    hhi = sum(w * w for w in weights.values()) if weights else None

    warnings: list[str] = []
    if unsupported:
        total_notional = sum(abs(row["notional"]) for row in unsupported)
        warnings.append(
            f"{UNSUPPORTED_INSTRUMENT_WARNING}: {len(unsupported)} position(s) "
            f"of a type this ledger does not model "
            f"({', '.join(sorted({row['instrument_type'] for row in unsupported}))}), "
            f"total notional ${total_notional:,.2f}. They are shown, not omitted."
        )

    return ExposureReport(
        by_symbol=by_symbol,
        by_sector=by_sector,
        by_tag=by_tag,
        gross_exposure=gross,
        net_exposure=net,
        long_exposure=long_exposure,
        short_exposure=short_exposure,
        total_value=total_value,
        weights=weights,
        largest_position_weight=largest,
        concentration_hhi=hhi,
        unsupported=tuple(unsupported),
        warnings=tuple(warnings),
        accounts_included=tuple(accounts_seen),
    )
