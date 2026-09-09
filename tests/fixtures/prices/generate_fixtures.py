"""Regenerate the committed price-plane fixtures. Not run by the test suite.

    python tests/fixtures/prices/generate_fixtures.py

Everything here is **synthetic**. The two delisted names carry real tickers
(`BBBY`, `RAD`) only so that the offline delisting audit exercises the real
`data/prices/delisting_audit_list.py` rows; the prices are invented, and no
vendor series is committed anywhere in this repo (Spec K §3.3).

The construction guarantees the Spec N §4.3 identity by design: a synthetic
split-adjusted path is chosen first, and raw closes are `path x forward split
factor`. A fixture that only approximately satisfies the identity would make
`test_series_reconstruct_from_factors` a test of the fixture, not of the code.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

START = date(2023, 1, 3)
N_SESSIONS = 80


def business_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS = business_days(START, N_SESSIONS)


def rounded(value: float) -> float:
    return round(value + 1e-12, 6)


# name -> (uid, venue, exchange, n_sessions, base, drift, volume, splits, dividends, delisting)
# `splits` and `dividends` are {session index: value}.
SPECS = {
    "ALPH": dict(
        uid="fx-0001", venue="nyse_amex", exchange="NYSE", sessions=N_SESSIONS,
        base=100.0, drift=0.0012, wobble=0.9, volume=1_800_000,
        splits={40: 2.0}, dividends={20: 0.50, 60: 0.55},
        listing=date(2011, 5, 2), delisting=None, reason="unknown",
    ),
    "BETA": dict(
        uid="fx-0002", venue="nasdaq", exchange="NASDAQ", sessions=N_SESSIONS,
        base=42.0, drift=-0.0004, wobble=0.5, volume=900_000,
        splits={}, dividends={}, listing=date(2014, 9, 18), delisting=None, reason="unknown",
    ),
    "GAMM": dict(
        uid="fx-0003", venue="nasdaq", exchange="NASDAQ", sessions=N_SESSIONS,
        base=210.0, drift=0.0008, wobble=1.8, volume=3_400_000,
        splits={}, dividends={35: 0.20}, listing=date(2009, 3, 5), delisting=None, reason="unknown",
    ),
    "DELT": dict(
        uid="fx-0004", venue="nyse_amex", exchange="NYSEAMERICAN", sessions=N_SESSIONS,
        base=7.5, drift=0.0002, wobble=0.09, volume=140_000,
        splits={}, dividends={}, listing=date(2018, 1, 22), delisting=None, reason="unknown",
    ),
    "EPSI": dict(
        uid="fx-0005", venue="nasdaq", exchange="NASDAQ", sessions=N_SESSIONS,
        base=63.0, drift=0.0003, wobble=0.7, volume=520_000,
        splits={}, dividends={}, listing=date(2021, 11, 4), delisting=None, reason="unknown",
    ),
    # The collapse case: the series ends at a fraction of where it traded, which
    # is what a vendor that carries the terminal decline looks like.
    "BBBY": dict(
        uid="fx-0006", venue="nasdaq", exchange="NASDAQ", sessions=64,
        base=18.0, drift=-0.0009, wobble=0.4, volume=2_100_000,
        splits={}, dividends={}, listing=date(1992, 7, 28),
        delisting=SESSIONS[63], reason="performance", collapse=True,
    ),
    # The stop case: the series simply ends at an ordinary quote. A file that
    # looks survivorship-free and still omits the terminal loss (Spec N §4.2).
    "RAD": dict(
        uid="fx-0007", venue="nyse_amex", exchange="NYSE", sessions=58,
        base=11.0, drift=-0.0011, wobble=0.3, volume=1_450_000,
        splits={}, dividends={}, listing=date(1968, 1, 2),
        delisting=SESSIONS[57], reason="performance", collapse=False,
    ),
}


def adjusted_path(spec: dict) -> list[float]:
    n = spec["sessions"]
    base, drift, wobble = spec["base"], spec["drift"], spec["wobble"]
    path = []
    for i in range(n):
        # Deterministic, no RNG: a linear drift plus a fixed triangular wobble.
        wave = ((i * 7) % 11 - 5) / 5.0
        value = base * (1.0 + drift * i) + wobble * wave
        if spec.get("collapse") and i >= n - 10:
            # A 92% terminal decline over the last ten sessions.
            steps = i - (n - 10) + 1
            value = value * (0.75 ** steps)
        path.append(max(value, 0.01))
    return path


def forward_factors(splits: dict[int, float], n: int) -> list[float]:
    factors = [1.0] * n
    running = 1.0
    for i in range(n - 1, -1, -1):
        factors[i] = running
        running *= splits.get(i, 1.0)
    return factors


def main() -> None:
    securities, bars, actions = [], [], []

    for ticker, spec in SPECS.items():
        n = spec["sessions"]
        path = adjusted_path(spec)
        factors = forward_factors(spec["splits"], n)

        securities.append({
            "security_uid": spec["uid"],
            "ticker": ticker,
            "name": f"{ticker} Fixture Corp",
            "exchange": spec["exchange"],
            "venue": spec["venue"],
            "ticker_valid_from": spec["listing"].isoformat(),
            "ticker_valid_to": "",
            "listing_date": spec["listing"].isoformat(),
            "delisting_date": spec["delisting"].isoformat() if spec["delisting"] else "",
            "delisting_reason": spec["reason"],
        })

        for i in range(n):
            raw_close = rounded(path[i] * factors[i])
            spread = max(raw_close * 0.006, 0.01)
            bars.append({
                "security_uid": spec["uid"],
                "ticker": ticker,
                "session_date": SESSIONS[i].isoformat(),
                "raw_open": rounded(raw_close - spread * 0.4),
                "raw_high": rounded(raw_close + spread),
                "raw_low": rounded(raw_close - spread),
                "raw_close": raw_close,
                "volume": float(spec["volume"] + (i % 7) * 10_000),
                "split_factor": spec["splits"].get(i, 1.0),
                "dividend_cash": spec["dividends"].get(i, 0.0),
            })

        for i, factor in sorted(spec["splits"].items()):
            actions.append({
                "security_uid": spec["uid"], "ticker": ticker,
                "ex_date": SESSIONS[i].isoformat(), "action_type": "split", "value": factor,
            })
        for i, cash in sorted(spec["dividends"].items()):
            actions.append({
                "security_uid": spec["uid"], "ticker": ticker,
                "ex_date": SESSIONS[i].isoformat(), "action_type": "dividend", "value": cash,
            })
        if spec["delisting"]:
            actions.append({
                "security_uid": spec["uid"], "ticker": ticker,
                "ex_date": spec["delisting"].isoformat(),
                "action_type": "delisting", "value": "",
            })

    # Membership: GAMM joins the index part-way through, which is what
    # test_universe_membership_is_point_in_time checks is not back-dated.
    membership = [
        {"universe_slug": "sp500_fixture_v1", "security_uid": "fx-0001", "ticker": "ALPH",
         "member_from": SESSIONS[0].isoformat(), "member_to": "",
         "known_at_utc": f"{SESSIONS[0].isoformat()}T21:00:00+00:00"},
        {"universe_slug": "sp500_fixture_v1", "security_uid": "fx-0002", "ticker": "BETA",
         "member_from": SESSIONS[0].isoformat(), "member_to": SESSIONS[50].isoformat(),
         "known_at_utc": f"{SESSIONS[0].isoformat()}T21:00:00+00:00"},
        {"universe_slug": "sp500_fixture_v1", "security_uid": "fx-0003", "ticker": "GAMM",
         "member_from": SESSIONS[40].isoformat(), "member_to": "",
         "known_at_utc": f"{SESSIONS[40].isoformat()}T21:00:00+00:00"},
        {"universe_slug": "sp500_fixture_v1", "security_uid": "fx-0006", "ticker": "BBBY",
         "member_from": SESSIONS[0].isoformat(), "member_to": SESSIONS[63].isoformat(),
         "known_at_utc": f"{SESSIONS[0].isoformat()}T21:00:00+00:00"},
    ]

    _write("securities.csv", securities)
    _write("bars.csv", bars)
    _write("corporate_actions.csv", actions)
    _write("universe_membership.csv", membership)
    print(f"{len(securities)} securities, {len(bars)} bars, {len(actions)} actions")


def _write(name: str, rows: list[dict]) -> None:
    path = HERE / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
