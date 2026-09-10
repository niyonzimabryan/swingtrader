"""`gap_and_go_v1` — the price-only roster entry (Spec N §4.0).

Every fact this setup conditions on comes from stored `price_bars`, so it is
the one roster entry that needs no filing at all and the one that can be built
against a price file alone.

Two conventions are worth reading before quoting a number from it.

**The gap is measured at an open, and entry is the *next* session's open.**
Spec N §5.0 puts session 0 at the first session whose open is at or after
`known_at_utc`, and entry at that open. For an 8-K accepted overnight that is
exactly right: the fact exists before the open you trade. For a gap it is not —
the gap *is* the open, so an entry at the same print is a fill nobody could
have got. The qualifying fact is therefore stamped with the ledger's own
day-precision convention (`filings.observations.day_precision_known_at`: a
dated fact is known at the close of its day), which puts session 0 on the
following session and entry at its open. The cost is one session of drift; the
alternative is a systematically optimistic entry in every cohort this setup
ever produces.

**The gap is against the split-adjusted prior close**, because a 2-for-1 split
is a -50% raw gap and not an event.
"""

from __future__ import annotations

from comparables.setup_spec import Condition, SetupSpec
from comparables.setups import RosterEntry, SOURCE_PRICE_SESSION

SPEC = SetupSpec(
    slug="gap_and_go_v1",
    version="1.0.0",
    conditions=(
        Condition("gap_pct", ">", 3.0),
        Condition("dollar_volume_20d", ">", 10_000_000.0),
        Condition("dist_from_sma50", ">", 0.0),
    ),
    universe="liquid_us_equity_v1",
    horizons_sessions=(1, 3, 5, 10, 20),
    execution_policy="event_swing_14cal_v1",
    match_covariates=("liquidity_decile", "realized_vol_decile", "price_bucket"),
    lookback_years=5,
)

ENTRY = RosterEntry(
    spec=SPEC,
    candidate_source=SOURCE_PRICE_SESSION,
    required_fact_types=(),
    tunable=(
        ("gap_pct", "gap_pct"),
        ("dollar_volume_20d", "dollar_volume_20d"),
        ("dist_from_sma50", "dist_from_sma50"),
    ),
    description=(
        "A name gaps up through its prior split-adjusted close on liquid "
        "volume while extended over its 50-day. Price-only: no filing, no "
        "share count, no vendor estimate. Entry is the open of the session "
        "after the gap."
    ),
)
