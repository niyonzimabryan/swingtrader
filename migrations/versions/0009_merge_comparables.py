"""merge comparable registry

Revision ID: 0009_merge_comparables
Revises: 0008_merge_strategy_lab, 0005_comparable_registry
Create Date: 2026-09-10 00:33:32.785271

"""
from alembic import op
import sqlalchemy as sa


revision = '0009_merge_comparables'
down_revision = ('0008_merge_strategy_lab', '0005_comparable_registry')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
