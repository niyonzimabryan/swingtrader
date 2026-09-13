"""The owner control surface: one queue for every decision recorded over MCP.

Revision ID: 0012_owner_control_surface
Revises: 0011_comparable_subject_ticker
Create Date: 2026-09-13

Owner ruling, 2026-09-13 (Spec K §10, Spec L §10): Bryan approves proposals in
his coding-agent chat instead of Telegram. The ruling is safe because of the
shape this revision gives it — **a tool records a decision; the runtime acts on
one** — and that shape is one table.

``owner_actions`` holds one row per decision: approve or reject a Phase 6
proposal, approve or reject a scan memo, promote or demote a Strategy Lab arm.
The workspace writes the row; ``orchestrator/approval_poller.py``, in the bot
container where ``execution/`` is importable, claims it and acts on it. The
claim is a column on this row, so two pollers cannot both act, and for an order
approval it sits *in front of* the real single-use lock rather than replacing
it: ``execution/lifecycle.py::on_approval`` still consumes
``proposals.approval_consumed_at`` inside its own transaction.

A tier change additionally carries its own signed, expiring, owner-bound,
single-use confirmation (Spec Q §13), because unlike a proposal it has no
reference minted for it anywhere else.

**Nothing else in the schema changes.** No column is added to ``proposals`` and
none to ``memos`` — deliberately, and not only for tidiness:

* ``proposals.status`` and ``proposals.approval_consumed_at`` stay the execution
  state and the single-use lock, with one writer on the approval path. A second
  writer would make single-use a two-place invariant.
* ``memos`` is a **baseline-era table**. ``database/schema.py::classify`` adopts
  an unversioned production database by matching its table-and-column signature
  against a revision exactly, so a column added to a baseline table turns every
  un-adopted database into ``unknown`` at startup;
  ``tests/test_schema_discipline.py::test_a_baseline_era_database_is_still_adopted_after_a_new_table_lands``
  is the assertion that says so. A new table costs that path nothing, which is
  precisely why the signatures are computed from the migration graph.

``downgrade()`` drops the table. Reversible, and lossy only in the honest
direction: a recorded-but-unexecuted decision disappears, and nothing already
executed is undone, because execution is recorded on the subject's own row.
"""
from alembic import op
import sqlalchemy as sa

revision = '0012_owner_control_surface'
down_revision = '0011_comparable_subject_ticker'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'owner_actions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('action_uid', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('subject_kind', sa.String(length=20), nullable=False),
        sa.Column('subject_ref', sa.String(length=64), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('card_md', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('requested_by', sa.String(length=64), nullable=False),
        sa.Column('requested_token_label', sa.String(length=100), nullable=False),
        sa.Column('requested_at', sa.DateTime(), nullable=True),
        sa.Column('confirm_nonce', sa.String(length=32), nullable=True),
        sa.Column('confirm_signature', sa.String(length=64), nullable=True),
        sa.Column('confirm_expires_at', sa.DateTime(), nullable=True),
        sa.Column('confirm_owner_id', sa.String(length=64), nullable=False),
        sa.Column('confirm_consumed_at', sa.DateTime(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.Column('claimed_by', sa.String(length=64), nullable=False),
        sa.Column('executed_at', sa.DateTime(), nullable=True),
        sa.Column('outcome_code', sa.String(length=60), nullable=False),
        sa.Column('outcome_detail', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('approve_order', 'reject_order', 'approve_memo', "
            "'reject_memo', 'promote_arm', 'demote_arm')",
            name='ck_owner_actions_kind',
        ),
        sa.CheckConstraint(
            "subject_kind IN ('proposal', 'memo', 'arm')",
            name='ck_owner_actions_subject',
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'prepared', 'refused', 'confirmed', "
            "'executed', 'cancelled', 'expired')",
            name='ck_owner_actions_status',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('action_uid', name='uq_owner_actions_uid'),
    )
    op.create_index(
        'ix_owner_actions_status_created', 'owner_actions', ['status', 'created_at']
    )
    op.create_index(
        'ix_owner_actions_subject', 'owner_actions', ['subject_kind', 'subject_ref']
    )


def downgrade():
    op.drop_index('ix_owner_actions_subject', table_name='owner_actions')
    op.drop_index('ix_owner_actions_status_created', table_name='owner_actions')
    op.drop_table('owner_actions')
