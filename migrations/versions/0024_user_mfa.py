"""FR-067: TOTP MFA secret and flag on users

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('mfa_secret', sa.Text(), nullable=True))
        batch.add_column(sa.Column('mfa_enabled', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('mfa_enabled')
        batch.drop_column('mfa_secret')
