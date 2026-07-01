"""Scorer primitives — the building blocks a per-app TaskSpec.score_item composes.

Each primitive is small and pure. The generic harness (run.evaluate) turns a list
of per-item scores into a mean + bootstrap CI; these functions produce those
per-item scores. PARITY tasks use agreement/overlap/recall/field_match/spearman;
DISCOVERY tasks use judge_delta (a signed per-item preference) or realized outcome.
"""
from __future__ import annotations

from statistics import mean


# ── PARITY primitives ────────────────────────────────────────────────────────
def agree(a, b) -> float:
    """1.0 if the two labels match exactly, else 0.0 (e.g. buy/skip agreement)."""
    return 1.0 if a == b else 0.0


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def recall(reference, candidate) -> float:
    """Fraction of the reference (positive) set the candidate kept.
    Winner-recall: reference = eventual winners, candidate = items the candidate did NOT kill."""
    reference = set(reference)
    if not reference:
        return 1.0
    return len(reference & set(candidate)) / len(reference)


def field_match(a: dict, b: dict, fields) -> float:
    """Fraction of `fields` on which two structured outputs agree."""
    fields = list(fields)
    if not fields:
        return 1.0
    return mean(1.0 if a.get(f) == b.get(f) else 0.0 for f in fields)


def spearman(xs, ys) -> float:
    """Spearman rank correlation, pure python. Returns 0.0 for degenerate input."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:            # average ranks for ties
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


# ── DISCOVERY primitives ─────────────────────────────────────────────────────
def judge_delta(vote_ab: str, vote_ba: str) -> float | None:
    """Collapse a position-swapped pairwise judge into a signed per-item preference.

    Run the judge twice: once with (incumbent=A, candidate=B), once swapped
    (candidate=A, incumbent=B). Each vote is the *winner position* ("A" or "B").
    Returns +1 if the candidate wins both, -1 if the incumbent wins both, 0 if the
    two disagree (order bias → no decision), None if a vote is malformed.

    This cancels position bias: a judge that always says "A" produces 0, not a
    spurious win for whichever contestant happened to be A.
    """
    def cand_won(vote, cand_is):
        v = (vote or "").strip().upper()
        if v not in ("A", "B"):
            return None
        return v == cand_is
    w1 = cand_won(vote_ab, "B")   # first pass: candidate is B
    w2 = cand_won(vote_ba, "A")   # swapped pass: candidate is A
    if w1 is None or w2 is None:
        return None
    if w1 and w2:
        return 1.0
    if not w1 and not w2:
        return -1.0
    return 0.0


def outcome_delta(candidate_outcome: float, incumbent_outcome: float) -> float:
    """Signed realized-outcome difference (P&L, CTR). MONITOR only — never gates
    a swap (design §2). Exposed so the P&L monitor and reports can use the same math."""
    return float(candidate_outcome) - float(incumbent_outcome)
