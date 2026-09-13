"""The notification delivery log and the stored card.

Revision ID: 0012_notify_email_cards
Revises: 0011_comparable_subject_ticker
Create Date: 2026-09-13

Two tables, and the reason they are two.

``cards``
    one row per rendered card, keyed by the opaque ``card_uid`` that appears in
    the signed link. ``payload_json`` is the card's **source data** — every
    number, every provenance and staleness flag, and the price bars behind the
    chart, exactly as they stood when the card was minted. The page at
    ``/cards/<uid>`` re-renders from this and from nothing else, so a card
    opened months later says what the email said rather than what the ledger
    says today (AGENTS.md §1.2, §1.3).

``notifications_sent``
    one row per delivery attempt per channel, append-only, written on success
    and on failure alike. A channel failure never unwinds the row it was telling
    the owner about (``portfolio.approvals.send_card``), so the only place a
    silent delivery failure can be seen is here.

They are separate because a card is a resource and a delivery is an event: the
same card goes to email and to Telegram, and a re-send is another delivery of
the same card. Folding the payload into the delivery row would store it once per
channel per attempt, and would leave the page having to choose which duplicate
is canonical.

No foreign keys, in keeping with every revision this one sits on top of;
``notifications_sent.card_uid`` is an ordinary string that is empty for a
notification that carried no card.

``downgrade()`` drops both tables. That is lossless in the sense that matters:
neither table is read by anything that decides an order, a size, or a number —
they are a delivery log and a rendering cache. Every link minted before a
downgrade stops resolving, which is the honest outcome of removing the table
the page reads.
"""
from alembic import op
import sqlalchemy as sa

revision = '0012_notify_email_cards'
down_revision = '0011_comparable_subject_ticker'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if 'cards' not in existing:
        op.create_table(
            'cards',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('card_uid', sa.String(length=64), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('ref', sa.String(length=120), nullable=False),
            sa.Column('subject', sa.Text(), nullable=False),
            sa.Column('payload_json', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        # `Card.card_uid` is declared `unique=True, index=True`, which SQLAlchemy
        # renders as a single UNIQUE index and no separate constraint. Adding a
        # named UniqueConstraint here as well would leave the schema one object
        # ahead of the models, which `compare_metadata` catches and which makes
        # every future autogenerate noisy.
        op.create_index('ix_cards_card_uid', 'cards', ['card_uid'], unique=True)
        op.create_index('ix_cards_kind_ref', 'cards', ['kind', 'ref'], unique=False)
        op.create_index('ix_cards_created_at', 'cards', ['created_at'], unique=False)

    if 'notifications_sent' not in existing:
        op.create_table(
            'notifications_sent',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('ref', sa.String(length=120), nullable=False),
            sa.Column('channel', sa.String(length=32), nullable=False),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('provider_id', sa.String(length=120), nullable=False),
            sa.Column('error', sa.Text(), nullable=False),
            sa.Column('card_uid', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ix_notifications_sent_kind_ref', 'notifications_sent', ['kind', 'ref'], unique=False
        )
        op.create_index(
            'ix_notifications_sent_created_at', 'notifications_sent', ['created_at'], unique=False
        )


def downgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if 'notifications_sent' in existing:
        op.drop_index('ix_notifications_sent_created_at', table_name='notifications_sent')
        op.drop_index('ix_notifications_sent_kind_ref', table_name='notifications_sent')
        op.drop_table('notifications_sent')

    if 'cards' in existing:
        op.drop_index('ix_cards_created_at', table_name='cards')
        op.drop_index('ix_cards_kind_ref', table_name='cards')
        op.drop_index('ix_cards_card_uid', table_name='cards')
        op.drop_table('cards')
