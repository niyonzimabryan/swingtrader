"""merge source_observations and workspace_tokens heads

Revision ID: 0003_merge_heads
Revises: 0002_source_observations, 0002_workspace_tokens
Create Date: 2026-09-09 07:58:45.367486

"""
from alembic import op
import sqlalchemy as sa


revision = '0003_merge_heads'
down_revision = ('0002_source_observations', '0002_workspace_tokens')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
