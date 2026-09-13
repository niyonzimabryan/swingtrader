"""merge notify email cards and the owner control surface

Revision ID: 0013_merge_notify_owner
Revises: 0012_notify_email_cards, 0012_owner_control_surface
Create Date: 2026-09-13 03:58:51.286427

"""
from alembic import op
import sqlalchemy as sa


revision = '0013_merge_notify_owner'
down_revision = ('0012_notify_email_cards', '0012_owner_control_surface')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
