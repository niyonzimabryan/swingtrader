"""merge research workspace into the ledger head

Revision ID: 0006_merge_research_workspace
Revises: 0005_merge_portfolio_ledger, 0004_research_workspace
Create Date: 2026-09-09 18:09:38.747150

"""
from alembic import op
import sqlalchemy as sa


revision = '0006_merge_research_workspace'
down_revision = ('0005_merge_portfolio_ledger', '0004_research_workspace')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
