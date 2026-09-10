"""`insider_cluster_v1` — declared, frozen, hashed, and refused until Phase 4.

The fact it conditions on, ``insider_cluster_count``, comes from the Form 4
plane, which Spec N §4.0 puts in Phase 4: *"Phase 3's first checkpoint lands
exactly those three [SEC feeds]; Phase 4 builds the full planes (13D/G, Form 4,
entity history, news) on top of it."*

It ships now, refusing, rather than later, for two reasons.

**A refusal is not an `insufficient`.** `insufficient` means the engine looked
and the cohort was too small or too clustered to answer; `pending_plane` means
the evidence does not exist yet. Rounding the second into the first would put a
"we found nothing" in front of a reader when the truth is "we have not looked".

**The trial budget starts now.** The setup is content-hashed and carries its
family slug today, so when the Form 4 facts land, the first cohort built from
it is trial one of a family whose definition was fixed before anybody saw a
result — which is the entire point of §7.
"""

from __future__ import annotations

from comparables.setup_spec import Condition, SetupSpec
from comparables.setups import RosterEntry, SOURCE_PENDING

SPEC = SetupSpec(
    slug="insider_cluster_v1",
    version="1.0.0",
    conditions=(
        Condition("insider_cluster_count", ">=", 3),
        Condition("dollar_volume_20d", ">", 5_000_000.0),
    ),
    universe="liquid_us_equity_v1",
    horizons_sessions=(5, 10, 20),
    execution_policy="event_swing_14cal_v1",
    match_covariates=("market_cap_decile", "liquidity_decile", "realized_vol_decile"),
    lookback_years=5,
)

PENDING_REASON = (
    "pending_plane: insider_cluster_v1 conditions on insider_cluster_count, "
    "which comes from the Form 4 plane (Spec N §4.0 — Phase 4). No Form 4 "
    "facts are in source_observations, so there is nothing to qualify against. "
    "This is a refusal because the evidence does not exist yet, not an "
    "`insufficient` answer about a cohort that was looked at and found too "
    "small."
)

ENTRY = RosterEntry(
    spec=SPEC,
    candidate_source=SOURCE_PENDING,
    required_fact_types=("insider_cluster_count",),
    tunable=(
        ("insider_cluster_count", "insider_cluster_count"),
        ("dollar_volume_20d", "dollar_volume_20d"),
    ),
    description=(
        "Three or more insider open-market buys clustered in a window on a "
        "liquid name. Frozen and hashed now; refused until the Phase 4 Form 4 "
        "plane supplies the fact."
    ),
    available=False,
    unavailable_reason=PENDING_REASON,
)
