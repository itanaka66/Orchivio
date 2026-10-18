"""OIDC providers, login states and external identities for application users

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')
NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('app_oidc_providers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('issuer', sa.String(length=500), nullable=False),
    sa.Column('client_id', sa.String(length=300), nullable=False),
    sa.Column('client_secret_ref', sa.String(length=300), nullable=True),
    sa.Column('scopes', sa.String(length=300), nullable=False),
    sa.Column('username_claim', sa.String(length=64), nullable=False),
    sa.Column('role_claim', sa.String(length=64), nullable=False),
    sa.Column('role_mapping', JSON, nullable=False),
    sa.Column('default_roles', JSON, nullable=False),
    sa.Column('auto_provision', sa.Boolean(), nullable=False),
    sa.Column('allowed_email_domains', JSON, nullable=True),
    sa.Column('post_login_url', sa.String(length=500), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f('ck_app_oidc_providers_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_app_oidc_providers_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_oidc_providers')),
    sa.UniqueConstraint('application_id', name='uq_app_oidc_providers_app')
    )
    op.create_index(op.f('ix_app_oidc_providers_application_id'), 'app_oidc_providers', ['application_id'], unique=False)
    op.create_table('oidc_login_states',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('browser_hash', sa.String(length=64), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('code_verifier', sa.String(length=128), nullable=False),
    sa.Column('next_path', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oidc_login_states')),
    sa.UniqueConstraint('state_hash', name='uq_oidc_login_states_state')
    )
    op.create_index(op.f('ix_oidc_login_states_application_id'), 'oidc_login_states', ['application_id'], unique=False)
    op.create_table('app_user_identities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('app_user_id', sa.Uuid(), nullable=False),
    sa.Column('issuer', sa.String(length=500), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['app_user_id'], ['app_users.id'], name=op.f('fk_app_user_identities_app_user_id_app_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_user_identities')),
    sa.UniqueConstraint('application_id', 'issuer', 'subject', name='uq_app_user_identities_subject')
    )
    op.create_index(op.f('ix_app_user_identities_app_user_id'), 'app_user_identities', ['app_user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('app_user_identities')
    op.drop_table('oidc_login_states')
    op.drop_table('app_oidc_providers')
