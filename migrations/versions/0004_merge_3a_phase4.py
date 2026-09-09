"""Merge Phase 3a's ledger and Phase 4's plane tables into one head.

Revision ID: 0004_merge_3a_phase4
Revises: 0002_source_observations, 0003_phase4_planes
Create Date: 2026-09-09

Both parents branch from ``0001_baseline`` — that is the rule every phase
follows so no phase's migration has to land before another's. The cost is two
heads, and ``alembic upgrade head`` refuses to pick between them. This
revision is the join: it creates nothing and drops nothing.

Phases 0b and 3p are adding tables on the same pattern; when they merge, this
file is not edited — another merge revision is written with the new heads as
its parents.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision = '0004_merge_3a_phase4'
down_revision = ('0002_source_observations', '0003_phase4_planes')
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Nothing to do: a merge point exists to make the graph single-headed."""


def downgrade() -> None:
    """Nothing to undo."""
