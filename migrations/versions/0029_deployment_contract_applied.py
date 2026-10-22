"""FR-097: track when the contract migration phase was applied to a deployment

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('package_deployments') as batch:
        batch.add_column(sa.Column('contract_applied_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('package_deployments') as batch:
        batch.drop_column('contract_applied_at')
