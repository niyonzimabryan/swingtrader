"""Engine-neutral SQLAlchemy column types.

The schema stores every timestamp as *naive UTC* in a
``TIMESTAMP WITHOUT TIME ZONE`` column (``DATETIME`` on SQLite). That choice
predates Postgres support and is kept deliberately: the codebase compares and
subtracts DB-loaded datetimes against :func:`utils.timeutils.utcnow_naive`, and
mixing aware and naive datetimes raises ``TypeError``.

The hazard is that neither engine *enforces* it, and they misbehave differently
when an aware datetime slips through:

* SQLite's ``DATETIME`` bind processor formats the datetime field-by-field and
  silently drops ``tzinfo``, so ``2026-01-01T00:00:00-05:00`` is stored as
  ``2026-01-01 00:00:00`` — five hours off from the UTC instant it names.
* Postgres coerces the same value into ``timestamp without time zone`` by
  discarding the offset, storing the same wall clock — also wrong, and wrong in
  a way that is invisible until two rows written from different call sites are
  compared.

``UtcDateTime`` closes that by normalising on the way in: an aware datetime is
converted to UTC and stripped, a naive one is passed through unchanged. The
underlying DDL is unchanged (``sa.DateTime()``), so this is a bind/result
behaviour fix, not a schema change — Alembic autogenerate sees the impl type.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """``DateTime`` that always reads and writes naive UTC on every engine."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        # Postgres never returns tzinfo for `timestamp without time zone`, but a
        # legacy SQLite string carrying an offset can round-trip as aware.
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
