"""Rebuild the synthetic ALFRED vintage fixtures in this directory.

Run from the repository root::

    python tests/fixtures/macro/make_fixtures.py

See README.md here for why these are synthetic rather than recorded, and for
``scripts/record_macro_fixtures.py``, which replaces them with real ALFRED
responses on a machine that can reach ``api.stlouisfed.org``.

Each file is ``{"series_id": ..., "rows": [{"reference_date", "release_date",
"value"}]}`` — the shape ``macro.vintage.FixtureVintageSource`` reads and the
shape ``data.macro_data.MacroDataAdapter.get_series_all_releases`` returns.

The numbers are invented. What is **not** invented is the structure: a revised
series carries several releases per reference period with later
``release_date`` values, and that structure is the whole thing under test.
"""
import json
import math
import os
from datetime import date, timedelta

OUT = os.path.dirname(os.path.abspath(__file__))


def write(series_id, rows):
    path = os.path.join(OUT, f"alfred_{series_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"series_id": series_id, "rows": rows}, handle, indent=1)
        handle.write("\n")
    print(f"  wrote alfred_{series_id}.json ({len(rows)} rows)")


def row(reference, release, value):
    return {
        "reference_date": reference.isoformat(),
        "release_date": release.isoformat(),
        "value": None if value is None else round(float(value), 6),
    }


# --- CPIAUCSL: revised, monthly, with a real release lag --------------------
#
# Each month is first published in the middle of the following month and then
# revised twice: once a month later, once at the following January's annual
# seasonal-factor revision. `series_as_of(2024-02-20)` therefore differs from
# the current print for January 2024, which is
# `test_macro_vintage_differs_from_current`.

cpi = []
level = 300.0
for month in range(1, 13):
    reference = date(2024, month, 1)
    level *= 1.0 + 0.0025 + 0.0004 * math.sin(month)
    first_release = date(2024, month, 13) + timedelta(days=31)
    cpi.append(row(reference, first_release, level))
    cpi.append(row(reference, first_release + timedelta(days=28), level * 1.0008))
    cpi.append(row(reference, date(2025, 2, 11), level * 1.0011))
write("CPIAUCSL", cpi)

# --- USREC: vintage-only, and the reason -----------------------------------
#
# NBER dated the 2024 slowdown a recession in this fixture only in 2025-09 —
# eighteen months after the fact. The current series says "recession from
# 2024-04"; on 2024-06-30 nobody could have known that, and reading USREC
# without a vintage is exactly that error.

usrec = []
for month in range(1, 13):
    reference = date(2024, month, 1)
    # The contemporaneous vintage: no recession declared.
    usrec.append(row(reference, reference + timedelta(days=40), 0))
for month in range(4, 10):
    # The retrospective declaration, published 2025-09-15.
    usrec.append(row(date(2024, month, 1), date(2025, 9, 15), 1))
write("USREC", usrec)

# --- The never-revised set: one release per observation ---------------------
#
# Daily series with a single release on the observation date itself. Two
# scenarios are baked into one timeline: a calm, rising, inverted-curve stretch
# (late_cycle) through 2024, then a drawdown with a volatility spike (crisis)
# in the first quarter of 2025.

START = date(2024, 1, 2)
SESSIONS = 460


def sessions(start, count):
    out = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = sessions(START, SESSIONS)

sp500, vix, dgs10, dgs3mo = [], [], [], []
level = 4700.0
peak = level
for index, day in enumerate(DAYS):
    if index < 380:
        # Calm uptrend: ~9%/yr with a small oscillation.
        level *= 1.00035 + 0.0004 * math.sin(index / 9.0)
        vix_level = 13.5 + 2.0 * math.sin(index / 17.0)
        ten, three = 4.25, 5.35          # inverted: 10y - 3m = -110bp
    else:
        # The break: a sharp drawdown with a volatility spike.
        step = index - 380
        level *= 0.9955 - 0.0022 * math.sin(step / 5.0)
        # 18 -> ~42 over the quarter, which is where VIX actually goes in a
        # drawdown. An unbounded ramp would put it at 130 and make the crisis
        # branch fire for a reason no market has ever produced.
        vix_level = min(42.0, 18.0 + 0.32 * step)
        ten, three = 3.60, 3.10          # curve re-steepens as the front end falls
    peak = max(peak, level)
    sp500.append(row(day, day, level))
    vix.append(row(day, day, vix_level))
    dgs10.append(row(day, day, ten))
    dgs3mo.append(row(day, day, three))

write("SP500", sp500)
write("VIXCLS", vix)
write("DGS10", dgs10)
write("DGS3MO", dgs3mo)
write("DGS2", [row(d, d, 4.60 if i < 380 else 3.30) for i, d in enumerate(DAYS)])

if __name__ == "__main__":
    print(f"wrote ALFRED fixtures to {OUT}")
