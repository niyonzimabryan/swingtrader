"""Workspace owner tokens (Spec K §4.1).

Revision ID: 0002_workspace_tokens
Revises: 0001_baseline
Create Date: 2026-09-09

Branches from the baseline, as every phase's first revision must
(``migrations/README.md``). One table, no foreign keys in either direction, so
the merge revision at integration with Phases 1-5 is trivial.

Only the SHA-256 digest of each token is stored; see
``database.models.WorkspaceToken`` for why a fast hash is the right one here.
"""
from alembic import op
import sqlalchemy as sa

import database.types

revision = '0002_workspace_tokens'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'workspace_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('token_prefix', sa.String(length=12), nullable=False),
        sa.Column('scopes', sa.String(length=200), nullable=False),
        sa.Column('created_at', database.types.UtcDateTime(), nullable=False),
        sa.Column('last_used_at', database.types.UtcDateTime(), nullable=True),
        sa.Column('revoked_at', database.types.UtcDateTime(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('workspace_tokens', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_workspace_tokens_label'), ['label'], unique=True
        )
        batch_op.create_index(
            batch_op.f('ix_workspace_tokens_token_hash'), ['token_hash'], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table('workspace_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workspace_tokens_token_hash'))
        batch_op.drop_index(batch_op.f('ix_workspace_tokens_label'))

    op.drop_table('workspace_tokens')
