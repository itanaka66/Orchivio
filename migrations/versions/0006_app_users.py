"""application end users, role assignments and sessions

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('app_users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f('ck_app_users_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_app_users_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_users')),
    sa.UniqueConstraint('application_id', 'username', name='uq_app_users_username')
    )
    op.create_index(op.f('ix_app_users_application_id'), 'app_users', ['application_id'], unique=False)
    op.create_table('app_user_roles',
    sa.Column('app_user_id', sa.Uuid(), nullable=False),
    sa.Column('role_key', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['app_user_id'], ['app_users.id'], name=op.f('fk_app_user_roles_app_user_id_app_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('app_user_id', 'role_key', name=op.f('pk_app_user_roles'))
    )
    op.create_table('app_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('app_user_id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=128), nullable=False),
    sa.Column('csrf_hash', sa.String(length=128), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['app_user_id'], ['app_users.id'], name=op.f('fk_app_sessions_app_user_id_app_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_app_sessions_token_hash'))
    )
    op.create_index(op.f('ix_app_sessions_app_user_id'), 'app_sessions', ['app_user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('app_sessions')
    op.drop_table('app_user_roles')
    op.drop_table('app_users')
