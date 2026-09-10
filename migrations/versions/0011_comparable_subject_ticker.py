"""The §6.6 subject ticker on a cohort query and its stored answer.

Revision ID: 0011_comparable_subject_ticker
Revises: 0010_merge_execution_lifecycle
Create Date: 2026-09-10

Spec L §6.6 gives an evidenced proposal at most one `cohort_answer_id` and
requires it to be "for the same ticker". Spec N §4.0 says a `SetupSpec` is a
typed predicate over a universe — a *pattern*, not a name. Both are right, and
together they mean the ticker cannot be read off a stored answer after the
fact: it has to be recorded when the question is asked. That is what these
columns are.

Four columns on each of `comparable_queries` and `cohort_answers`:

``subject_ticker``
    the one security the question was about, or `''` for a question asked about
    the pattern alone. Empty string rather than NULL because it joins the
    answer cache's unique key, and NULL never equals NULL on either engine —
    the same reasoning that made `price_snapshot_id` use `-1` (Spec N §4.0).
``subject_qualifies``
    whether that name met the setup's conditions at its most recent candidate
    on or before `as_of_date`, decided by the same `comparables/cohort.py`
    qualification pass the cohort members went through. **Nullable on purpose:**
    NULL means no subject was named, which is not the same statement as `false`.
``subject_reason`` / ``subject_event_date``
    why, and when the opportunity was. A citation's age is bounded to 5 sessions
    by Spec L §6.6; the *event's* age is not, so it is written down rather than
    implied.

The unique key on `cohort_answers` gains `subject_ticker`. It has to: the
citation an agent carries is `cohort:<cohort_answers.id>`, and with the subject
outside the key two questions about different names would share one row and one
id, so asking about a second name would silently re-point a citation already
written into the journal. Two subjects are therefore two rows with byte-
identical `answer_json` — the statistics do not depend on the subject — and two
citation ids, which is the honest shape.

No foreign keys, in keeping with the two revisions this one sits on top of.
Written through `op.batch_alter_table` so the constraint swap is a table rebuild
on SQLite and a plain `ALTER` on Postgres, and reversible on both.
"""
from alembic import op
import sqlalchemy as sa

revision = '0011_comparable_subject_ticker'
down_revision = '0010_merge_execution_lifecycle'
branch_labels = None
depends_on = None

TABLES = ('comparable_queries', 'cohort_answers')

#: Named so the constraint can be dropped by name on both engines. The original
#: is `0005_comparable_registry`'s; this revision replaces it in place rather
#: than editing that file, which another phase's history already descends from.
UNIQUE_KEY = 'uq_cohort_answers_key'
OLD_KEY_COLUMNS = ['setup_hash', 'as_of_date', 'price_snapshot_id', 'depth']
NEW_KEY_COLUMNS = OLD_KEY_COLUMNS + ['subject_ticker']


def _add_subject_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        # A server default on the add, so existing rows get `''` rather than
        # NULL in a NOT NULL column; the ORM default supplies it thereafter.
        batch_op.add_column(sa.Column(
            'subject_ticker', sa.String(length=20),
            nullable=False, server_default='',
        ))
        batch_op.add_column(sa.Column('subject_qualifies', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column(
            'subject_reason', sa.String(length=200),
            nullable=False, server_default='',
        ))
        batch_op.add_column(sa.Column('subject_event_date', sa.Date(), nullable=True))


def _drop_subject_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_column('subject_event_date')
        batch_op.drop_column('subject_reason')
        batch_op.drop_column('subject_qualifies')
        batch_op.drop_column('subject_ticker')


def upgrade() -> None:
    for table in TABLES:
        _add_subject_columns(table)

    with op.batch_alter_table('cohort_answers', schema=None) as batch_op:
        batch_op.drop_constraint(UNIQUE_KEY, type_='unique')
        batch_op.create_unique_constraint(UNIQUE_KEY, NEW_KEY_COLUMNS)


def downgrade() -> None:
    # Narrow the key back *before* dropping the column it names, or the drop
    # takes the constraint's meaning with it on one engine and not the other.
    with op.batch_alter_table('cohort_answers', schema=None) as batch_op:
        batch_op.drop_constraint(UNIQUE_KEY, type_='unique')
        batch_op.create_unique_constraint(UNIQUE_KEY, OLD_KEY_COLUMNS)

    for table in TABLES:
        _drop_subject_columns(table)
