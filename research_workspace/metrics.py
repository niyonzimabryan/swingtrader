"""Honesty metrics, the Brier score, and the calibration table (Spec M §6).

Printed whether or not they flatter, and refused when the sample cannot carry
them. Three properties are the point of this module:

**The original probability is what gets scored.** A thesis whose probability was
revised after entry keeps ``original_probability`` for scoring and shows the
revision history. Scoring the revised number would let a forecast be corrected
towards the outcome and still count as a forecast.

**Small n prints ``insufficient``.** A Brier score needs ten resolved theses;
the calibration table needs forty, and uses three coarse buckets below a
hundred. Below the floor the answer is the word, not a number with a wide
interval nobody will read.

**The decomposition is reported, not just the score.** Murphy's identity is
``BS = reliability − resolution + uncertainty``. The score alone cannot tell
"my 70%s happen 70% of the time" from "I only ever say 60%"; reliability and
resolution can. The decomposition is bucket-dependent — coarse buckets below the
coarse-bucket floor, deciles above it — and the returned block names the
buckets it used, because a decomposition whose binning is unstated is not
reproducible.

No model call anywhere in this module: it is arithmetic over recorded rows
(Spec K §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

from database.models import DecisionJournalEntry, Thesis, ThesisInvalidator
from utils.timeutils import utcnow_naive

INSUFFICIENT = "insufficient"

#: Spec M §6: three coarse buckets below a hundred resolutions.
COARSE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<=40%", 0.0, 0.40),
    ("40-60%", 0.40, 0.60),
    (">=60%", 0.60, 1.0),
)

#: Deciles, once there are enough resolutions to populate them.
DECILE_BUCKETS: tuple[tuple[str, float, float], ...] = tuple(
    (f"{i * 10}-{(i + 1) * 10}%", i / 10, (i + 1) / 10) for i in range(10)
)


@dataclass(frozen=True)
class Resolved:
    """One scored forecast: the number stated first, and what happened."""

    thesis_id: int
    ticker: str
    probability: float
    outcome: int
    revised: bool


def _settings(settings=None):
    if settings is None:
        from config.settings import Settings

        settings = Settings()
    return settings


def resolved_theses(session) -> list[Resolved]:
    """Every thesis with a stated probability and a recorded outcome.

    ``original_probability`` — never ``probability`` — because Spec M §6 says
    the revision does not get to be the forecast.
    """
    rows = (
        session.query(Thesis)
        .filter(Thesis.outcome.in_(("true", "false")))
        .filter(Thesis.original_probability.isnot(None))
        .order_by(Thesis.id.asc())
        .all()
    )
    return [
        Resolved(
            thesis_id=r.id,
            ticker=r.ticker,
            probability=float(r.original_probability),
            outcome=1 if r.outcome == "true" else 0,
            revised=len(r.probability_history) > 1,
        )
        for r in rows
    ]


def _buckets_for(n: int, settings) -> tuple[tuple[str, float, float], ...]:
    coarse_below = int(getattr(settings, "research_calibration_coarse_below", 100))
    return COARSE_BUCKETS if n < coarse_below else DECILE_BUCKETS


def _bucket_of(probability: float, buckets) -> str:
    for label, low, high in buckets:
        if low <= probability <= high if high >= 1.0 else low <= probability < high:
            return label
    return buckets[-1][0]  # pragma: no cover - probabilities are clamped to [0,1]


def brier(session, *, settings=None) -> dict:
    """The Brier score with its Murphy decomposition, or ``insufficient``.

    Reference point, stated so the number means something: Tetlock's
    superforecasters score roughly 0.20-0.25.
    """
    settings = _settings(settings)
    floor = int(getattr(settings, "research_brier_min_resolved", 10))
    scored = resolved_theses(session)
    n = len(scored)
    if n < floor:
        return {
            "status": INSUFFICIENT,
            "n_resolved": n,
            "floor": floor,
            "reason": (
                f"{n} resolved thesis/theses; a Brier score waits for {floor} "
                f"(Spec M §6). No score is printed below the floor."
            ),
        }

    base_rate = sum(s.outcome for s in scored) / n
    score = sum((s.probability - s.outcome) ** 2 for s in scored) / n

    buckets = _buckets_for(n, settings)
    reliability = 0.0
    resolution = 0.0
    per_bucket = []
    for label, low, high in buckets:
        members = [s for s in scored if _bucket_of(s.probability, buckets) == label]
        if not members:
            continue
        n_k = len(members)
        mean_forecast = sum(s.probability for s in members) / n_k
        mean_outcome = sum(s.outcome for s in members) / n_k
        reliability += n_k * (mean_forecast - mean_outcome) ** 2
        resolution += n_k * (mean_outcome - base_rate) ** 2
        per_bucket.append(
            {
                "bucket": label,
                "n": n_k,
                "mean_stated": round(mean_forecast, 4),
                "realized_frequency": round(mean_outcome, 4),
            }
        )
    reliability /= n
    resolution /= n
    uncertainty = base_rate * (1 - base_rate)

    return {
        "status": "ok",
        "n_resolved": n,
        "brier_score": round(score, 4),
        "reliability": round(reliability, 4),
        "resolution": round(resolution, 4),
        "uncertainty": round(uncertainty, 4),
        "identity_residual": round(score - (reliability - resolution + uncertainty), 9),
        "base_rate": round(base_rate, 4),
        "buckets_used": [b[0] for b in buckets],
        "decomposition_by_bucket": per_bucket,
        "n_revised_probabilities": sum(1 for s in scored if s.revised),
        "reference": "superforecaster Brier is roughly 0.20-0.25 (Spec M §6)",
        "scored_on": "original_probability",
    }


def calibration_table(session, *, settings=None) -> dict:
    """Stated bucket versus realized frequency, or ``insufficient``."""
    settings = _settings(settings)
    floor = int(getattr(settings, "research_calibration_min_resolved", 40))
    scored = resolved_theses(session)
    n = len(scored)
    if n < floor:
        return {
            "status": INSUFFICIENT,
            "n_resolved": n,
            "floor": floor,
            "reason": (
                f"{n} resolved thesis/theses; the calibration table waits for "
                f"{floor} (Spec M §6)."
            ),
        }
    buckets = _buckets_for(n, settings)
    rows = []
    for label, low, high in buckets:
        members = [s for s in scored if _bucket_of(s.probability, buckets) == label]
        rows.append(
            {
                "bucket": label,
                "n": len(members),
                "mean_stated": (
                    round(sum(s.probability for s in members) / len(members), 4)
                    if members
                    else None
                ),
                "realized_frequency": (
                    round(sum(s.outcome for s in members) / len(members), 4)
                    if members
                    else None
                ),
            }
        )
    return {"status": "ok", "n_resolved": n, "buckets_used": [b[0] for b in buckets], "rows": rows}


def honesty_metrics(session, *, now: datetime | None = None, settings=None) -> dict:
    """The four §6 numbers, including the ones that do not flatter.

    Two readings are stated rather than implied:

    * "hit rate on active theses" is scored over **resolved** theses. An active
      thesis has no outcome yet, so a hit rate over active ones would be a
      number about nothing; the count of live theses is reported alongside.
    * post-hoc invalidators are excluded from every count here (Spec M §4 rule
      3), and the excluded count is printed so the exclusion is visible rather
      than silent.
    """
    settings = _settings(settings)
    now = now or utcnow_naive()
    floor = int(getattr(settings, "research_brier_min_resolved", 10))

    theses = session.query(Thesis).order_by(Thesis.id.asc()).all()
    by_status: dict[str, int] = {}
    for thesis in theses:
        by_status[thesis.status] = by_status.get(thesis.status, 0) + 1

    scored = resolved_theses(session)
    hits = sum(
        1
        for s in scored
        if (s.probability >= 0.5 and s.outcome == 1)
        or (s.probability < 0.5 and s.outcome == 0)
    )
    hit_rate = (
        {"status": "ok", "n": len(scored), "hit_rate": round(hits / len(scored), 4)}
        if len(scored) >= floor
        else {
            "status": INSUFFICIENT,
            "n": len(scored),
            "floor": floor,
            "reason": f"{len(scored)} resolved; a hit rate waits for {floor}",
        }
    )

    invalidated = [t for t in theses if t.status == "invalidated"]
    days = [
        (t.closed_at - t.activated_at).days
        for t in invalidated
        if t.closed_at and t.activated_at
    ]
    finished = [t for t in theses if t.status in ("invalidated", "closed")]
    abandoned = [t for t in finished if t.close_reason == "abandoned"]

    invalidator_rows = session.query(ThesisInvalidator).all()
    post_hoc = [i for i in invalidator_rows if i.post_hoc]

    closes = (
        session.query(DecisionJournalEntry)
        .filter(DecisionJournalEntry.decision == "closed")
        .filter(DecisionJournalEntry.outcome_matched_reason.isnot(None))
        .all()
    )
    matched = (
        {
            "status": "ok",
            "n": len(closes),
            "share_matching": round(
                sum(1 for c in closes if c.outcome_matched_reason) / len(closes), 4
            ),
        }
        if closes
        else {
            "status": INSUFFICIENT,
            "n": 0,
            "reason": "no closed decision has had its outcome recorded yet",
        }
    )

    return {
        "as_of_utc": now.isoformat(),
        "theses": {
            "total": len(theses),
            "by_status": by_status,
            "resolved": len(scored),
        },
        "hit_rate_resolved": hit_rate,
        "median_days_to_invalidation": (
            {"status": "ok", "n": len(days), "median_days": median(days)}
            if days
            else {"status": INSUFFICIENT, "n": 0, "reason": "no thesis has been invalidated"}
        ),
        "invalidated_versus_abandoned": (
            {
                "status": "ok",
                "n_finished": len(finished),
                "invalidated": len(invalidated),
                "abandoned": len(abandoned),
                "share_abandoned": round(len(abandoned) / len(finished), 4),
            }
            if finished
            else {"status": INSUFFICIENT, "n_finished": 0, "reason": "no thesis has finished"}
        ),
        "journal_matched_exit_reason": matched,
        "invalidators": {
            "total": len(invalidator_rows),
            "counted": len(invalidator_rows) - len(post_hoc),
            "post_hoc_excluded": len(post_hoc),
            "note": (
                "post-hoc invalidators are kept and watched, and excluded from "
                "these metrics (Spec M §4 rule 3)"
            ),
        },
    }


def quarterly_report(session, *, settings=None) -> dict:
    """Everything Spec M §6 says to print quarterly, flattering or not."""
    settings = _settings(settings)
    return {
        "honesty": honesty_metrics(session, settings=settings),
        "brier": brier(session, settings=settings),
        "calibration": calibration_table(session, settings=settings),
    }
