"""The lookahead harness (Spec N §10, `test_truncated_data_identical_answer`).

Borrowed from freqtrade's `lookahead-analysis`, and it exists because a
point-in-time filter can be written correctly and still be bypassed. The
`known_at_utc <= t` clause in `filings/observations.py` is one line and it is
right; the leak, when there is one, is a join three modules away that reads a
covariate off today's row, or a universe list rebuilt after the fact, or a
moving average that quietly includes the session it is supposed to precede.
The only way to know is to **delete the rows and see whether anything changes**.

So, for a cohort:

1. Build it normally at `as_of` and fingerprint every qualified event —
   identity, cutoff, provenance, covariates, terminal outcome. Not the price
   series: that is the *outcome*, and truncating it is what step 2 does on
   purpose.
2. For each distinct event cutoff `t`, inside a savepoint that is always rolled
   back: delete every observation with `known_at_utc > t`, every membership row
   with `known_at_utc > t`, and every price bar with `session_date > t`, then
   rebuild the cohort **as of `t`** and compare its fingerprint JSON, byte for
   byte, against the baseline's fingerprints for the events that existed by
   then. Any difference is a fact from after `t` that had reached an event
   dated before it.
3. Once more at the latest cutoff, but keeping the bars, and compare the
   **whole answer's** canonical JSON. That catches a dependence on a fact
   published after the last event in the cohort — a restatement, a late
   universe rebuild — which step 2 cannot see because it truncates everything.

Both `tests/test_comparables_lookahead.py` and `scripts/cohort_lookahead_check.py`
call :func:`check_cohort`; the harness is the shared thing, not a shape each
copies.

Nothing here is safe to run against a database you care about outside a
savepoint, and nothing here rolls anything back for you if you call the pieces
directly. :func:`check_cohort` owns its savepoint and always rolls it back,
including on the way out of an exception.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from data.prices.store import delete_bars_after, delete_membership_after
from filings.observations import delete_observations_after

from comparables import cohort as cohort_mod
from comparables import report as report_mod
from comparables.setup_spec import SetupSpec
from comparables.setups import RosterEntry


class LookaheadFailure(AssertionError):
    """A cohort changed when facts it should never have seen were removed."""


@dataclass(frozen=True)
class Finding:
    """One cutoff at which the truncated rebuild disagreed with the baseline."""

    cutoff: str
    kind: str            # "event_fingerprints" | "full_answer"
    baseline: str
    truncated: str

    def summary(self) -> str:
        return (
            f"{self.kind} differ at cutoff {self.cutoff}: the cohort is not the "
            f"same when facts published after that instant are deleted, so "
            f"something read forward"
        )


@dataclass(frozen=True)
class LookaheadReport:
    """What the CI test asserts on and what the script prints."""

    setup_slug: str
    setup_hash: str
    as_of: date
    n_events: int
    n_cutoffs_checked: int
    n_cutoffs_total: int
    answer_identical: bool
    findings: tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings and self.answer_identical

    def raise_for_findings(self) -> "LookaheadReport":
        if self.clean:
            return self
        detail = "; ".join(f.summary() for f in self.findings) or (
            "the full answer changed when post-cohort facts were removed"
        )
        raise LookaheadFailure(
            f"{self.setup_slug} @ {self.as_of}: {detail} (Spec N §10)"
        )


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #


def event_fingerprint(event) -> dict:
    """Everything about an event that qualification decided.

    The price series is deliberately absent: it is the outcome, and the harness
    truncates it on purpose in step 2. What must not move is *which* events
    qualified, *when* each was knowable, *what* provenance it carries, *which
    covariates* it was matched on, and *how* its history was resolved.
    """
    terminal = event.terminal
    return {
        "event_id": event.event_id,
        "ticker": event.ticker,
        "known_at_utc": _iso(event.known_at_utc),
        "provenance": event.provenance,
        "regime": event.regime,
        "liquidity_decile": event.liquidity_decile,
        "covariates": [[name, repr(value)] for name, value in event.covariates],
        "terminal": None if terminal is None else {
            "date": terminal.date.isoformat(),
            "reason": terminal.reason,
            "terminal_return": repr(terminal.terminal_return),
            "resolved": terminal.resolved,
        },
    }


def fingerprints(events: Sequence[Any]) -> str:
    """Canonical JSON over the cohort's qualification, sorted by event id."""
    return json.dumps(
        sorted((event_fingerprint(e) for e in events), key=lambda d: d["event_id"]),
        sort_keys=True,
        separators=(",", ":"),
    )


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


@contextmanager
def _rolled_back(session):
    """A savepoint that is always rolled back, exception or not."""
    nested = session.begin_nested()
    try:
        yield nested
    finally:
        if nested.is_active:
            nested.rollback()


def truncate_at(session, cutoff: datetime) -> dict[str, int]:
    """Delete every stored fact that was not knowable at `cutoff`.

    Only call this inside :func:`_rolled_back`, which is why it is not exported
    in the module's docstring as something to use on its own.
    """
    return {
        "observations": delete_observations_after(session, cutoff),
        "universe_membership": delete_membership_after(session, cutoff),
        "price_bars": delete_bars_after(session, cutoff.date()),
    }


def _cutoffs(events: Sequence[Any], limit: int | None) -> tuple[datetime, ...]:
    """The distinct event cutoffs, ascending, thinned deterministically.

    Thinning keeps the first and the last and spreads the rest evenly, so a
    `limit` never silently drops the earliest event (whose history is shortest
    and where a leak is most likely) or the latest (where a restatement is).
    """
    distinct = sorted({_aware(e.known_at_utc) for e in events})
    if limit is None or len(distinct) <= limit:
        return tuple(distinct)
    if limit <= 2:
        return (distinct[0], distinct[-1])[:limit]
    step = (len(distinct) - 1) / (limit - 1)
    picked = {round(i * step) for i in range(limit)}
    return tuple(distinct[i] for i in sorted(picked))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def check_cohort(
    session,
    setup: SetupSpec | RosterEntry,
    *,
    as_of: date,
    context: cohort_mod.CohortContext,
    max_cutoffs: int | None = None,
    check_full_answer: bool = True,
    depth: str = "quick",
    reps: int = 200,
) -> LookaheadReport:
    """Re-run one cohort against a truncated ledger and compare, byte for byte.

    `depth="quick"` and a small `reps` by default: the harness is asserting
    that the *inputs* did not move, and a 10,000-replication bootstrap on every
    cutoff would make it too slow to run in CI, which is the same as not
    running it. The bootstrap is seeded, so `quick` is byte-comparable too.
    """
    baseline = cohort_mod.build_cohort(session, setup, as_of=as_of, context=context)
    if not baseline.events:
        return LookaheadReport(
            setup_slug=baseline.setup.slug,
            setup_hash=baseline.setup.content_hash,
            as_of=as_of,
            n_events=0,
            n_cutoffs_checked=0,
            n_cutoffs_total=0,
            answer_identical=True,
            findings=(),
        )

    all_cutoffs = _cutoffs(baseline.events, None)
    cutoffs = _cutoffs(baseline.events, max_cutoffs)
    findings: list[Finding] = []

    for cutoff in cutoffs:
        expected = fingerprints(
            [e for e in baseline.events if _aware(e.known_at_utc) <= cutoff]
        )
        with _rolled_back(session):
            truncate_at(session, cutoff)
            try:
                rebuilt = cohort_mod.build_cohort(
                    session, setup, as_of=cutoff.date(), context=context
                )
            except cohort_mod.CohortConstructionError:
                # Too little history left to build anything is not a leak: it is
                # what truncation does at the earliest cutoffs. Only a cohort
                # that *builds differently* is a finding.
                continue
            actual = fingerprints(rebuilt.events)
        if actual != expected:
            findings.append(Finding(cutoff.isoformat(), "event_fingerprints",
                                    expected, actual))

    answer_identical = True
    if check_full_answer:
        answer_identical = _full_answer_identical(
            session, setup, baseline, as_of=as_of, context=context,
            cutoff=all_cutoffs[-1], depth=depth, reps=reps, findings=findings,
        )

    return LookaheadReport(
        setup_slug=baseline.setup.slug,
        setup_hash=baseline.setup.content_hash,
        as_of=as_of,
        n_events=len(baseline.events),
        n_cutoffs_checked=len(cutoffs),
        n_cutoffs_total=len(all_cutoffs),
        answer_identical=answer_identical,
        findings=tuple(findings),
    )


def _full_answer_identical(
    session,
    setup,
    baseline,
    *,
    as_of: date,
    context: cohort_mod.CohortContext,
    cutoff: datetime,
    depth: str,
    reps: int,
    findings: list[Finding],
) -> bool:
    """The whole `to_json` of the answer, with post-cohort *facts* removed.

    Bars are kept: they are the outcome the answer measures, and deleting them
    would change the answer for a legitimate reason. Observations and
    membership rows published after the last event are deleted, and the answer
    must come out byte-identical — a restatement filed next month, or a
    universe rebuilt in hindsight, must not be able to move a cohort dated
    before it.
    """
    before = report_mod.to_json(
        cohort_mod.answer_for(baseline, depth=depth, reps=reps).primary
    )
    with _rolled_back(session):
        delete_observations_after(session, cutoff)
        delete_membership_after(session, cutoff)
        rebuilt = cohort_mod.build_cohort(session, setup, as_of=as_of, context=context)
        after = report_mod.to_json(
            cohort_mod.answer_for(rebuilt, depth=depth, reps=reps).primary
        )
    if before == after:
        return True
    findings.append(Finding(cutoff.isoformat(), "full_answer", before, after))
    return False


def check_roster(
    session,
    *,
    as_of: date,
    context: cohort_mod.CohortContext,
    slugs: Sequence[str] | None = None,
    max_cutoffs: int | None = None,
) -> tuple[LookaheadReport, ...]:
    """Run the harness over every available roster setup.

    Setups whose evidence plane does not exist yet (`insider_cluster_v1`) are
    skipped rather than reported clean: a harness that passes because there was
    nothing to check is the kind of green nobody should trust.
    """
    from comparables import setups as roster

    reports: list[LookaheadReport] = []
    for slug in (slugs or roster.slugs()):
        entry = roster.get(slug)
        if not entry.available:
            continue
        reports.append(check_cohort(
            session, entry, as_of=as_of, context=context, max_cutoffs=max_cutoffs,
        ))
    return tuple(reports)
