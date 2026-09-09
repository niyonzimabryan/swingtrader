"""Join the Phase 4 evidence-plane tables to the integrated head.

Revision ID: 0006_merge_evidence_planes
Revises: 0005_merge_portfolio_ledger, 0003_phase4_planes
Create Date: 2026-09-09

``0003_phase4_planes`` branches from ``0001_baseline``, which is the rule every
phase follows so no phase's migration has to land before another's. The cost is
an extra head, and ``alembic upgrade head`` refuses to pick between heads. This
revision is the join: it creates nothing and drops nothing.

It is the fourth such join in this graph — ``0003_merge_heads``,
``0004_merge_price_plane`` and ``0005_merge_portfolio_ledger`` are the same
shape, one per pair of phases that developed in parallel. That is the pattern
working, not a smell: each phase's table migration stayed independent of every
other phase's, and the ordering was decided once, at integration, in a file
that touches no schema.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision = '0006_merge_evidence_planes'
down_revision = ('0005_merge_portfolio_ledger', '0003_phase4_planes')
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Nothing to do: a merge point exists to make the graph single-headed."""


def downgrade() -> None:
    """Nothing to undo."""
