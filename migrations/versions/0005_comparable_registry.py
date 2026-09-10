"""Comparable-setups registry: comparable_queries, cohort_answers, cohort_predictions.

Revision ID: 0005_comparable_registry
Revises: 0004_merge_price_plane
Create Date: 2026-09-09

Phase 3c (Spec N §6.5, §7, §8). Branches from ``0004_merge_price_plane``, which
is the single head on ``main`` at the time this branch was cut (``alembic
heads``). Phases 1 and 2 are adding their own tables in parallel from the same
parent, so a merge revision at integration is expected and this revision must
not be edited to accommodate one — ``migrations/README.md``.

**No cross-phase foreign keys.** ``comparable_queries.price_snapshot_id`` refers
to ``price_snapshots.id`` (Phase 3p) and ``cohort_predictions.query_id`` to
``comparable_queries.id``, both by value. A constraint would make the phases'
integration merge a schema argument, and the join is one integer either way.

``cohort_predictions`` carries **no coverage column and no in-interval column**,
which is Spec N §6.5 as a schema fact rather than a convention:
``test_mean_ci_not_scored_as_prediction_interval`` reads the table's columns and
fails if one appears. The CI is on the cohort mean; scoring a single trade
against it would manufacture a failure out of a category error.
"""
from alembic import op
import sqlalchemy as sa

import database.types

revision = '0005_comparable_registry'
down_revision = '0004_merge_price_plane'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'comparable_queries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('setup_hash', sa.String(length=64), nullable=False),
        sa.Column('setup_slug', sa.String(length=80), nullable=False),
        sa.Column('setup_version', sa.String(length=20), nullable=False),
        sa.Column('family_slug', sa.String(length=160), nullable=False),
        sa.Column('setup_json', sa.Text(), nullable=False),
        sa.Column('requester_label', sa.String(length=80), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('price_snapshot_id', sa.Integer(), nullable=True),
        sa.Column('universe_slug', sa.String(length=64), nullable=False),
        sa.Column('candidate_source', sa.String(length=32), nullable=False),
        sa.Column('depth', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('evidence_tier', sa.String(length=32), nullable=False),
        sa.Column('refusal_reason', sa.Text(), nullable=True),
        sa.Column('n_matured', sa.Integer(), nullable=False),
        sa.Column('n_distinct_dates', sa.Integer(), nullable=False),
        sa.Column('trials_against_this_pattern', sa.Integer(), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('created_at', database.types.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_comparable_queries_family', 'comparable_queries',
        ['family_slug', 'created_at'], unique=False,
    )
    op.create_index(
        'ix_comparable_queries_setup', 'comparable_queries',
        ['setup_hash', 'as_of_date'], unique=False,
    )

    op.create_table(
        'cohort_answers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('setup_hash', sa.String(length=64), nullable=False),
        sa.Column('family_slug', sa.String(length=160), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('price_snapshot_id', sa.Integer(), nullable=False),
        sa.Column('depth', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('evidence_tier', sa.String(length=32), nullable=False),
        sa.Column('query_id', sa.Integer(), nullable=True),
        sa.Column('answer_json', sa.Text(), nullable=False),
        sa.Column('archival_block_json', sa.Text(), nullable=True),
        sa.Column('provenance_mix_json', sa.Text(), nullable=False),
        sa.Column('family_moments_json', sa.Text(), nullable=False),
        sa.Column('created_at', database.types.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'setup_hash', 'as_of_date', 'price_snapshot_id', 'depth',
            name='uq_cohort_answers_key',
        ),
    )
    op.create_index(
        'ix_cohort_answers_family', 'cohort_answers',
        ['family_slug', 'as_of_date'], unique=False,
    )

    op.create_table(
        'cohort_predictions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cohort_answer_id', sa.Integer(), nullable=False),
        sa.Column('query_id', sa.Integer(), nullable=True),
        sa.Column('setup_hash', sa.String(length=64), nullable=False),
        sa.Column('family_slug', sa.String(length=160), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('query_ticker', sa.String(length=20), nullable=False),
        sa.Column('horizon_sessions', sa.Integer(), nullable=False),
        sa.Column('point_estimate', sa.Float(), nullable=False),
        sa.Column('ci_low', sa.Float(), nullable=False),
        sa.Column('ci_high', sa.Float(), nullable=False),
        sa.Column('cohort_outcomes_json', sa.Text(), nullable=False),
        sa.Column('matures_on', sa.Date(), nullable=False),
        sa.Column('realized_return', sa.Float(), nullable=True),
        sa.Column('sign_correct', sa.Boolean(), nullable=True),
        sa.Column('realized_percentile', sa.Float(), nullable=True),
        sa.Column('scored_at', database.types.UtcDateTime(), nullable=True),
        sa.Column('created_at', database.types.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'cohort_answer_id', 'horizon_sessions', 'query_ticker',
            name='uq_cohort_predictions_answer_horizon',
        ),
    )
    op.create_index(
        'ix_cohort_predictions_due', 'cohort_predictions',
        ['matures_on', 'scored_at'], unique=False,
    )
    op.create_index(
        'ix_cohort_predictions_family', 'cohort_predictions',
        ['family_slug', 'as_of_date'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_cohort_predictions_family', table_name='cohort_predictions')
    op.drop_index('ix_cohort_predictions_due', table_name='cohort_predictions')
    op.drop_table('cohort_predictions')
    op.drop_index('ix_cohort_answers_family', table_name='cohort_answers')
    op.drop_table('cohort_answers')
    op.drop_index('ix_comparable_queries_setup', table_name='comparable_queries')
    op.drop_index('ix_comparable_queries_family', table_name='comparable_queries')
    op.drop_table('comparable_queries')
