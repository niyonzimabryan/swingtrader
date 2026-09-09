"""
Macro data adapter — FRED and ALFRED (Federal Reserve Economic Data).

Two paths, and the difference between them is the whole of Spec O section 4:

* **Current values** (``get_fed_funds_rate`` and friends) — what the series
  says today. Correct for "what is the environment right now", and *lookahead*
  for any historical question, because today's value is the revised value.
* **Vintages** (``get_series_as_of_date`` / ``get_series_all_releases``) —
  what the series said on a past date, including the release lag: a print that
  had not been published at ``as_of`` is **absent**, not back-filled.

``fredapi`` is rated inactive upstream, so it is pinned in ``requirements.txt``
and wrapped **here** — this module is the only place that imports it, which is
what makes a break a one-file fix (``pyfredapi`` is the runner-up). The vintage
methods return plain rows rather than pandas objects so that callers, and the
test suite, do not inherit that dependency either.
"""

from datetime import date, datetime
from fredapi import Fred
from utils.logger import get_logger

log = get_logger("macro_data")


class MacroDataAdapter:
    def __init__(self, api_key: str):
        self.fred = Fred(api_key=api_key) if api_key else None

    # --- ALFRED vintages (Spec O section 4.1) ---------------------------

    def get_series_all_releases(self, series_id: str) -> list[dict]:
        """Every (reference period, release date, value) triple ALFRED holds.

        Returned as plain dicts with ISO dates:
        ``{"reference_date": "2024-02-01", "release_date": "2024-03-12",
        "value": 3.2}``. A ``value`` of ``None`` is ALFRED's "." — the series
        existed but had no observation for that period in that vintage, which
        is a different fact from the period not existing yet.

        ``fredapi`` names the release-date column ``realtime_start``. That is
        the date the value became the current value, and it is a **date**, not
        a timestamp — hence ``precision='day'`` on every macro observation.
        """
        if self.fred is None:
            return []
        frame = self.fred.get_series_all_releases(series_id)
        return _rows_from_frame(frame, series_id)

    def get_series_as_of_date(self, series_id: str, as_of: date) -> list[dict]:
        """The series **as it stood** on ``as_of``.

        ``fredapi.get_series_as_of_date`` returns every vintage up to the date;
        collapsing those to one value per reference period is
        ``macro.vintage``'s job, because the collapse rule (latest release on
        or before ``as_of`` wins) is a point-in-time rule and belongs where the
        other ones are.
        """
        if self.fred is None:
            return []
        frame = self.fred.get_series_as_of_date(series_id, _as_datetime(as_of))
        return _rows_from_frame(frame, series_id)

    def get_fed_funds_rate(self) -> dict:
        """Current federal funds effective rate."""
        try:
            data = self.fred.get_series("DFF", observation_start="2024-01-01")
            current = float(data.dropna().iloc[-1])
            prev_month = float(data.dropna().iloc[-22]) if len(data.dropna()) > 22 else current
            return {
                "rate": round(current, 2),
                "prev_month": round(prev_month, 2),
                "direction": "rising" if current > prev_month else "falling" if current < prev_month else "stable",
            }
        except Exception as e:
            log.error("fed_funds_failed", error=str(e))
            return {"rate": 5.0, "prev_month": 5.0, "direction": "stable"}

    def get_yield_curve(self) -> dict:
        """2Y/10Y Treasury spread."""
        try:
            t10y = self.fred.get_series("DGS10", observation_start="2024-01-01")
            t2y = self.fred.get_series("DGS2", observation_start="2024-01-01")
            t10_current = float(t10y.dropna().iloc[-1])
            t2_current = float(t2y.dropna().iloc[-1])
            spread = t10_current - t2_current
            # Check historical spread for trend
            t10_prev = float(t10y.dropna().iloc[-22]) if len(t10y.dropna()) > 22 else t10_current
            t2_prev = float(t2y.dropna().iloc[-22]) if len(t2y.dropna()) > 22 else t2_current
            prev_spread = t10_prev - t2_prev
            return {
                "t10y": round(t10_current, 3),
                "t2y": round(t2_current, 3),
                "spread": round(spread, 3),
                "inverted": spread < 0,
                "steepening": spread > prev_spread,
                "prev_spread": round(prev_spread, 3),
            }
        except Exception as e:
            log.error("yield_curve_failed", error=str(e))
            return {"spread": 0.0, "inverted": False, "steepening": False}

    def get_credit_spreads(self) -> dict:
        """Investment-grade credit spread (OAS)."""
        try:
            # ICE BofA US Corporate Index OAS
            oas = self.fred.get_series("BAMLC0A0CM", observation_start="2024-01-01")
            current = float(oas.dropna().iloc[-1])
            prev_month = float(oas.dropna().iloc[-22]) if len(oas.dropna()) > 22 else current
            ma_60 = float(oas.dropna().tail(60).mean()) if len(oas.dropna()) >= 60 else current
            return {
                "oas": round(current, 2),
                "prev_month": round(prev_month, 2),
                "ma_60": round(ma_60, 2),
                "widening": current > prev_month,
                "elevated": current > ma_60 * 1.2,
            }
        except Exception as e:
            log.error("credit_spreads_failed", error=str(e))
            return {"oas": 1.0, "widening": False, "elevated": False}

    def get_all_macro_inputs(self) -> dict:
        """Aggregate all macro data for the regime agent."""
        return {
            "fed_funds": self.get_fed_funds_rate(),
            "yield_curve": self.get_yield_curve(),
            "credit_spreads": self.get_credit_spreads(),
        }


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return value


def _iso(value) -> str | None:
    """A pandas Timestamp, a datetime, a date or an ISO string -> ISO date."""
    if value is None:
        return None
    for attribute in ("date",):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method().isoformat()
            except TypeError:
                pass
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _float_or_none(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number      # NaN -> None


def _rows_from_frame(frame, series_id: str) -> list[dict]:
    """``fredapi``'s DataFrame -> plain rows, sorted and deduplicated.

    Kept defensive on purpose: the shape of what ``fredapi`` returns is the
    one thing in this path that a pinned-but-unmaintained dependency can
    change under us, and a silently empty result would look exactly like "the
    series had not been released yet".
    """
    if frame is None:
        return []
    columns = {str(c).lower() for c in getattr(frame, "columns", [])}
    required = {"date", "realtime_start", "value"}
    if not required <= columns:
        log.error(
            "fred_vintage_shape_changed", series=series_id, columns=sorted(columns)
        )
        raise ValueError(
            f"fredapi returned columns {sorted(columns)} for {series_id}; "
            f"{sorted(required)} are required to build a vintage. Refusing to "
            "guess: a wrong release date is undetectable lookahead."
        )

    rows = []
    for record in frame.to_dict("records"):
        rows.append(
            {
                "reference_date": _iso(record.get("date")),
                "release_date": _iso(record.get("realtime_start")),
                "value": _float_or_none(record.get("value")),
            }
        )
    rows = [r for r in rows if r["reference_date"] and r["release_date"]]
    rows.sort(key=lambda r: (r["reference_date"], r["release_date"]))
    return rows
