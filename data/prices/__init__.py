"""The price plane (Spec N §4.2/§4.3) — the historical price backbone.

Everything here is additive and flag-gated: `PRICE_PLANE_ENABLED` defaults to
false, and `data/market_data.py` (the incumbent yfinance path) is untouched.

  * `base`            the `PricePlane` interface and the record shapes.
  * `fixture_plane`   `FixturePricePlane`, backed by committed CSVs.
  * `sharadar`        `SharadarPricePlane`, against Nasdaq Data Link.
  * `derived`         the three series, the factors between them, and the
                      point-in-time covariates computable from stored bars.
  * `store`           persistence into the five Phase 3p tables.
  * `sp500_history`   the free MIT membership source.
  * `universes`       `liquid_us_equity_v1`, computed from `price_bars` alone.
  * `audit`           the Spec N §4.2 twenty-delisting audit.

No module here calls a model, and nothing here can reach `execution/`.
"""
