"""FR-067: platform user password reset tokens; member invitation table

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('reset_token_hash', sa.String(64), nullable=True))
        batch.add_column(sa.Column('reset_token_expires_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'invitations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_by', sa.Uuid(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE',
                                name='fk_invitations_organization_id_organizations'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'],
                                name='fk_invitations_created_by_users'),
        sa.ForeignKeyConstraint(['accepted_by'], ['users.id'],
                                name='fk_invitations_accepted_by_users'),
        sa.CheckConstraint("role IN ('owner','admin','editor','viewer')", name='ck_invitations_role'),
        sa.CheckConstraint("status IN ('pending','accepted','cancelled')", name='ck_invitations_status'),
        sa.PrimaryKeyConstraint('id', name='pk_invitations'),
        sa.UniqueConstraint('token_hash', name='uq_invitations_token_hash'),
    )
    op.create_index('ix_invitations_organization_id', 'invitations', ['organization_id'])


def downgrade() -> None:
    op.drop_index('ix_invitations_organization_id', 'invitations')
    op.drop_table('invitations')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('reset_token_expires_at')
        batch.drop_column('reset_token_hash')
