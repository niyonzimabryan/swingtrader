"""The citation rule and the §6.6 scaling inputs, on the ledger side of the boundary.

Spec L §6.6 gives a proposal at most one ``cohort_answer_id``, and four
conditions it has to satisfy before the proposal may draw on the **evidenced**
budget:

1. it resolves, through Phase 2's citation seam (:mod:`research_workspace.citations`);
2. ``assert_citable`` accepts it — so ``depth="full"``, never ``quick``, never
   ``insufficient``;
3. ``status == "ok"`` — ``inconclusive`` is a real finding and is not a citation;
4. it is for **this ticker** and was computed within the last
   ``CITATION_MAX_AGE_SESSIONS`` trading sessions.

Everything that fails any of those is not an error. In ``advisory`` mode — the
owner's decision of 2026-09-08 and the default — it re-labels the proposal
``discretionary``, prints the reason, and draws from the separate discretionary
budget. **Nothing sizes to zero on evidence alone.** ``strict`` mode restores
zero-sizing without a code change.

The one thing this module will not do is *substitute* a statistic. §6.6 names
one number pair — the lower 90% bootstrap bound and the point estimate of the
**policy-simulated net return** (Spec N §5.3) at the horizon nearest the
proposal's expected hold — and if the answer does not carry that pair, the
answer to "what is ``m``?" is "unknown", not "here is a different interval that
was to hand". A market-adjusted CAR interval is a different quantity measured
under a different exit rule, and quietly sizing from it would be a model
characterising a statistic (Spec N §9) with extra steps. An answer with no
policy lower bound is therefore ``discretionary`` with that reason stated, which
is both honest and the smaller of the two budgets.

**Known gap, stated rather than papered over.** As of this phase
``comparables.report.PolicySummary`` carries ``net`` (the point estimate) and no
interval, so a real Spec N answer reaches :func:`policy_bounds` with a point
estimate and no bound and lands in ``discretionary``. The extractor already
accepts the interval under any of :data:`POLICY_INTERVAL_FIELDS` the moment
Phase 3c adds one, at which point evidenced sizing starts working with no change
here. This is recorded in ``docs/EXECUTION_LIFECYCLE.md`` under "Needs owner".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

#: Names an interval on the policy summary may be published under. Whichever is
#: present must expose ``lower``, ``estimate`` and ``level`` — the shape of
#: ``comparables.inference.ConfidenceInterval``.
POLICY_INTERVAL_FIELDS: tuple[str, ...] = ("net_ci", "net_interval", "net_bootstrap_ci")

#: Spec L §6.6 says the **lower 90%** bound. An interval at another level is a
#: different number and is refused rather than relabelled.
REQUIRED_INTERVAL_LEVEL = 0.90
INTERVAL_LEVEL_TOLERANCE = 1e-6

#: ``advisory`` (default) versus ``strict`` (Spec L §6.6).
ADVISORY = "advisory"
STRICT = "strict"

EVIDENCED = "evidenced"
DISCRETIONARY = "discretionary"

#: Reason codes. Stable strings — they are printed on the card and are what a
#: later "how did my judgment trades do" query groups by.
NO_CITATION = "no_citation"
UNRESOLVABLE = "unresolvable_cohort_answer"
NOT_CITABLE = "not_citable"
NOT_OK = "citation_status_not_ok"
WRONG_TICKER = "citation_other_ticker"
STALE_CITATION = "citation_stale"
NO_HORIZON = "citation_no_horizon"
NO_POLICY_BOUND = "citation_no_policy_lower_bound"
NONPOSITIVE_POINT_ESTIMATE = "citation_nonpositive_point_estimate"
NONPOSITIVE_LOWER_BOUND = "citation_nonpositive_lower_bound"
ACCEPTED = "evidenced"


class CitationRefused(Exception):
    """A citation that cannot back the evidenced budget, with the reason.

    Raised only where the caller wants to stop; :func:`assess` returns the same
    information as data, because in ``advisory`` mode nothing stops.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class EvidenceAssessment:
    """What §6.6 has to know, and what the card has to print.

    ``budget`` is the honest label after the rule ran. ``multiplier`` is
    ``m = clip(LB / PE, 0, 1)`` for an evidenced proposal and ``None``
    otherwise — never a silent 1.0, because a 1.0 would read as "the evidence
    supported full size".
    """

    budget: str
    reason_code: str
    reason: str
    cohort_answer_id: str = ""
    lower_bound: float | None = None
    point_estimate: float | None = None
    horizon_sessions: int | None = None
    multiplier: float | None = None
    gate_mode: str = ADVISORY
    #: ``strict`` mode only: the evidence says size to zero (``LB <= 0``).
    sizes_to_zero: bool = False
    #: ``strict`` mode only: the proposal is refused outright — an uncited
    #: proposal, or a citation that is not ``full``/``ok``/same-ticker/recent.
    #: In ``advisory`` mode this is always false and the proposal is written as
    #: ``discretionary`` with the reason on the card.
    refused: bool = False

    @property
    def is_evidenced(self) -> bool:
        return self.budget == EVIDENCED

    def as_dict(self) -> dict:
        return {
            "budget": self.budget,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "cohort_answer_id": self.cohort_answer_id or None,
            "lower_bound": self.lower_bound,
            "point_estimate": self.point_estimate,
            "horizon_sessions": self.horizon_sessions,
            "multiplier": self.multiplier,
            "evidence_gate_mode": self.gate_mode,
            "sizes_to_zero_on_evidence": self.sizes_to_zero,
            "refused_by_evidence_gate": self.refused,
        }


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #


def _naive_utc(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    return value


def answer_ticker(answer) -> str:
    """The ticker a Spec N answer is about, or ``""`` when it does not say.

    A ``SetupSpec`` is a *pattern*, not a name, so the ticker rides on the
    answer's setup where the engine put it. Several field names are accepted
    because the store that resolves an id is Phase 3c's and is being built in
    parallel; an answer that names no ticker fails the same-ticker check rather
    than passing it by default.
    """
    setup = getattr(answer, "setup", None)
    for holder in (answer, setup):
        if holder is None:
            continue
        for field in ("ticker", "symbol", "subject_ticker"):
            value = getattr(holder, field, None)
            if value:
                return str(value).strip().upper()
    return ""


def answer_as_of(answer) -> date | None:
    """The date the answer was computed for, or ``None``."""
    for holder in (answer, getattr(answer, "setup", None)):
        if holder is None:
            continue
        for field in ("as_of", "as_of_date", "computed_on", "as_of_utc"):
            value = getattr(holder, field, None)
            if isinstance(value, datetime):
                return _naive_utc(value).date()
            if isinstance(value, date):
                return value
    return None


def sessions_between(earlier: date, later: date) -> int:
    """Trading sessions between two dates, weekends excluded, holidays not.

    Deliberately the same crude weekday count :mod:`portfolio.settlement` uses:
    over a five-session budget a market holiday moves the boundary by one day,
    and the direction of that error is to call a citation *older* than it is,
    which fails toward the discretionary budget rather than away from it.
    """
    if later <= earlier:
        return 0
    sessions = 0
    cursor = earlier
    while cursor < later:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            sessions += 1
    return sessions


def _nearest_horizon(answer, expected_hold_sessions: int | None):
    """The ``HorizonResult`` nearest the proposal's expected hold.

    Ties break to the **shorter** horizon: a swing trade held to a longer
    horizon than it planned is the common case, and the shorter horizon's
    estimate is the more conservative of two equally-distant ones.
    """
    horizons = tuple(getattr(answer, "horizons", ()) or ())
    if not horizons:
        return None
    if expected_hold_sessions is None:
        return horizons[0]
    target = int(expected_hold_sessions)
    return min(
        horizons,
        key=lambda h: (abs(int(getattr(h, "horizon_sessions", 0)) - target),
                       int(getattr(h, "horizon_sessions", 0))),
    )


def policy_bounds(answer, expected_hold_sessions: int | None):
    """``(lower_bound, point_estimate, horizon_sessions)`` for the policy net.

    Returns the point estimate whenever the answer carries one and ``None`` for
    the bound when it carries no 90% interval on that same quantity. The caller
    decides what to do with a missing bound; this function never fills one in
    from a different statistic.

    Raises :class:`CitationRefused` only when there is no policy summary at all
    at the chosen horizon, which means the answer is not the shape §6.6 names.
    """
    horizon = _nearest_horizon(answer, expected_hold_sessions)
    if horizon is None:
        raise CitationRefused(
            NO_HORIZON,
            "the cited answer carries no horizon results, so there is no "
            "policy-simulated net return to scale from (Spec L §6.6).",
        )
    horizon_sessions = int(getattr(horizon, "horizon_sessions", 0)) or None
    policy = getattr(horizon, "policy", None)
    if policy is None:
        raise CitationRefused(
            NO_HORIZON,
            f"the cited answer's {horizon_sessions}-session horizon carries no "
            "policy summary; §6.6 scales from the policy-simulated net return, "
            "not from the raw or market-adjusted one.",
        )
    point_estimate = getattr(policy, "net", None)
    point_estimate = float(point_estimate) if point_estimate is not None else None

    lower = None
    for field in POLICY_INTERVAL_FIELDS:
        interval = getattr(policy, field, None)
        if interval is None:
            continue
        level = getattr(interval, "level", None)
        if level is None or abs(float(level) - REQUIRED_INTERVAL_LEVEL) > INTERVAL_LEVEL_TOLERANCE:
            # A different confidence level is a different number. Say nothing
            # rather than relabel it.
            continue
        bound = getattr(interval, "lower", None)
        if bound is not None:
            lower = float(bound)
            break
    return lower, point_estimate, horizon_sessions


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def gate_mode(settings) -> str:
    raw = str(getattr(settings, "evidence_gate_mode", ADVISORY) or ADVISORY).strip().lower()
    return STRICT if raw == STRICT else ADVISORY


def assess(
    *,
    ticker: str,
    cohort_answer_id: str,
    expected_hold_sessions: int | None,
    on_date: date,
    settings,
    resolver=None,
) -> EvidenceAssessment:
    """Run the §6.6 citation rule and return the honest budget label.

    ``resolver`` defaults to :func:`research_workspace.citations.resolve`, the
    Phase 2 seam Phase 3c registers ``compare_setups`` answers with. It is an
    argument so a test can supply a fake one without touching a process-global.
    """
    mode = gate_mode(settings)
    symbol = (ticker or "").strip().upper()
    answer_id = (cohort_answer_id or "").strip()

    if not answer_id:
        return _unevidenced(
            mode,
            NO_CITATION,
            "no cohort_answer_id was cited, so this proposal is not evidenced. "
            "In advisory mode it draws from the discretionary budget "
            "(Spec L §6.6); nothing is suppressed for lack of evidence.",
        )

    if resolver is None:
        from research_workspace import citations

        resolver = citations.resolve

    answer = resolver(answer_id)
    if answer is None:
        return _unevidenced(
            mode,
            UNRESOLVABLE,
            f"cohort answer {answer_id!r} cannot be resolved, so its depth and "
            "status cannot be checked. An unverifiable citation is not a "
            "citation (Spec N §8).",
            answer_id,
        )

    from comparables.report import NotCitableError, assert_citable

    try:
        full = assert_citable(answer)
    except NotCitableError as exc:
        return _unevidenced(mode, NOT_CITABLE, str(exc), answer_id)

    status = getattr(full, "status", "")
    if status != "ok":
        return _unevidenced(
            mode,
            NOT_OK,
            f"a depth='full' answer with status={status!r} is not a citation: "
            f"{getattr(full, 'refusal_reason', None) or 'no effect was established'}.",
            answer_id,
        )

    cited_ticker = answer_ticker(full)
    if cited_ticker != symbol:
        return _unevidenced(
            mode,
            WRONG_TICKER,
            f"the cited answer is for {cited_ticker or 'an unnamed ticker'} and "
            f"this proposal is for {symbol}. Evidence for one name does not "
            "size a position in another (Spec L §6.6).",
            answer_id,
        )

    as_of = answer_as_of(full)
    max_age = int(getattr(settings, "citation_max_age_sessions", 5) or 5)
    if as_of is None:
        return _unevidenced(
            mode,
            STALE_CITATION,
            "the cited answer does not say when it was computed, so its age "
            f"against the {max_age}-session budget cannot be checked.",
            answer_id,
        )
    age = sessions_between(as_of, on_date)
    if age > max_age:
        return _unevidenced(
            mode,
            STALE_CITATION,
            f"the cited answer was computed on {as_of.isoformat()}, {age} "
            f"sessions ago; the budget is {max_age}. Re-run compare_setups.",
            answer_id,
        )

    try:
        lower, point, horizon = policy_bounds(full, expected_hold_sessions)
    except CitationRefused as exc:
        return _unevidenced(mode, exc.code, exc.message, answer_id)

    if point is None:
        return _unevidenced(
            mode,
            NO_POLICY_BOUND,
            "the cited answer carries no policy-simulated net return at the "
            "horizon nearest this hold, so there is no point estimate to "
            "scale from.",
            answer_id,
            horizon_sessions=horizon,
        )
    if lower is None:
        return _unevidenced(
            mode,
            NO_POLICY_BOUND,
            "the cited answer carries no lower 90% bound on the "
            "policy-simulated net return. §6.6 scales from that bound and this "
            "code will not substitute a different interval for it, so the "
            "proposal is discretionary with the point estimate printed.",
            answer_id,
            point_estimate=point,
            horizon_sessions=horizon,
        )
    if point <= 0:
        return _unevidenced(
            mode,
            NONPOSITIVE_POINT_ESTIMATE,
            f"the cited answer's policy-simulated net return is {point:+.4f} at "
            f"{horizon} sessions — not positive, so there is no edge to scale.",
            answer_id,
            lower_bound=lower,
            point_estimate=point,
            horizon_sessions=horizon,
        )
    if lower <= 0:
        return _unevidenced(
            mode,
            NONPOSITIVE_LOWER_BOUND,
            f"the cited answer's lower 90% bound is {lower:+.4f} against a "
            f"point estimate of {point:+.4f} at {horizon} sessions: the "
            "evidence does not exclude zero. Advisory mode re-labels this "
            "discretionary and prints the bound; strict mode sizes it to zero.",
            answer_id,
            lower_bound=lower,
            point_estimate=point,
            horizon_sessions=horizon,
        )

    multiplier = min(1.0, max(0.0, lower / point))
    return EvidenceAssessment(
        budget=EVIDENCED,
        reason_code=ACCEPTED,
        reason=(
            f"cited answer {answer_id} is depth='full', status='ok', for "
            f"{symbol}, {age} session(s) old. Lower 90% bound {lower:+.4f} over "
            f"point estimate {point:+.4f} at {horizon} sessions gives "
            f"m={multiplier:.4f}."
        ),
        cohort_answer_id=answer_id,
        lower_bound=lower,
        point_estimate=point,
        horizon_sessions=horizon,
        multiplier=multiplier,
        gate_mode=mode,
    )


def _unevidenced(
    mode: str,
    code: str,
    reason: str,
    cohort_answer_id: str = "",
    *,
    lower_bound: float | None = None,
    point_estimate: float | None = None,
    horizon_sessions: int | None = None,
) -> EvidenceAssessment:
    """The discretionary label, and what ``strict`` mode does differently.

    In ``advisory`` mode — the default, and the owner's 2026-09-08 decision —
    every branch here is the same answer: ``discretionary``, with the reason
    printed on the card and the evidence shown in full including a negative
    lower bound. Nothing is refused and nothing sizes to zero for want of
    evidence.

    ``strict`` mode splits the same reasons two ways, which is the whole
    difference between the two modes and the reason the flag exists (Spec L
    §6.6): ``LB <= 0`` **sizes to zero**, and an uncited proposal or a citation
    that is not ``full``/``ok``/same-ticker/recent is **refused** — which is the
    ``risk_rejected`` outcome the Spec L §8 row
    ``test_citation_must_be_full_ok_recent`` describes. Both branches are
    reachable by flipping one setting and neither needs a code change, which is
    what the owner asked for.
    """
    sizes_to_zero = mode == STRICT and code in (
        NONPOSITIVE_LOWER_BOUND,
        NONPOSITIVE_POINT_ESTIMATE,
    )
    refused = mode == STRICT and not sizes_to_zero
    return EvidenceAssessment(
        budget=DISCRETIONARY,
        reason_code=code,
        reason=reason,
        cohort_answer_id=cohort_answer_id,
        lower_bound=lower_bound,
        point_estimate=point_estimate,
        horizon_sessions=horizon_sessions,
        multiplier=None,
        gate_mode=mode,
        sizes_to_zero=sizes_to_zero,
        refused=refused,
    )
