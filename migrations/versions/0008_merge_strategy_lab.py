"""merge strategy lab domain into the evidence-planes head

Revision ID: 0008_merge_strategy_lab
Revises: 0007_merge_evidence_planes, 0007_strategy_lab
Create Date: 2026-09-09 19:01:44.654182

"""
from alembic import op
import sqlalchemy as sa


revision = '0008_merge_strategy_lab'
down_revision = ('0007_merge_evidence_planes', '0007_strategy_lab')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
