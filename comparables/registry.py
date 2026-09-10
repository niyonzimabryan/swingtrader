"""The query log, the answer cache, and the engine's own track record.

Three stored tables and the reasons they exist.

``comparable_queries`` — **researcher degrees of freedom (Spec N §7).**
Every cohort query is logged, answered or refused, with its `SetupSpec`, the
requester's token label, the `as_of` date, the price snapshot, the depth and
the result. The trial count a response prints is a `COUNT(DISTINCT setup_hash)`
within the setup's *family slug*, so the twelfth variant cannot present itself
as the first. The family is derived from the universe and the primary
condition, never from the slug, so renaming a setup does not reset the count
(`test_trial_count_keyed_by_family`, `test_trial_count_increments`).

``cohort_answers`` — **the cache Spec N §4.0 describes.** Keyed by
`(setup_hash, as_of_date, price_snapshot_id, depth, subject_ticker)`. Same
question, same data vintage, same answer, byte for byte — which is also what
makes the lookahead harness's byte-comparison meaningful. The subject is in the
key even though the statistics do not depend on it (Spec L §6.6): a citation is
`cohort:<row id>`, and a row serving two subjects would silently re-point a
citation already written into the journal.

``cohort_predictions`` — **the engine's own track record (§6.5).** Every cited
`full` answer is written here and scored later by *sign* and by *cohort
percentile*, and by nothing else. The confidence interval is on the cohort
mean; it is never scored as a predictive interval, and there is no column in
which a coverage figure could be stored.

Nothing in this module computes a statistic. It stores what
`comparables/report.py` produced and reads it back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from sqlalchemy import func, select

from database.models import CohortAnswerRow, CohortPredictionRow, ComparableQuery

from comparables.setup_spec import SetupSpec

if TYPE_CHECKING:  # a shape this module stores, not a dependency it takes on
    from comparables.cohort import SubjectQualification

#: `cohort_answers.price_snapshot_id` when a cohort ran without a snapshot row.
#: A NULL would make every un-snapshotted answer distinct from every other
#: under the unique key, on both engines, which is the opposite of a cache.
NO_SNAPSHOT = -1


class RegistryError(RuntimeError):
    """A registry operation that must not be papered over."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Trial accounting (§7)
# --------------------------------------------------------------------------- #


def trials_for_family(session, family_slug: str) -> int:
    """Distinct setup hashes tried against this fact pattern."""
    return int(session.execute(
        select(func.count(func.distinct(ComparableQuery.setup_hash)))
        .where(ComparableQuery.family_slug == family_slug)
    ).scalar() or 0)


def queries_for_family(session, family_slug: str) -> int:
    """Every query against the family, including repeats of one spec."""
    return int(session.execute(
        select(func.count()).select_from(ComparableQuery)
        .where(ComparableQuery.family_slug == family_slug)
    ).scalar() or 0)


class StoredTrialRegistry:
    """`comparables.inference.TrialRegistry`, backed by `comparable_queries`.

    Duck-typed rather than subclassed: `report.build_answer` needs `record`,
    and this one counts against the stored log instead of an in-process dict,
    so a trial count survives a restart. Trials are counted at *record* time —
    the query row is written by `record_query` after the answer exists, so this
    reports the count **including** the query being answered, which is what
    §7's "the twelfth variant reports 12" means.
    """

    def __init__(self, session, *, pending: Sequence[SetupSpec] = ()):
        self._session = session
        self._pending = {s.family_slug: {s.content_hash} for s in pending}

    def record(self, spec: SetupSpec) -> int:
        stored = set(session_hashes(self._session, spec.family_slug))
        stored |= self._pending.setdefault(spec.family_slug, set())
        stored.add(spec.content_hash)
        self._pending[spec.family_slug] = stored
        return len(stored)

    def trials(self, spec: SetupSpec) -> int:
        return self.record(spec)


def session_hashes(session, family_slug: str) -> tuple[str, ...]:
    rows = session.execute(
        select(ComparableQuery.setup_hash)
        .where(ComparableQuery.family_slug == family_slug)
        .distinct()
    ).scalars().all()
    return tuple(sorted(rows))


# --------------------------------------------------------------------------- #
# The query log
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QueryRecord:
    """What a caller gets back: the stored row's identity, not the ORM object."""

    query_id: int
    setup_hash: str
    family_slug: str
    trials_against_this_pattern: int
    status: str
    depth: str
    as_of: date
    price_snapshot_id: int | None
    subject_ticker: str = ""
    subject_qualifies: bool | None = None
    subject_reason: str = ""


def record_query(
    session,
    spec: SetupSpec,
    *,
    requester_label: str,
    as_of: date,
    depth: str,
    status: str,
    evidence_tier: str,
    result: dict,
    price_snapshot_id: int | None = None,
    universe_slug: str = "",
    candidate_source: str = "",
    refusal_reason: str | None = None,
    n_matured: int = 0,
    n_distinct_dates: int = 0,
    trials: int | None = None,
    subject: "SubjectQualification | None" = None,
) -> QueryRecord:
    """Log one query. Refusals are logged exactly like answers.

    A search that stopped counting when it stopped succeeding would be no
    accounting at all: the variants that came back `insufficient` are precisely
    the ones a reader needs to know were tried.
    """
    row = ComparableQuery(
        setup_hash=spec.content_hash,
        setup_slug=spec.slug,
        setup_version=spec.version,
        family_slug=spec.family_slug,
        setup_json=json.dumps(spec.canonical(), sort_keys=True, separators=(",", ":")),
        requester_label=requester_label or "",
        as_of_date=as_of,
        price_snapshot_id=price_snapshot_id,
        universe_slug=universe_slug or spec.universe,
        candidate_source=candidate_source or "",
        depth=depth,
        status=status,
        evidence_tier=evidence_tier or "",
        refusal_reason=refusal_reason,
        n_matured=int(n_matured),
        n_distinct_dates=int(n_distinct_dates),
        trials_against_this_pattern=0,
        subject_ticker=subject.ticker if subject is not None else "",
        subject_qualifies=subject.qualifies if subject is not None else None,
        subject_reason=subject.reason if subject is not None else "",
        subject_event_date=subject.event_date if subject is not None else None,
        result_json=json.dumps(result, sort_keys=True, separators=(",", ":"), default=str),
    )
    session.add(row)
    session.flush()
    # Counted *after* the insert, so the query being logged is included: §7's
    # "the twelfth variant reports `trials_against_this_pattern=12`".
    row.trials_against_this_pattern = (
        trials if trials is not None else trials_for_family(session, spec.family_slug)
    )
    session.flush()
    return QueryRecord(
        query_id=row.id,
        setup_hash=row.setup_hash,
        family_slug=row.family_slug,
        trials_against_this_pattern=row.trials_against_this_pattern,
        status=row.status,
        depth=row.depth,
        as_of=row.as_of_date,
        price_snapshot_id=row.price_snapshot_id,
        subject_ticker=row.subject_ticker or "",
        subject_qualifies=row.subject_qualifies,
        subject_reason=row.subject_reason or "",
    )


def query(session, query_id: int) -> ComparableQuery | None:
    return session.get(ComparableQuery, query_id)


def queries_for_setup(session, setup_hash: str) -> tuple[ComparableQuery, ...]:
    return tuple(session.execute(
        select(ComparableQuery)
        .where(ComparableQuery.setup_hash == setup_hash)
        .order_by(ComparableQuery.id)
    ).scalars().all())


# --------------------------------------------------------------------------- #
# The answer cache
# --------------------------------------------------------------------------- #


def cached_answer(
    session,
    *,
    setup_hash: str,
    as_of: date,
    price_snapshot_id: int | None,
    depth: str,
    subject_ticker: str = "",
) -> CohortAnswerRow | None:
    """The stored answer for this exact question and data vintage, or `None`.

    `subject_ticker` is part of the key, not a filter over it: the cohort's
    statistics do not depend on which name the question was *about*, but the
    citation does, and one row serving two subjects would re-point a citation
    somebody already wrote down (Spec L §6.6). `''` — no subject named — is a
    key value like any other, which is why it is the empty string and not NULL.
    """
    return session.execute(
        select(CohortAnswerRow).where(
            CohortAnswerRow.setup_hash == setup_hash,
            CohortAnswerRow.as_of_date == as_of,
            CohortAnswerRow.price_snapshot_id == (
                NO_SNAPSHOT if price_snapshot_id is None else price_snapshot_id
            ),
            CohortAnswerRow.depth == depth,
            CohortAnswerRow.subject_ticker == (subject_ticker or ""),
        )
    ).scalars().first()


def store_answer(
    session,
    spec: SetupSpec,
    *,
    as_of: date,
    price_snapshot_id: int | None,
    depth: str,
    status: str,
    evidence_tier: str,
    answer_json: str,
    provenance_mix: dict,
    archival_block_json: str | None = None,
    family_moments: Sequence[dict] = (),
    query_id: int | None = None,
    subject: "SubjectQualification | None" = None,
) -> CohortAnswerRow:
    """Insert or refresh the cached answer for this key.

    Refresh rather than refuse: re-running the same question against the same
    snapshot must produce the same bytes, so overwriting is a no-op in the
    normal case and the honest repair in the case where it is not.
    """
    snapshot_id = NO_SNAPSHOT if price_snapshot_id is None else price_snapshot_id
    subject_ticker = subject.ticker if subject is not None else ""
    row = cached_answer(
        session, setup_hash=spec.content_hash, as_of=as_of,
        price_snapshot_id=snapshot_id, depth=depth, subject_ticker=subject_ticker,
    )
    if row is None:
        row = CohortAnswerRow(
            setup_hash=spec.content_hash,
            as_of_date=as_of,
            price_snapshot_id=snapshot_id,
            depth=depth,
            subject_ticker=subject_ticker,
        )
        session.add(row)
    # The subject verdict is refreshed with the row it belongs to. It is a pure
    # function of the same stored facts the answer is, so a refresh that changed
    # it would mean the *facts* moved — which is the case the lookahead harness
    # exists to catch, not one to paper over here.
    row.subject_qualifies = subject.qualifies if subject is not None else None
    row.subject_reason = subject.reason if subject is not None else ""
    row.subject_event_date = subject.event_date if subject is not None else None
    row.family_slug = spec.family_slug
    row.status = status
    row.evidence_tier = evidence_tier or ""
    row.query_id = query_id
    row.answer_json = answer_json
    row.archival_block_json = archival_block_json
    row.provenance_mix_json = json.dumps(provenance_mix, sort_keys=True)
    row.family_moments_json = json.dumps(list(family_moments), sort_keys=True)
    row.created_at = _utcnow()
    session.flush()
    return row


def answer(session, answer_id: int) -> CohortAnswerRow | None:
    return session.get(CohortAnswerRow, answer_id)


def family_moments(session, family_slug: str, *, exclude_setup_hash: str = "",
                   depth: str = "full"):
    """`CohortMoments` for every stored sibling cohort in a family (§6.4).

    One entry per sibling, taken at that cohort's **longest** horizon. Spec N
    §6.4 pools "all cohorts sharing the same primary condition" into one
    `k`, and a cohort contributes one mean to that pool, not one per horizon;
    the longest horizon is the one the family's headline is quoted at, so it is
    the one the pool is formed from. Mixing horizons inside a family would
    shrink a 1-session mean toward a 20-session one, which is not a prior, it
    is a units error.

    The cohort being answered is excluded: shrinking an estimate toward a pool
    that contains it pulls it toward itself and understates the shrinkage.
    """
    from comparables.inference import CohortMoments

    out = []
    for row in family_answers(session, family_slug, depth=depth):
        if exclude_setup_hash and row.setup_hash == exclude_setup_hash:
            continue
        stored = row.family_moments
        if not stored:
            continue
        best = max(stored, key=lambda m: int(m.get("horizon", 0)))
        try:
            out.append(CohortMoments(
                slug=f"{row.setup_hash[:12]}@{row.as_of_date.isoformat()}",
                n=int(best["n"]),
                mean=float(best["mean"]),
                within_variance=float(best["within_variance"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def family_answers(
    session, family_slug: str, *, depth: str = "full"
) -> tuple[CohortAnswerRow, ...]:
    """Every stored cohort in a family, for the §6.4 shrinkage gate.

    Empirical-Bayes shrinkage needs at least five cohorts in a family before
    `τ̂²` is anything but noise. That count is a stored fact, not an in-process
    one, which is why the gate reads this and not a list a caller passed in.
    """
    return tuple(session.execute(
        select(CohortAnswerRow)
        .where(CohortAnswerRow.family_slug == family_slug,
               CohortAnswerRow.depth == depth,
               CohortAnswerRow.status != "insufficient")
        .order_by(CohortAnswerRow.as_of_date, CohortAnswerRow.id)
    ).scalars().all())


# --------------------------------------------------------------------------- #
# The engine's own track record (§6.5)
# --------------------------------------------------------------------------- #

#: Column names that must never appear on `cohort_predictions`. The CI is on
#: the cohort mean and is never scored as a predictive interval, so a coverage
#: figure has nowhere to live (`test_mean_ci_not_scored_as_prediction_interval`).
FORBIDDEN_PREDICTION_FIELDS = (
    "coverage", "covered", "in_interval", "within_ci", "hit_interval",
)


def prediction_columns() -> tuple[str, ...]:
    return tuple(c.name for c in CohortPredictionRow.__table__.columns)


def record_predictions(
    session,
    *,
    cohort_answer_id: int,
    spec: SetupSpec,
    as_of: date,
    query_ticker: str,
    horizons: Iterable[dict],
    query_id: int | None = None,
) -> tuple[CohortPredictionRow, ...]:
    """Write one row per horizon of a cited `full` answer.

    `horizons` carries, per horizon: `horizon_sessions`, `point_estimate`,
    `ci_low`, `ci_high`, `cohort_outcomes` (the per-event outcome distribution
    at that horizon) and `matures_on`. Those are read off the answer the engine
    already produced; nothing is recomputed here.
    """
    written: list[CohortPredictionRow] = []
    for item in horizons:
        horizon = int(item["horizon_sessions"])
        existing = session.execute(
            select(CohortPredictionRow).where(
                CohortPredictionRow.cohort_answer_id == cohort_answer_id,
                CohortPredictionRow.horizon_sessions == horizon,
                CohortPredictionRow.query_ticker == query_ticker,
            )
        ).scalars().first()
        row = existing or CohortPredictionRow(
            cohort_answer_id=cohort_answer_id,
            horizon_sessions=horizon,
            query_ticker=query_ticker,
        )
        row.query_id = query_id
        row.setup_hash = spec.content_hash
        row.family_slug = spec.family_slug
        row.as_of_date = as_of
        row.point_estimate = float(item["point_estimate"])
        row.ci_low = float(item["ci_low"])
        row.ci_high = float(item["ci_high"])
        row.cohort_outcomes_json = json.dumps(
            [float(v) for v in item.get("cohort_outcomes", ())]
        )
        row.matures_on = item["matures_on"]
        if existing is None:
            session.add(row)
        written.append(row)
    session.flush()
    return tuple(written)


def percentile_of(value: float, distribution: Sequence[float]) -> float:
    """Where `value` sits in `distribution`, 0..100, ties counted at a half.

    The same convention `comparables.balance.query_vs_cohort` uses, so a
    percentile printed beside a cohort and a percentile scored against it later
    mean the same thing.
    """
    if not distribution:
        raise RegistryError("cannot take a percentile in an empty distribution")
    below = sum(1 for v in distribution if v < value)
    ties = sum(1 for v in distribution if v == value)
    return (below + 0.5 * ties) / len(distribution) * 100.0


def score_prediction(
    session, prediction: CohortPredictionRow, realized_return: float
) -> CohortPredictionRow:
    """Score one matured prediction by sign and cohort percentile, and no more.

    Spec N §6.5: *"The CI is on the cohort mean and is never scored as a
    predictive interval — one trade landing outside the interval for the
    average of a hundred trades says nothing."* So: does the realized return
    have the sign the point estimate predicted, and where does it sit in the
    cohort's own outcome distribution. Over many predictions those percentiles
    should be uniform; a pile-up at the tails means the cohorts are not
    describing the trades being taken.
    """
    realized = float(realized_return)
    prediction.realized_return = realized
    estimate = float(prediction.point_estimate)
    # A zero point estimate predicts no direction, so no sign can be right or
    # wrong about it. `None` says that; `False` would score it as a miss.
    prediction.sign_correct = (
        None if estimate == 0.0 else (realized > 0.0) == (estimate > 0.0)
    )
    outcomes = prediction.cohort_outcomes
    prediction.realized_percentile = (
        percentile_of(realized, outcomes) if outcomes else None
    )
    prediction.scored_at = _utcnow()
    session.flush()
    return prediction


def due_predictions(session, as_of: date) -> tuple[CohortPredictionRow, ...]:
    """Cited predictions whose horizon has matured and that are not yet scored."""
    return tuple(session.execute(
        select(CohortPredictionRow).where(
            CohortPredictionRow.matures_on <= as_of,
            CohortPredictionRow.scored_at.is_(None),
        ).order_by(CohortPredictionRow.matures_on, CohortPredictionRow.id)
    ).scalars().all())


@dataclass(frozen=True)
class TrackRecord:
    """What the weekly review prints once twenty predictions have matured."""

    n_scored: int
    n_directional: int
    sign_hit_rate: float | None
    percentiles: tuple[float, ...]
    reportable: bool
    reason: str

    #: There is no coverage field here either, for the same reason.
    MIN_SCORED_TO_REPORT = 20


def track_record(session, family_slug: str | None = None) -> TrackRecord:
    """Sign hit rate and the percentile sample, gated on twenty matured rows."""
    statement = select(CohortPredictionRow).where(
        CohortPredictionRow.scored_at.is_not(None)
    )
    if family_slug is not None:
        statement = statement.where(CohortPredictionRow.family_slug == family_slug)
    rows = tuple(session.execute(statement).scalars().all())

    directional = [r for r in rows if r.sign_correct is not None]
    hit_rate = (
        sum(1 for r in directional if r.sign_correct) / len(directional)
        if directional else None
    )
    percentiles = tuple(
        float(r.realized_percentile) for r in rows if r.realized_percentile is not None
    )
    reportable = len(rows) >= TrackRecord.MIN_SCORED_TO_REPORT
    return TrackRecord(
        n_scored=len(rows),
        n_directional=len(directional),
        sign_hit_rate=hit_rate,
        percentiles=percentiles,
        reportable=reportable,
        reason=(
            "reportable"
            if reportable
            else f"{len(rows)} matured predictions against a floor of "
                 f"{TrackRecord.MIN_SCORED_TO_REPORT} (Spec N §6.5)"
        ),
    )


def prediction_inputs(answer, build) -> tuple[dict, ...]:
    """Turn a `full` answer and its cohort into the per-horizon rows §6.5 stores.

    Both arguments are the objects the engine built, not their JSON: the point
    estimate and the interval come off the `HorizonResult`, and the outcome
    distribution the percentile will later be taken in is the cohort's own
    per-event CAR at that horizon, computed by `comparables.outcomes` and
    stored as it stood when the prediction was made. Re-deriving it at scoring
    time would score the trade against a cohort nobody cited.
    """
    from comparables.outcomes import horizon_outcomes

    sessions = build.calendar.sessions
    future = [d for d in sessions if d > build.as_of]
    events = build.blocks().point_in_time or build.events

    out: list[dict] = []
    for horizon in answer.horizons:
        h = int(horizon.horizon_sessions)
        measured = horizon_outcomes(events, build.benchmark, build.calendar, h)
        matures_on = future[h - 1] if len(future) >= h else _add_sessions(build.as_of, h)
        out.append({
            "horizon_sessions": h,
            "point_estimate": float(horizon.headline.estimate),
            "ci_low": float(horizon.headline.lower),
            "ci_high": float(horizon.headline.upper),
            "cohort_outcomes": tuple(measured.car),
            "matures_on": matures_on,
        })
    return tuple(out)


def _add_sessions(day: date, sessions: int) -> date:
    """A weekday-only fallback when the stored calendar does not reach forward.

    Deliberately crude and deliberately visible: the maturation job re-reads
    the real calendar before scoring, and this only decides when to *look*.
    """
    from datetime import timedelta

    out = day
    remaining = sessions
    while remaining > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            remaining -= 1
    return out
