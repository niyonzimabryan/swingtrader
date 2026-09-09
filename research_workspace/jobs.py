"""The scheduled entry points. Pure Python, no model call (Spec K §5).

Three jobs, all of which produce "nothing to report" without inventing content:

``daily_invalidator_check``
    Evaluates every checkable invalidator on every live thesis, pages on a
    trigger, and moves the thesis to ``weakened``. Never touches a position.
``review_queue``
    Theses past ``next_review_at``, for the Sunday report (Spec M §6).
``quarterly_honesty_report``
    The §6 numbers, printed whether or not they flatter.

Each opens its own session so a scheduler can call it with no arguments, and
each also accepts one so a test — or a caller already inside a transaction —
can pass its own.
"""

from __future__ import annotations

from contextlib import contextmanager

from research_workspace import invalidators, metrics, render, store
from utils.logger import get_logger

log = get_logger("research_jobs")


@contextmanager
def _session(session=None):
    if session is not None:
        yield session
        return
    from database.db import get_session

    with get_session() as own:
        yield own


def daily_invalidator_check(session=None, *, settings=None, now=None, price_source=None):
    with _session(session) as active:
        return invalidators.run_daily_check(
            active, settings=settings, now=now, price_source=price_source
        )


def review_queue(session=None, *, on=None, settings=None) -> list[dict]:
    """Theses due for review, staleness first.

    Spec M §6 ranks by position size × staleness. Position size is Spec L's
    ledger — a parallel phase — so this ranks by staleness alone rather than
    inventing a size, and says so in the payload.
    """
    with _session(session) as active:
        due = store.due_for_review(active, on=on)
        return [
            {
                **render.thesis_payload(active, thesis),
                "ranking": "staleness only; position size arrives with Spec L",
            }
            for thesis in due
        ]


def quarterly_honesty_report(session=None, *, settings=None) -> dict:
    with _session(session) as active:
        report = metrics.quarterly_report(active, settings=settings)
    log.info(
        "research_honesty_report",
        brier=report["brier"]["status"],
        calibration=report["calibration"]["status"],
        resolved=report["honesty"]["theses"]["resolved"],
    )
    return report
