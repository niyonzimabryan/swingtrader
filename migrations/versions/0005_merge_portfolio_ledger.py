"""merge portfolio ledger into price-plane head

Revision ID: 0005_merge_portfolio_ledger
Revises: 0004_merge_price_plane, 0004_portfolio_ledger
Create Date: 2026-09-09 18:07:16.787367

"""
from alembic import op
import sqlalchemy as sa


revision = '0005_merge_portfolio_ledger'
down_revision = ('0004_merge_price_plane', '0004_portfolio_ledger')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
