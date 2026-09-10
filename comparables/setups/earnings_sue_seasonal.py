"""`earnings_sue_seasonal_v1` — SUE from XBRL, timed by the 8-K (Spec N §4.0).

Two stored facts and nothing else:

* ``eps_diluted`` from XBRL ``companyfacts``, whose ``known_at_utc`` is the
  filing's ``acceptanceDateTime``, gives the **seasonal random-walk SUE** —
  actual EPS minus the same quarter a year earlier, scaled by the standard
  deviation of that difference. Spec N §4.0 is explicit about why it is not an
  analyst-consensus surprise: no retail source offers a verifiable
  point-in-time consensus archive, and a restated consensus used as a pre-print
  fact is lookahead in the direction that flatters the engine. This proxy is
  weaker and the literature says so. It is honest, free and reproducible.
* ``earnings_release_8k_item_202``, whose ``acceptanceDateTime`` is the
  **announcement instant**, exact to the second in UTC. An event dated from
  ``filingDate`` is rejected outright — by `filings/observations.py` at write
  time and by `comparables/cohort.py` at read time.

`consensus_eps_news` (§4.0, the deterministic extractor over timestamped
articles) is a better fact where it exists and is not in this setup: the
extractor is Phase 4 work. Adding it later is a **new slug**, not a silent
redefinition of this one.
"""

from __future__ import annotations

from comparables.setup_spec import Condition, SetupSpec
from comparables.setups import RosterEntry, SOURCE_EARNINGS_8K

SPEC = SetupSpec(
    slug="earnings_sue_seasonal_v1",
    version="1.0.0",
    conditions=(
        Condition("sue_seasonal", ">", 1.5),
        Condition("dollar_volume_20d", ">", 10_000_000.0),
    ),
    universe="liquid_us_equity_v1",
    horizons_sessions=(1, 3, 5, 10, 20),
    execution_policy="event_swing_14cal_v1",
    match_covariates=(
        "market_cap_decile", "liquidity_decile", "realized_vol_decile",
    ),
    lookback_years=5,
)

ENTRY = RosterEntry(
    spec=SPEC,
    candidate_source=SOURCE_EARNINGS_8K,
    required_fact_types=("earnings_release_8k_item_202", "eps_diluted"),
    tunable=(
        ("sue_seasonal", "sue_seasonal"),
        ("dollar_volume_20d", "dollar_volume_20d"),
    ),
    description=(
        "A liquid name whose seasonal random-walk SUE clears a threshold, "
        "timed to the second by the 8-K Item 2.02 acceptance timestamp. "
        "No analyst consensus is involved: none is available point-in-time."
    ),
)
