"""Failure modes the SEC plane refuses to swallow."""

from __future__ import annotations


class AdapterSchemaError(RuntimeError):
    """A provider payload did not have the shape the adapter was written for.

    Spec O section 6, ``test_adapter_schema_change_fails_loudly``: when SEC
    changes a field name, drops a key, or starts returning a null where a
    number used to be, the adapter raises. It never coerces the surprise into
    a null and writes it, because a null in ``source_observations`` is
    indistinguishable from a fact that genuinely did not exist, and the second
    one is a legitimate answer to a point-in-time question.
    """


class PlaneDisabled(RuntimeError):
    """``PLANE_SEC_MINIMAL_ENABLED`` is false, so nothing may be ingested.

    Every new capability ships behind a flag defaulting to off. Reads of
    whatever is already in ``source_observations`` are unaffected — the ledger
    is a table, not a capability.
    """
