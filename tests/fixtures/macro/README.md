# ALFRED vintage fixtures

Every test in `tests/test_macro_plane.py` reads these through
`macro.vintage.FixtureVintageSource`, so the suite runs with no network and no
FRED API key.

## These are synthetic, and that is a compromise, not a preference

`api.stlouisfed.org` is refused at this environment's egress proxy (`403` to
`CONNECT`), so no ALFRED response could be recorded. The numbers are invented.

**The structure is not.** A revised series carries several releases per
reference period with later `release_date` values, a print does not exist
before its release date, and the NBER recession indicator is declared long
after the fact — and that structure is the entire thing under test. A fixture
whose numbers were real but whose release dates were not would test nothing;
a fixture whose numbers are invented but whose release lag and restatements are
faithful tests exactly the rules.

Each file is:

```json
{"series_id": "CPIAUCSL",
 "rows": [{"reference_date": "2024-01-01",
           "release_date": "2024-02-13",
           "value": 300.75}]}
```

which is both the shape `macro.vintage.FixtureVintageSource` reads and the
shape `data.macro_data.MacroDataAdapter.get_series_all_releases` returns. The
two are the same on purpose: a recording drops straight in.

## Replacing them with real recordings

On any machine that can reach FRED:

```bash
export FRED_API_KEY=your_key
python -m scripts.record_macro_fixtures --series CPIAUCSL USREC SP500 VIXCLS DGS10 DGS3MO
```

ALFRED uses the same key as FRED, and it is already provisioned as
`FRED_API_KEY`.

## What each fixture is for

| Series | Exercises |
|---|---|
| `CPIAUCSL` | The revised case. Each month is first published mid-way through the next month and then restated twice — once a month later, once at the following January's seasonal-factor revision. `test_macro_vintage_absent_before_release` and `test_macro_vintage_differs_from_current`. |
| `USREC` | The vintage-only case. Contemporaneous vintages say "no recession"; a 2025-09-15 vintage declares one that began in April 2024. Reading the current series to label mid-2024 is the error `test_usrec_only_via_vintage` prevents. |
| `SP500`, `VIXCLS`, `DGS10`, `DGS3MO`, `DGS2` | The never-revised set: one release per observation, on the observation date. Two regimes in one timeline — a calm inverted-curve uptrend through 2024 (`late_cycle`), then a drawdown with a volatility spike in early 2025 (`crisis`). |

Regenerate with `python tests/fixtures/macro/make_fixtures.py` from the
repository root.
