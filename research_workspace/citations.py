"""Resolving a cited cohort answer id back to the answer (Spec N §8, Spec L §6.6).

An agent cannot hand a ``CohortAnswer`` across MCP — it cites an **id** that a
previous ``compare_setups`` call returned. So the journal needs a way to get
from that id back to the answer, and the answer store is Phase 3's to build.

This is the seam. Until Phase 3 registers a resolver, a citation cannot be
verified, and an unverifiable citation is refused rather than trusted: the
whole point of the evidenced budget is that the label cannot be self-awarded.
A decision can still be recorded — as ``discretionary``, which is what it
honestly is.
"""

from __future__ import annotations

_RESOLVER = None


def register_answer_resolver(resolver) -> None:
    """Register ``callable(answer_id) -> Answer | None``. Phase 3 calls this."""
    global _RESOLVER
    _RESOLVER = resolver


def clear_answer_resolver() -> None:
    """Test seam: the registry is process-global."""
    global _RESOLVER
    _RESOLVER = None


def has_resolver() -> bool:
    return _RESOLVER is not None


def resolve(answer_id: str):
    """The answer behind an id, or ``None`` if nothing can resolve it."""
    if _RESOLVER is None:
        return None
    return _RESOLVER(answer_id)
