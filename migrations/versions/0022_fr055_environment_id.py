"""FR-055: environment_id on job_runs for scheduled jobs environment context

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('job_runs') as batch:
        batch.add_column(sa.Column('environment_id', sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_runs') as batch:
        batch.drop_column('environment_id')
