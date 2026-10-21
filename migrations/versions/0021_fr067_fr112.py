"""FR-067: app_user password-reset token fields; FR-112: process instance migration_policy

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-067: self-service password reset token on end-user accounts
    with op.batch_alter_table('app_users') as batch:
        batch.add_column(sa.Column('reset_token_hash', sa.String(64), nullable=True))
        batch.add_column(sa.Column('reset_token_expires_at', sa.DateTime(timezone=True), nullable=True))

    # FR-112: process instance migration policy (pin = stay on starting revision; migrate = apply new definition)
    with op.batch_alter_table('process_instances') as batch:
        batch.add_column(sa.Column('migration_policy', sa.String(10), server_default='pin', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('process_instances') as batch:
        batch.drop_column('migration_policy')
    with op.batch_alter_table('app_users') as batch:
        batch.drop_column('reset_token_expires_at')
        batch.drop_column('reset_token_hash')
