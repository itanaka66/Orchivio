"""version counters for requirements and stories (conflict detection for offline edits)

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('requirements', 'stories'):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))


def downgrade() -> None:
    for table in ('requirements', 'stories'):
        with op.batch_alter_table(table) as batch:
            batch.drop_column('version')
