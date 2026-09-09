"""merge phase 3p (price plane) into the 0b/3a head

Revision ID: 0004_merge_price_plane
Revises: 0003_merge_heads, 0002_price_plane
Create Date: 2026-09-09

Phase 3p branched from ``0001_baseline`` in parallel with Phases 0b and 3a,
which were themselves joined by ``0003_merge_heads``. This is the integration
merge ``migrations/README.md`` prescribes: no DDL of its own, and neither side
rewritten.
"""
from alembic import op
import sqlalchemy as sa


revision = '0004_merge_price_plane'
down_revision = ('0003_merge_heads', '0002_price_plane')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
