"""FR-001: database-backed login throttle for multi-replica deployments

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'login_failures',
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('failed_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_login_failures_key_at', 'login_failures', ['key', 'failed_at'])


def downgrade() -> None:
    op.drop_index('ix_login_failures_key_at', table_name='login_failures')
    op.drop_table('login_failures')
