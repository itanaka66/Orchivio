"""FR-030/055/094/097: data_url on environments for production runtime

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('environments') as batch:
        batch.add_column(sa.Column('data_url', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('environments') as batch:
        batch.drop_column('data_url')
