"""`securities.asset_class`: an instrument is an equity or a fund.

Revision ID: 0014_securities_asset_class
Revises: 0013_merge_notify_owner
Create Date: 2026-09-13

Sharadar keeps equities in `stocks` (legacy `SEP`) and funds in `funds`
(legacy `SFP`), and SPY exists **only** in the second one. Before this
revision `data/prices/sharadar.py` sent `table=stocks` everywhere, so the
benchmark the whole of Spec N §5.2 measures abnormal returns against was
unreachable and `COMPARABLE_BENCHMARK_SECURITY_UID` could not be set.

Reaching the fund table is an adapter change. What needs a schema change is
the consequence: once ETFs are ingestible, `data/prices/universes.py` must be
able to *exclude* them, because a benchmark that is also a member of the
liquid-equity universe is a cohort measured against itself. The class has to
be a stored property of the security, not a guess from the symbol — "SPY is an
ETF" is not something a three-letter string tells you.

One column, `NOT NULL`, `equity` by default. `equity` is not a convenient
default, it is the true value for every row that already exists: the only
table the adapter could read before this revision was `stocks`.

`securities` is a **Phase 3p** table (`0002_price_plane`), not a baseline-era
one, so adding a column to it does not disturb
`database/schema.py::classify`'s signature match for an unversioned
production database — the trap `migrations/README.md` documents and
`tests/test_schema_discipline.py` asserts. Adding this column to `memos`,
`trades` or `tickers` would have been a different story.

The server default exists only to fill existing rows during the `ALTER` and is
dropped immediately afterwards, in a second batch block: `database/models.py`
declares this column with a Python-side `default="equity"` like every other
column on the table, and `tests/test_schema_discipline.py` compares
`upgrade head` against `create_all()` column by column, so a server default
left behind is schema drift and the test is right to say so.

`downgrade()` drops the column. It is lossy in one specific way worth naming:
a database that had ingested funds loses the record of *which* securities were
funds, so a universe rebuild on the downgraded schema would rank an ETF into
"liquid US equities" — which is the very thing this column exists to prevent.
It does not refuse, because the rows themselves survive and re-running the
upgrade plus a master refresh restores the fact from the vendor.
"""
from alembic import op
import sqlalchemy as sa

revision = '0014_securities_asset_class'
down_revision = '0013_merge_notify_owner'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('securities', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'asset_class', sa.String(length=16),
            nullable=False, server_default='equity',
        ))

    # A separate batch: alembic applies one table rebuild per block on SQLite,
    # and a column cannot be altered in the same block that adds it.
    with op.batch_alter_table('securities', schema=None) as batch_op:
        batch_op.alter_column(
            'asset_class', existing_type=sa.String(length=16),
            existing_nullable=False, server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table('securities', schema=None) as batch_op:
        batch_op.drop_column('asset_class')
