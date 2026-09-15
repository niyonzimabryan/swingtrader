"""`broker_stop_probes`: the live protective-exit probe, recorded as evidence.

Revision ID: 0015_broker_stop_probes
Revises: 0014_securities_asset_class
Create Date: 2026-09-15

``docs/EXECUTION_LIFECYCLE.md`` §6 and Spec L §5.1 have both said "until this
probe passes, live entries stay closed" since Phase 6 shipped, and nothing
enforced it: ``execution/lifecycle.py::live_gate_refusal`` was a conjunction of
two environment variables and neither of them was the probe. This revision is
the half of the fix that has to be schema.

**Why a table rather than a flag.** ``ROBINHOOD_STOP_PROBE_PASSED=true`` would
be a third environment variable asserting something a human believes, which is
what ``ALLOW_LIVE_TRADING`` and ``EXECUTION_MODE`` already are. A row here is
written only by ``scripts/robinhood_stop_probe.py`` from an *observation* — a
real ``gtc`` ``stop_market`` read back out of ``get_equity_orders`` — and it
carries the broker's order id and the moment it was seen, so the claim can be
checked later against the broker's own history. The script places nothing; the
probe itself is an owner action performed by hand.

**The unique key is ``(broker, account_fingerprint)``**, because a probe proves
something about the account it ran in and nothing about any other. The
fingerprint is a SHA-256 of the account number: the key has to be exact — a
masked ``****1234`` can collide across accounts, and a collision in a gate reads
as permission — and the number itself has no business in a table that ends up in
backups and log lines. ``account_masked`` rides along only so a human can tell
which account a row is about.

A **new table**, not a column on ``proposals`` or on a baseline-era table. That
is the same reasoning ``0012_owner_control_surface`` wrote down:
``database/schema.py::classify`` adopts an unversioned production database by
matching its table-and-column signature against a revision exactly, so a column
added to a baseline table turns every un-adopted database into ``unknown`` at
startup. A new table costs that path nothing, and
``tests/test_schema_discipline.py`` asserts it.

No foreign key leaves this revision. ``brokerage_accounts`` is Phase 1's and is
keyed by a masked external id, which is precisely the ambiguity this table
refuses to depend on.

``downgrade()`` drops the table. Lossy in exactly one direction, and the safe
one: the record of a passed probe disappears, so the gate refuses live
Robinhood entries again until it is re-recorded. Absence means *not probed*
(Spec Q §12 invariant 1), which is the correct reading of a dropped table.
"""
from alembic import op
import sqlalchemy as sa

import database.types


revision = '0015_broker_stop_probes'
down_revision = '0014_securities_asset_class'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'broker_stop_probes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('broker', sa.String(length=32), nullable=False),
        sa.Column('account_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('account_masked', sa.String(length=16), nullable=False),
        sa.Column('observed_order_id', sa.String(length=64), nullable=False),
        sa.Column('observed_symbol', sa.String(length=32), nullable=False),
        sa.Column('observed_stop_price', sa.Float(), nullable=True),
        sa.Column('observed_at', database.types.UtcDateTime(), nullable=True),
        sa.Column('recorded_by', sa.String(length=64), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_at', database.types.UtcDateTime(), nullable=False),
        sa.Column('updated_at', database.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'broker', 'account_fingerprint', name='uq_broker_stop_probes_account'
        ),
    )


def downgrade() -> None:
    op.drop_table('broker_stop_probes')
