"""Phase 4 evidence planes: ``entity_history``, ``news_clusters``, ``news_articles``.

Revision ID: 0003_phase4_planes
Revises: 0001_baseline
Create Date: 2026-09-09

Spec O sections 3.2 and 5. **Branches directly from ``0001_baseline``**, not
from Phase 3a's ``0002_source_observations``, and carries no foreign key to
any other phase's table — so the integration merge is a no-op join and no
phase's migration has to land before another's.

Three tables, and the argument for each is that ``source_observations`` cannot
hold the shape:

``entity_history``
    A resolution needs a half-open *interval* (``valid_from`` <= t <
    ``valid_to``) that gets closed later when a change is observed. An
    observation carries one ``valid_at`` instant.
``news_articles``
    Alpaca's terms bar sharing the data "or any derived products"
    (verification claim 11). Keeping article bodies in their own table makes
    "nothing news-derived leaves Postgres" a table-level rule rather than a
    payload filter over a ledger that is otherwise mirror-eligible.
``news_clusters``
    A story is a set of articles with a minimum publisher timestamp, and that
    minimum has to be updatable as members arrive. Ledger rows are immutable
    by design.

The news *facts* — the story event, ``consensus_eps_news`` — still land in
``source_observations`` through ``filings/observations.py`` like everything
else. These tables hold the working state the facts are derived from.
"""
from alembic import op
import sqlalchemy as sa

import database.types


revision = '0003_phase4_planes'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'entity_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('entity_cik', sa.String(length=10), nullable=False),
        sa.Column('attribute', sa.String(length=20), nullable=False),
        sa.Column('value', sa.String(length=200), nullable=False),
        sa.Column('valid_from', sa.Date(), nullable=False),
        sa.Column('valid_to', sa.Date(), nullable=True),
        sa.Column('valid_from_is_first_observation', sa.Boolean(), nullable=False),
        sa.Column('basis', sa.String(length=40), nullable=False),
        sa.Column('exchange', sa.String(length=40), nullable=True),
        sa.Column('source', sa.String(length=60), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('known_at_utc', database.types.UtcDateTime(), nullable=False),
        sa.Column('first_observed_at', database.types.UtcDateTime(), nullable=False),
        sa.Column('last_observed_at', database.types.UtcDateTime(), nullable=False),
        sa.Column('ingested_at', database.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'entity_cik', 'attribute', 'value', 'valid_from',
            name='uq_entity_history_interval',
        ),
    )
    with op.batch_alter_table('entity_history', schema=None) as batch_op:
        batch_op.create_index(
            'ix_entity_history_cik_attr',
            ['entity_cik', 'attribute', 'valid_from'],
            unique=False,
        )
        batch_op.create_index(
            'ix_entity_history_value', ['attribute', 'value', 'valid_from'], unique=False
        )

    op.create_table(
        'news_clusters',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cluster_id', sa.String(length=64), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False),
        sa.Column('known_at_utc', database.types.UtcDateTime(), nullable=True),
        sa.Column('member_count', sa.Integer(), nullable=False),
        sa.Column('novelty_score', sa.Float(), nullable=True),
        sa.Column('novelty_basis', sa.Text(), nullable=False),
        sa.Column('symbols', sa.Text(), nullable=False),
        sa.Column('replay_eligible', sa.Boolean(), nullable=False),
        sa.Column('updated_at', database.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cluster_id', name='uq_news_cluster_id'),
    )
    with op.batch_alter_table('news_clusters', schema=None) as batch_op:
        batch_op.create_index('ix_news_cluster_known_at', ['known_at_utc'], unique=False)

    op.create_table(
        'news_articles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('article_uid', sa.String(length=64), nullable=False),
        sa.Column('cluster_id', sa.String(length=64), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('provider_id', sa.String(length=80), nullable=True),
        sa.Column('publisher', sa.String(length=120), nullable=False),
        sa.Column('tier', sa.String(length=20), nullable=False),
        sa.Column('canonical_url', sa.Text(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False),
        sa.Column('lead', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('symbols', sa.Text(), nullable=False),
        sa.Column('published_at_utc', database.types.UtcDateTime(), nullable=True),
        sa.Column('first_seen_at_utc', database.types.UtcDateTime(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('revision_of_uid', sa.String(length=64), nullable=True),
        sa.Column('replay_eligible', sa.Boolean(), nullable=False),
        sa.Column('provenance_class', sa.String(length=30), nullable=False),
        sa.Column('quality_warnings', sa.Text(), nullable=False),
        sa.Column('ingested_at', database.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('article_uid', name='uq_news_article_uid'),
    )
    with op.batch_alter_table('news_articles', schema=None) as batch_op:
        batch_op.create_index('ix_news_article_cluster', ['cluster_id'], unique=False)
        batch_op.create_index('ix_news_article_published', ['published_at_utc'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('news_articles', schema=None) as batch_op:
        batch_op.drop_index('ix_news_article_published')
        batch_op.drop_index('ix_news_article_cluster')
    op.drop_table('news_articles')

    with op.batch_alter_table('news_clusters', schema=None) as batch_op:
        batch_op.drop_index('ix_news_cluster_known_at')
    op.drop_table('news_clusters')

    with op.batch_alter_table('entity_history', schema=None) as batch_op:
        batch_op.drop_index('ix_entity_history_value')
        batch_op.drop_index('ix_entity_history_cik_attr')
    op.drop_table('entity_history')
