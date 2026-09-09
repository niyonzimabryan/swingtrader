"""The five invalidator types, their parameters, and the daily check job.

Spec M §4 is the load-bearing part of this phase, so the rules are stated here
in one place:

1. A thesis with no invalidator cannot leave ``draft``.
2. At least one invalidator must be machine-checkable.
3. Invalidators are set before the position; one added after entry is kept,
   flagged ``post_hoc``, and excluded from the honesty metrics.

Rules 1-3 are enforced in ``research_workspace/store.py``. This module owns the
question the job asks every day — *has it happened?* — and the answer's shape.

What triggers each type
-----------------------

``price_level``
    ``{"operator": "below"|"above", "price": 82.0, "consecutive_sessions": 3}``
    — triggers when the last *n* closes are all on the wrong side. Closes only:
    an intraday wick is not a close, and Spec M's own example says "closes
    below".

``metric_threshold``
    ``{"fact_type": "gross_margin", "operator": "below", "value": 0.45,
    "consecutive_periods": 2}`` — reads ``source_observations`` through
    ``filings.observations`` at today's cutoff, takes the latest known value
    for each of the last *n* periods, and triggers when all of them are on the
    wrong side. Point-in-time by construction: a restatement is a later row for
    the same ``valid_at`` and supersedes the original without deleting it.

``time_decay``
    ``{"deadline": "2027-03-31"}``, or ``{"quarters": 2}`` / ``{"days": 180}``
    measured from the position open date when there is one and from the
    invalidator's own ``created_at`` otherwise — triggers once that date has
    passed. Optionally ``{"unless_metric": {…metric_threshold params…}}``:
    the deadline does not trigger if the named observable *did* move the way
    the thesis said it would.

    **Ruling.** Spec M's example is "no re-rating within two quarters", which
    is a deadline plus an observable. A bare deadline triggers unconditionally
    — which is the honest reading: an unresolved thesis at its own deadline
    *is* weakened, and "give it more time" should cost a human decision rather
    than happen silently. ``unless_metric`` exists so a thesis whose observable
    demonstrably moved is not paged for nothing. Flagged for ratification in
    the Phase 2 PR.

``event``
    ``{"event_key": "guidance_cut", "match": {…}}`` — evaluated by a matcher a
    later plane registers through :func:`register_event_matcher`. With no
    matcher registered the check reports ``pending_plane``: it does not
    trigger, and it does not pretend the invalidator is being watched.

``qualitative``
    Never machine-evaluated. Created with status ``needs_human_review`` rather
    than ``armed``, so "armed" means exactly "a job is watching this", and
    surfaced for the human in every check run and every review.

**Nothing here triggers on missing data.** A price feed that is down, a fact
type with no observations, an event plane that has not shipped — each reports
its own outcome and leaves the thesis alone. A page fired because a feed broke
teaches the owner to ignore pages.

**Nothing here touches a position.** A trigger moves the thesis to ``weakened``
and pages. It creates no order and no proposal, and this module imports neither
``execution`` nor any broker adapter (``test_never_auto_closes``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from database.models import (
    MACHINE_CHECKABLE_INVALIDATOR_TYPES,
    INVALIDATOR_TYPES,
    Thesis,
    ThesisInvalidator,
)
from research_workspace import paging
from research_workspace.errors import ResearchRefused
from research_workspace.prices import default_price_source
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("research_invalidators")

PRICE_LEVEL = "price_level"
METRIC_THRESHOLD = "metric_threshold"
EVENT = "event"
TIME_DECAY = "time_decay"
QUALITATIVE = "qualitative"

#: Check outcomes. Only ``triggered`` changes anything.
TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
INSUFFICIENT_DATA = "insufficient_data"
NEEDS_HUMAN_REVIEW = "needs_human_review"
PENDING_PLANE = "pending_plane"
ALREADY_TRIGGERED = "already_triggered"

_COMPARATORS = {
    "below": lambda observed, threshold: observed < threshold,
    "below_or_equal": lambda observed, threshold: observed <= threshold,
    "above": lambda observed, threshold: observed > threshold,
    "above_or_equal": lambda observed, threshold: observed >= threshold,
}


def is_machine_checkable(type_: str) -> bool:
    return type_ in MACHINE_CHECKABLE_INVALIDATOR_TYPES


# --------------------------------------------------------------------------- #
# Parameter validation
# --------------------------------------------------------------------------- #


def _require(params: dict, key: str, type_: str):
    if key not in params:
        raise ResearchRefused(
            "invalid_invalidator_params",
            f"a {type_!r} invalidator requires {key!r}; got {sorted(params)}",
        )
    return params[key]


def _require_operator(params: dict, type_: str) -> str:
    operator = str(_require(params, "operator", type_))
    if operator not in _COMPARATORS:
        raise ResearchRefused(
            "invalid_invalidator_params",
            f"unknown operator {operator!r}; valid operators are "
            f"{sorted(_COMPARATORS)}",
        )
    return operator


def _positive_int(params: dict, key: str, default: int, type_: str) -> int:
    raw = params.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ResearchRefused(
            "invalid_invalidator_params", f"{key!r} must be an integer, got {raw!r}"
        ) from None
    if value < 1:
        raise ResearchRefused(
            "invalid_invalidator_params", f"{key!r} must be at least 1, got {value}"
        )
    return value


def _parse_date(raw, key: str) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ResearchRefused(
            "invalid_invalidator_params", f"{key!r} must be an ISO date, got {raw!r}"
        ) from None


def validate_params(type_: str, params: dict | None) -> dict:
    """Canonicalise and refuse. An unparseable invalidator is not an invalidator.

    Validation happens at write time on purpose: a check job that discovers a
    malformed parameter set at 6am is a check that silently never ran.
    """
    if type_ not in INVALIDATOR_TYPES:
        raise ResearchRefused(
            "unknown_invalidator_type",
            f"unknown invalidator type {type_!r}; valid types are "
            f"{list(INVALIDATOR_TYPES)}",
        )
    params = dict(params or {})

    if type_ == QUALITATIVE:
        # Deliberately unparameterised: the "check" is a human reading it.
        return {}

    if type_ == PRICE_LEVEL:
        operator = _require_operator(params, type_)
        try:
            price = float(_require(params, "price", type_))
        except (TypeError, ValueError):
            raise ResearchRefused(
                "invalid_invalidator_params",
                f"price must be a number, got {params.get('price')!r}",
            ) from None
        return {
            "operator": operator,
            "price": price,
            "consecutive_sessions": _positive_int(
                params, "consecutive_sessions", 1, type_
            ),
        }

    if type_ == METRIC_THRESHOLD:
        return _validate_metric_params(params, type_)

    if type_ == TIME_DECAY:
        out: dict = {}
        if "deadline" in params:
            out["deadline"] = _parse_date(params["deadline"], "deadline").isoformat()
        elif "quarters" in params:
            out["quarters"] = _positive_int(params, "quarters", 1, type_)
        elif "days" in params:
            out["days"] = _positive_int(params, "days", 1, type_)
        else:
            raise ResearchRefused(
                "invalid_invalidator_params",
                "a 'time_decay' invalidator needs one of 'deadline', 'quarters' "
                "or 'days'; a decay with no horizon can never be checked",
            )
        if params.get("unless_metric"):
            out["unless_metric"] = _validate_metric_params(
                dict(params["unless_metric"]), METRIC_THRESHOLD
            )
        return out

    if type_ == EVENT:
        event_key = str(_require(params, "event_key", type_)).strip()
        if not event_key:
            raise ResearchRefused(
                "invalid_invalidator_params", "'event_key' must be a non-empty string"
            )
        out = {"event_key": event_key}
        if params.get("match"):
            out["match"] = dict(params["match"])
        return out

    raise ResearchRefused(  # pragma: no cover - the vocabularies agree by test
        "unknown_invalidator_type", f"no parameter schema for {type_!r}"
    )


def _validate_metric_params(params: dict, type_: str) -> dict:
    fact_type = str(_require(params, "fact_type", type_)).strip()
    if not fact_type:
        raise ResearchRefused(
            "invalid_invalidator_params", "'fact_type' must be a non-empty string"
        )
    operator = _require_operator(params, type_)
    try:
        value = float(_require(params, "value", type_))
    except (TypeError, ValueError):
        raise ResearchRefused(
            "invalid_invalidator_params",
            f"value must be a number, got {params.get('value')!r}",
        ) from None
    out = {
        "fact_type": fact_type,
        "operator": operator,
        "value": value,
        "consecutive_periods": _positive_int(params, "consecutive_periods", 1, type_),
    }
    for optional in ("entity_cik", "ticker", "unit"):
        if params.get(optional):
            out[optional] = str(params[optional])
    return out


# --------------------------------------------------------------------------- #
# Event hooks — filled by later planes (Spec O)
# --------------------------------------------------------------------------- #

#: ``event_key`` -> callable(session, thesis, params, now) -> (bool, detail).
_EVENT_MATCHERS: dict[str, object] = {}


def register_event_matcher(event_key: str, matcher) -> None:
    """Register the evaluator for one ``event`` key.

    Phase 4 builds the 13D/G, Form 4 and 8-K planes; this is where they attach.
    Until one does, an ``event`` invalidator reports ``pending_plane`` rather
    than quietly reading as "not triggered", because the two mean different
    things to a human deciding whether the thesis is still being watched.
    """
    _EVENT_MATCHERS[str(event_key)] = matcher


def registered_event_keys() -> tuple[str, ...]:
    return tuple(sorted(_EVENT_MATCHERS))


def clear_event_matchers() -> None:
    """Test seam: the registry is process-global, so a test must be able to reset it."""
    _EVENT_MATCHERS.clear()


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    """One invalidator, evaluated once. ``outcome`` is the whole verdict."""

    invalidator_id: int | None
    thesis_id: int
    type: str
    outcome: str
    detail: str

    @property
    def triggered(self) -> bool:
        return self.outcome == TRIGGERED


def _result(invalidator, outcome, detail) -> CheckResult:
    return CheckResult(
        invalidator_id=invalidator.id,
        thesis_id=invalidator.thesis_id,
        type=invalidator.type,
        outcome=outcome,
        detail=detail,
    )


def evaluate(
    session,
    invalidator: ThesisInvalidator,
    thesis: Thesis,
    *,
    now: datetime | None = None,
    price_source=None,
) -> CheckResult:
    """Ask one invalidator whether it has happened. Writes nothing."""
    now = now or utcnow_naive()
    params = invalidator.params

    if invalidator.type == QUALITATIVE:
        return _result(
            invalidator,
            NEEDS_HUMAN_REVIEW,
            "qualitative invalidators are judged by a human, never by this job",
        )
    if invalidator.status == TRIGGERED:
        return _result(invalidator, ALREADY_TRIGGERED, invalidator.triggered_reason)

    if invalidator.type == PRICE_LEVEL:
        return _evaluate_price_level(
            session, invalidator, thesis, params, price_source=price_source
        )
    if invalidator.type == METRIC_THRESHOLD:
        return _evaluate_metric(session, invalidator, thesis, params, now=now)
    if invalidator.type == TIME_DECAY:
        return _evaluate_time_decay(session, invalidator, thesis, params, now=now)
    if invalidator.type == EVENT:
        return _evaluate_event(session, invalidator, thesis, params, now=now)

    return _result(  # pragma: no cover - the CHECK constraint forbids the case
        invalidator, INSUFFICIENT_DATA, f"no evaluator for type {invalidator.type!r}"
    )


def _evaluate_price_level(session, invalidator, thesis, params, *, price_source) -> CheckResult:
    sessions = int(params.get("consecutive_sessions", 1))
    source = price_source or default_price_source(session)
    closes = list(source.recent_closes(thesis.ticker, sessions))
    if len(closes) < sessions:
        return _result(
            invalidator,
            INSUFFICIENT_DATA,
            f"needs {sessions} session close(s) for {thesis.ticker}, have "
            f"{len(closes)}; the thesis is left alone",
        )
    compare = _COMPARATORS[params["operator"]]
    threshold = float(params["price"])
    window = closes[-sessions:]
    if all(compare(c.close, threshold) for c in window):
        rendered = ", ".join(f"{c.session_date.isoformat()}={c.close:g}" for c in window)
        return _result(
            invalidator,
            TRIGGERED,
            f"{thesis.ticker} closed {params['operator'].replace('_', ' ')} "
            f"{threshold:g} for {sessions} session(s): {rendered}",
        )
    last = window[-1]
    return _result(
        invalidator,
        NOT_TRIGGERED,
        f"last close {last.close:g} on {last.session_date.isoformat()} "
        f"(threshold {threshold:g}, needs {sessions} consecutive)",
    )


def _latest_per_period(rows) -> list:
    """One observation per ``valid_at``, the latest known — restatements win.

    ``observations_known_at`` returns rows ordered by ``valid_at`` then
    ``known_at_utc``, so the last row seen for a period is the most recently
    known version of it as of the cutoff.
    """
    by_period: dict = {}
    for row in rows:
        by_period[row.valid_at] = row
    return [by_period[k] for k in sorted(by_period)]


def _evaluate_metric(session, invalidator, thesis, params, *, now) -> CheckResult:
    from filings.observations import observations_known_at

    periods = int(params.get("consecutive_periods", 1))
    query_args = {
        "fact_type": params["fact_type"],
        "cutoff": now,
        "ticker_at_time": params.get("ticker") or thesis.ticker,
    }
    if params.get("entity_cik"):
        query_args["entity_cik"] = params["entity_cik"]
        query_args.pop("ticker_at_time")
    rows = observations_known_at(session, **query_args)
    observations = [r for r in _latest_per_period(rows) if r.value_numeric is not None]
    if len(observations) < periods:
        return _result(
            invalidator,
            INSUFFICIENT_DATA,
            f"needs {periods} period(s) of {params['fact_type']!r} for "
            f"{thesis.ticker}, have {len(observations)}; the thesis is left alone",
        )
    compare = _COMPARATORS[params["operator"]]
    threshold = float(params["value"])
    window = observations[-periods:]
    rendered = ", ".join(
        f"{o.valid_at.date().isoformat()}={o.value_numeric:g}" for o in window
    )
    if all(compare(float(o.value_numeric), threshold) for o in window):
        return _result(
            invalidator,
            TRIGGERED,
            f"{params['fact_type']} {params['operator'].replace('_', ' ')} "
            f"{threshold:g} for {periods} period(s): {rendered}",
        )
    return _result(
        invalidator,
        NOT_TRIGGERED,
        f"{params['fact_type']} not {params['operator'].replace('_', ' ')} "
        f"{threshold:g} across {periods} period(s): {rendered}",
    )


def deadline_for(invalidator: ThesisInvalidator, thesis: Thesis) -> date:
    """When a ``time_decay`` invalidator comes due.

    Anchored on the position open date when there is one — the clock the thesis
    is actually running against — and on the invalidator's own ``created_at``
    otherwise.
    """
    params = invalidator.params
    if params.get("deadline"):
        return _parse_date(params["deadline"], "deadline")
    anchor = thesis.position_opened_at or invalidator.created_at or utcnow_naive()
    anchor_date = anchor.date() if isinstance(anchor, datetime) else anchor
    days = int(params["days"]) if params.get("days") else int(params["quarters"]) * 91
    return anchor_date + timedelta(days=days)


def _evaluate_time_decay(session, invalidator, thesis, params, *, now) -> CheckResult:
    due = deadline_for(invalidator, thesis)
    today = now.date() if isinstance(now, datetime) else now
    if today < due:
        return _result(
            invalidator,
            NOT_TRIGGERED,
            f"due {due.isoformat()}, {(due - today).days} day(s) to run",
        )
    if params.get("unless_metric"):
        inner = _evaluate_metric(
            session, invalidator, thesis, params["unless_metric"], now=now
        )
        if inner.outcome == TRIGGERED:
            return _result(
                invalidator,
                NOT_TRIGGERED,
                f"deadline {due.isoformat()} passed, but the stated observable "
                f"moved: {inner.detail}",
            )
        if inner.outcome == INSUFFICIENT_DATA:
            return _result(
                invalidator,
                INSUFFICIENT_DATA,
                f"deadline {due.isoformat()} passed, but the 'unless' observable "
                f"could not be read: {inner.detail}",
            )
    return _result(
        invalidator,
        TRIGGERED,
        f"deadline {due.isoformat()} passed with the thesis unresolved",
    )


def _evaluate_event(session, invalidator, thesis, params, *, now) -> CheckResult:
    event_key = params["event_key"]
    matcher = _EVENT_MATCHERS.get(event_key)
    if matcher is None:
        return _result(
            invalidator,
            PENDING_PLANE,
            f"no plane has registered a matcher for event {event_key!r}; this "
            f"invalidator is recorded but not yet watched",
        )
    matched, detail = matcher(session, thesis, params, now)
    if matched:
        return _result(invalidator, TRIGGERED, f"{event_key}: {detail}")
    return _result(invalidator, NOT_TRIGGERED, f"{event_key}: {detail}")


# --------------------------------------------------------------------------- #
# The trigger path
# --------------------------------------------------------------------------- #


def record_trigger(session, invalidator: ThesisInvalidator, thesis: Thesis, result: CheckResult, *, now=None) -> bool:
    """Mark the invalidator triggered, weaken the thesis, and page.

    In that order, and nothing else. There is no branch in this function that
    closes a position, creates an order, or creates a proposal — Spec M §3 is
    explicit that surfacing "the reason for holding may be gone" is the whole
    job, and the decision is the owner's.

    Returns whether a page sink accepted the event. A Telegram failure does not
    roll back the status change: the database is the record, Telegram is the
    notification.
    """
    now = now or utcnow_naive()
    invalidator.status = TRIGGERED
    invalidator.triggered_at = now
    invalidator.triggered_reason = result.detail
    invalidator.last_checked_at = now
    invalidator.last_check_detail = result.detail

    if thesis.status == "active":
        thesis.status = "weakened"
        thesis.weakened_at = now
        thesis.updated_at = now
    session.flush()

    return paging.page(
        paging.PageEvent(
            kind="invalidator_triggered",
            ticker=thesis.ticker,
            thesis_id=thesis.id,
            thesis_title=thesis.title,
            detail=result.detail,
            invalidator_id=invalidator.id,
            invalidator_type=invalidator.type,
            at=now,
        )
    )


@dataclass
class CheckRunReport:
    """What one run of the daily job did. Printable, and asserted by tests."""

    ran: bool
    checked: int = 0
    results: list = field(default_factory=list)
    triggered: list = field(default_factory=list)
    needs_human_review: list = field(default_factory=list)
    pages_sent: int = 0
    skipped_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "checked": self.checked,
            "triggered": [r.invalidator_id for r in self.triggered],
            "needs_human_review": [r.invalidator_id for r in self.needs_human_review],
            "pages_sent": self.pages_sent,
            "skipped_reason": self.skipped_reason,
            "outcomes": {
                outcome: sum(1 for r in self.results if r.outcome == outcome)
                for outcome in sorted({r.outcome for r in self.results})
            },
        }


#: Statuses whose invalidators the daily job evaluates. A `draft` thesis has no
#: position behind it and an `invalidated`/`closed` one is finished.
CHECKED_STATUSES = ("active", "weakened")


def run_daily_check(
    session,
    *,
    now: datetime | None = None,
    price_source=None,
    settings=None,
    tickers=None,
) -> CheckRunReport:
    """Evaluate every checkable invalidator on every live thesis. Pure Python.

    No model call, by construction (Spec K §5): this is comparisons against
    prices, observations, dates, and registered event matchers.
    """
    if settings is None:
        from config.settings import Settings

        settings = Settings()
    if not getattr(settings, "research_workspace_enabled", False):
        return CheckRunReport(
            ran=False,
            skipped_reason="RESEARCH_WORKSPACE_ENABLED is false",
        )

    now = now or utcnow_naive()
    query = session.query(Thesis).filter(Thesis.status.in_(CHECKED_STATUSES))
    if tickers:
        query = query.filter(Thesis.ticker.in_([t.upper() for t in tickers]))
    report = CheckRunReport(ran=True)

    for thesis in query.order_by(Thesis.id.asc()).all():
        invalidators = (
            session.query(ThesisInvalidator)
            .filter(ThesisInvalidator.thesis_id == thesis.id)
            .order_by(ThesisInvalidator.id.asc())
            .all()
        )
        for invalidator in invalidators:
            if invalidator.status in ("triggered", "retired"):
                continue
            result = evaluate(
                session, invalidator, thesis, now=now, price_source=price_source
            )
            report.checked += 1
            report.results.append(result)
            if result.outcome == NEEDS_HUMAN_REVIEW:
                report.needs_human_review.append(result)
                continue
            invalidator.last_checked_at = now
            invalidator.last_check_detail = result.detail
            if result.triggered:
                report.triggered.append(result)
                if record_trigger(session, invalidator, thesis, result, now=now):
                    report.pages_sent += 1
    session.flush()
    log.info("research_invalidator_check", **report.as_dict())
    return report
