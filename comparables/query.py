"""One question, asked and answered and logged — the whole flow, in one place.

`compare_setups` over MCP, `scripts/cohort_smoke.py` and the lookahead script
all ask the same question in the same order, and the order matters:

1. resolve the roster slug (or the explicit `SetupSpec`) to a frozen, hashed
   spec — a setup that refuses because its evidence plane is Phase 4 work
   refuses *here*, before anything is built, and says `pending_plane` rather
   than `insufficient`;
2. serve the cached answer if this exact `(setup_hash, as_of, snapshot, depth)`
   has been answered before, because the same question against the same data
   vintage has to give the same bytes;
3. build the cohort from stored rows (`comparables/cohort.py`);
4. measure it (`comparables/report.py`, unchanged from Phase 3b), once per
   provenance block, because archival facts are never pooled with the others;
5. **log the query whatever the outcome** — `comparable_queries` is the §7
   trial ledger and a search that stopped counting when it stopped succeeding
   would be no accounting at all;
6. store the answer and hand back its citation id.

Nothing in this module produces a number. Step 4 is the only step that
computes anything, and it is a call into the pure core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from comparables import citations, cohort as cohort_mod, config, registry
from comparables import report as report_mod
from comparables import setups as roster
from comparables.setup_spec import Condition, SetupSpec, SetupSpecError

STATUS_PENDING_PLANE = "pending_plane"
STATUS_UNAVAILABLE = "unavailable"


class QueryRefused(RuntimeError):
    """The question could not be asked. Distinct from an `insufficient` answer.

    Spec N §8 is emphatic that `insufficient` means *the engine looked and the
    cohort was too small or too clustered*. "There is no Form 4 plane yet" and
    "no price snapshot is configured" are different statements, and rounding
    them into `insufficient` would put a "we found nothing" in front of a
    reader whose truth is "we have not looked".
    """

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class QueryOutcome:
    """What a caller gets back: the answer, where it is stored, what it rests on."""

    setup: SetupSpec
    depth: str
    status: str
    answer: Any
    archival_block: Any | None
    provenance: dict
    query_id: int | None
    citation_id: str | None
    cached: bool
    build: cohort_mod.CohortBuild | None

    def payload(self) -> dict:
        """The §8 discriminated answer plus the provenance block, as JSON data."""
        body = _as_json_body(self.answer)
        return {
            "status": self.status,
            "depth": self.depth,
            "setup": self.setup.canonical(),
            "setup_hash": self.setup.content_hash,
            "family_slug": self.setup.family_slug,
            "answer": body,
            "archival_block": (
                _as_json_body(self.archival_block)
                if self.archival_block is not None else None
            ),
            "provenance": self.provenance,
            "query_id": self.query_id,
            "citation_id": self.citation_id,
            "cached": self.cached,
            "citable": self.depth == "full" and self.status != "insufficient",
        }


# --------------------------------------------------------------------------- #
# Resolving a question into a spec
# --------------------------------------------------------------------------- #


def spec_from_json(payload: Mapping[str, Any]) -> SetupSpec:
    """An explicit `SetupSpec`, validated by the same rules the roster is.

    Spec N §4.0: a free-form request is the same code path as a roster entry
    and *counts as a trial against the nearest family*. That happens for free —
    the family slug is derived from the universe and the primary condition, so
    an ad-hoc spec lands in the family it belongs to whatever it calls itself.
    """
    try:
        conditions = tuple(
            Condition(
                c["fact"], c["op"],
                tuple(c["value"]) if isinstance(c.get("value"), list) else c["value"],
            )
            for c in payload["conditions"]
        )
        return SetupSpec(
            slug=payload["slug"],
            version=payload["version"],
            conditions=conditions,
            universe=payload["universe"],
            horizons_sessions=tuple(int(h) for h in payload["horizons_sessions"]),
            execution_policy=payload.get("execution_policy"),
            match_covariates=tuple(payload.get("match_covariates", ())),
            lookback_years=int(payload["lookback_years"]),
        )
    except KeyError as exc:
        raise SetupSpecError(
            f"setup_spec is missing {exc.args[0]!r}; every field of a SetupSpec is "
            f"required (Spec N §4.0) — a setup is a typed predicate, not a vibe"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise SetupSpecError(f"setup_spec is not a valid SetupSpec: {exc}") from exc


def resolve_setup(
    *,
    setup: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    setup_spec: Mapping[str, Any] | None = None,
) -> tuple[SetupSpec, roster.RosterEntry | None]:
    """`(spec, roster_entry)` for a roster slug or an explicit spec."""
    if setup and setup_spec:
        raise SetupSpecError(
            "pass either a roster slug or an explicit setup_spec, not both: "
            "which one the answer was about has to be unambiguous in the log"
        )
    if setup_spec:
        if parameters:
            raise SetupSpecError(
                "parameters move a roster entry's thresholds; an explicit "
                "setup_spec already carries its own"
            )
        return spec_from_json(setup_spec), None
    if not setup:
        raise SetupSpecError(
            f"name a setup: the roster is {list(roster.slugs())}, or pass an "
            f"explicit setup_spec"
        )
    entry = roster.get(setup, parameters)
    return entry.spec, entry


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #


def answer_query(
    session,
    *,
    context: cohort_mod.CohortContext,
    as_of: date,
    depth: str = "quick",
    setup: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    setup_spec: Mapping[str, Any] | None = None,
    requester_label: str = "",
    use_cache: bool = True,
    query_covariates: Mapping[str, float] | None = None,
    reps: int | None = None,
    seed: int = config.DEFAULT_SEED,
) -> QueryOutcome:
    """Ask one cohort question end to end, and leave a stored trail of it."""
    if depth not in ("quick", "full"):
        raise SetupSpecError(f"depth must be 'quick' or 'full', got {depth!r}")

    spec, entry = resolve_setup(
        setup=setup, parameters=parameters, setup_spec=setup_spec
    )
    if entry is not None and not entry.available:
        raise QueryRefused(STATUS_PENDING_PLANE, entry.unavailable_reason or
                           f"{entry.slug} has no evidence plane in this phase")

    snapshot = cohort_mod.snapshot_status(session, context.price_snapshot_slug)

    if use_cache:
        cached = registry.cached_answer(
            session, setup_hash=spec.content_hash, as_of=as_of,
            price_snapshot_id=snapshot.snapshot_id, depth=depth,
        )
        if cached is not None:
            return QueryOutcome(
                setup=spec,
                depth=depth,
                status=cached.status,
                answer=_Stored(cached.answer),
                archival_block=(
                    _Stored(cached.archival_block) if cached.archival_block else None
                ),
                provenance=_provenance_block(
                    snapshot, None, cached.query_id, cached.provenance_mix, context,
                ),
                query_id=cached.query_id,
                citation_id=citations.citation_id(cached.id),
                cached=True,
                build=None,
            )

    try:
        build = cohort_mod.build_cohort(session, entry or spec, as_of=as_of, context=context)
    except cohort_mod.PendingPlane as exc:
        raise QueryRefused(STATUS_PENDING_PLANE, str(exc)) from exc
    except cohort_mod.CohortConstructionError as exc:
        raise QueryRefused(STATUS_UNAVAILABLE, str(exc)) from exc

    if not build.events:
        # An empty cohort is still a trial, and still a stored query.
        result = _empty_result(build)
        record = registry.record_query(
            session, spec, requester_label=requester_label, as_of=as_of, depth=depth,
            status="insufficient", evidence_tier=build.evidence_cap,
            result=result, price_snapshot_id=snapshot.snapshot_id,
            universe_slug=context.universe_slug,
            candidate_source=roster.source_for_spec(spec) if entry is None
            else entry.candidate_source,
            refusal_reason=result["refusal_reason"],
        )
        answer = report_mod.RefusedAnswer(
            setup=spec, depth=depth, status="insufficient",
            evidence_tier=build.evidence_cap,
            refusal_reason=result["refusal_reason"],
            n_matured=0, n_distinct_dates=0, delisting_rate=0.0,
            trials_against_this_pattern=record.trials_against_this_pattern,
            floors=config.get_floors(),
        )
        stored = registry.store_answer(
            session, spec, as_of=as_of, price_snapshot_id=snapshot.snapshot_id,
            depth=depth, status="insufficient", evidence_tier=build.evidence_cap,
            answer_json=report_mod.to_json(answer),
            provenance_mix=build.provenance_mix, query_id=record.query_id,
        )
        return QueryOutcome(
            setup=spec, depth=depth, status="insufficient", answer=answer,
            archival_block=None,
            provenance=_provenance_block(
                snapshot, build, record.query_id, build.provenance_mix, context
            ),
            query_id=record.query_id,
            citation_id=citations.citation_id(stored.id),
            cached=False, build=build,
        )

    trial_registry = registry.StoredTrialRegistry(session)
    result = cohort_mod.answer_for(
        build,
        depth=depth,
        registry=trial_registry,
        query_covariates=query_covariates,
        family_cohorts=registry.family_moments(
            session, spec.family_slug, exclude_setup_hash=spec.content_hash
        ),
        reps=reps,
        seed=seed,
    )
    answer = result.primary
    status = getattr(answer, "status", "insufficient")

    record = registry.record_query(
        session, spec, requester_label=requester_label, as_of=as_of, depth=depth,
        status=status, evidence_tier=answer.evidence_tier,
        result=json.loads(report_mod.to_json(answer)),
        price_snapshot_id=snapshot.snapshot_id,
        universe_slug=context.universe_slug,
        candidate_source=roster.source_for_spec(spec) if entry is None
        else entry.candidate_source,
        refusal_reason=getattr(answer, "refusal_reason", None),
        n_matured=getattr(answer, "n_matured", 0),
        n_distinct_dates=getattr(answer, "n_distinct_dates", 0),
        trials=getattr(answer, "trials_against_this_pattern", None),
    )
    stored = registry.store_answer(
        session, spec, as_of=as_of, price_snapshot_id=snapshot.snapshot_id,
        depth=depth, status=status, evidence_tier=answer.evidence_tier,
        answer_json=report_mod.to_json(answer),
        archival_block_json=(
            report_mod.to_json(result.archival) if result.archival is not None else None
        ),
        provenance_mix=dict(result.provenance_mix),
        family_moments=cohort_moments(build),
        query_id=record.query_id,
    )
    return QueryOutcome(
        setup=spec,
        depth=depth,
        status=status,
        answer=answer,
        archival_block=result.archival,
        provenance=_provenance_block(
            snapshot, build, record.query_id, dict(result.provenance_mix), context
        ),
        query_id=record.query_id,
        citation_id=citations.citation_id(stored.id),
        cached=False,
        build=build,
    )


@dataclass(frozen=True)
class _Stored:
    """A cached answer, replayed. It is data, and it is never recomputed."""

    body: dict


def _as_json_body(answer: Any) -> dict:
    """The answer as plain JSON data, whether freshly built or replayed."""
    if isinstance(answer, _Stored):
        return answer.body
    return json.loads(report_mod.to_json(answer))


def cohort_moments(build: cohort_mod.CohortBuild) -> tuple[dict, ...]:
    """This cohort's own `(n, mean, within_variance)` per horizon.

    Stored on the answer so a *sibling* cohort in the same family can be shrunk
    toward the family mean (§6.4) without re-running this one. The numbers come
    from `comparables.outcomes.horizon_outcomes` — the same call every other
    figure in the answer comes from — so writing them down is not a second
    estimate of anything.
    """
    from statistics import variance

    from comparables.outcomes import horizon_outcomes

    events = build.blocks().point_in_time or build.events
    out: list[dict] = []
    for horizon in build.setup.horizons_sessions:
        measured = horizon_outcomes(events, build.benchmark, build.calendar, horizon)
        if measured.n_matured < 2:
            continue
        out.append({
            "horizon": int(horizon),
            "n": int(measured.n_matured),
            "mean": float(measured.mean_car),
            "within_variance": float(variance(measured.car)),
        })
    return tuple(out)


def _empty_result(build: cohort_mod.CohortBuild) -> dict:
    excluded = {}
    for item in build.excluded:
        excluded[item.reason] = excluded.get(item.reason, 0) + 1
    return {
        "refusal_reason": (
            f"no event met the setup's conditions: {build.n_candidates} candidates "
            f"were examined and every one was excluded "
            f"({', '.join(f'{k}={v}' for k, v in sorted(excluded.items())) or 'no candidates'}). "
            f"`insufficient` is a valid, expected, frequently-correct answer and is "
            f"rendered as a refusal, never rounded into a hedge (Spec N §8)"
        ),
        "n_candidates": build.n_candidates,
        "excluded_by_reason": excluded,
    }


def _provenance_block(
    snapshot: cohort_mod.SnapshotStatus,
    build: cohort_mod.CohortBuild | None,
    query_id: int | None,
    provenance_mix: Mapping[str, int],
    context: cohort_mod.CohortContext,
) -> dict:
    """What the answer rests on: the snapshot, its audit, the universe, the query.

    Every one of these can silently degrade an answer, so every one of them is
    named in the response rather than left to be inferred from the tier.
    """
    block: dict[str, Any] = {
        "price_snapshot": {
            "slug": snapshot.slug,
            "id": snapshot.snapshot_id,
            "exists": snapshot.exists,
            "source": snapshot.source,
            "delisting_audit_recorded": snapshot.audit_recorded,
            "delisting_audit_reason": snapshot.audit_reason,
            "terminal_returns_synthesised": snapshot.terminal_returns_synthesised,
            "collapse_rate_of_classified": snapshot.collapse_rate_of_classified,
            "backs_point_in_time_tier": snapshot.can_back_point_in_time,
        },
        "query_id": query_id,
        "provenance_mix": dict(provenance_mix),
        "benchmark_security_uid": context.benchmark_security_uid,
    }
    if build is not None:
        block["universe"] = {
            "slug": build.universe.slug,
            "point_in_time": build.universe.point_in_time,
            "reason": build.universe.reason,
            "sources": list(build.universe.sources),
            "provenance": build.universe.provenance,
            "member_dates": build.universe.n_member_dates,
            "members_seen": build.universe.n_members_seen,
        }
        block["evidence_cap"] = build.evidence_cap
        block["universe_delisting_rate"] = build.universe_delisting_rate
        block["n_candidates"] = build.n_candidates
        block["n_excluded"] = len(build.excluded)
        block["build_warnings"] = list(build.warnings)
    else:
        block["universe"] = {"slug": context.universe_slug, "replayed_from_cache": True}
    return block


# --------------------------------------------------------------------------- #
# `cohort_detail`
# --------------------------------------------------------------------------- #


def cohort_detail(
    session,
    *,
    context: cohort_mod.CohortContext,
    query_id: int,
) -> dict:
    """The constituent events of a stored query, with provenance and outcomes.

    Rebuilt from the stored `SetupSpec` and `as_of`, not from a cached event
    list: the cohort is a pure function of the stored rows and the spec, and if
    rebuilding it gives a different cohort then the *cache* was the thing that
    was wrong. `constituents_match_stored_answer` says whether it did.
    """
    row = registry.query(session, query_id)
    if row is None:
        raise QueryRefused(
            STATUS_UNAVAILABLE,
            f"no stored query {query_id}; every answer this engine gives carries "
            f"the id of the query that produced it, and cohort_detail resolves it",
        )
    spec = spec_from_json(row.setup)
    build = cohort_mod.build_cohort(session, spec, as_of=row.as_of_date, context=context)
    horizons = spec.horizons_sessions

    from comparables.outcomes import car, event_return, maturity

    events = []
    for event in sorted(build.events, key=lambda e: e.event_id):
        status = {h: maturity(event, build.calendar, h) for h in horizons}
        outcomes = {}
        for h in horizons:
            if not status[h].matured:
                outcomes[str(h)] = {
                    "matured": False, "censored_reason": status[h].reason,
                }
                continue
            outcomes[str(h)] = {
                "matured": True,
                "raw_return": event_return(event, build.calendar, h),
                "car": car(event, build.benchmark, build.calendar, h),
            }
        events.append({
            "event_id": event.event_id,
            "ticker": event.ticker,
            "known_at_utc": event.known_at_utc.isoformat(),
            "provenance_class": event.provenance,
            "regime": event.regime,
            "liquidity_decile": event.liquidity_decile,
            "covariates": {name: value for name, value in event.covariates},
            "terminal": None if event.terminal is None else {
                "date": event.terminal.date.isoformat(),
                "reason": event.terminal.reason,
                "terminal_return": event.terminal.terminal_return,
                "resolved": event.terminal.resolved,
            },
            "outcomes_by_horizon": outcomes,
        })

    return {
        "query_id": row.id,
        "setup_hash": row.setup_hash,
        "setup_slug": row.setup_slug,
        "family_slug": row.family_slug,
        "as_of": row.as_of_date.isoformat(),
        "status": row.status,
        "evidence_tier": row.evidence_tier,
        "n_events": len(events),
        "provenance_mix": build.provenance_mix,
        "events": events,
        "excluded": [
            {
                "event_id": e.event_id, "ticker": e.ticker,
                "event_date": e.event_date.isoformat(),
                "reason": e.reason, "detail": e.detail,
            }
            for e in sorted(build.excluded, key=lambda e: (e.event_date, e.event_id))
        ],
        "constituents_match_stored_answer": (
            len(events) == (row.result.get("n_matured", len(events))
                            + row.result.get("n_censored", 0))
            if row.status != "insufficient" else None
        ),
    }
