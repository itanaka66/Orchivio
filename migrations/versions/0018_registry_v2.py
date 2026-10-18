"""registry: uninstall, renamed modules, dependencies, remote registries

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('registry_installs') as batch:
        batch.add_column(sa.Column('source_key', sa.String(length=63), nullable=False, server_default=''))
        batch.add_column(sa.Column('action', sa.String(length=10), nullable=False, server_default='install'))
        batch.add_column(sa.Column('dependency_of', sa.String(length=130), nullable=True))
    op.create_table('registry_remotes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=63), nullable=False),
    sa.Column('base_url', sa.String(length=500), nullable=False),
    sa.Column('token_ref', sa.String(length=300), nullable=True),
    sa.Column('remote_organization_id', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_registry_remotes_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_registry_remotes')),
    sa.UniqueConstraint('organization_id', 'name', name='uq_registry_remotes_name')
    )
    op.create_index(op.f('ix_registry_remotes_organization_id'), 'registry_remotes', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_table('registry_remotes')
    with op.batch_alter_table('registry_installs') as batch:
        batch.drop_column('dependency_of')
        batch.drop_column('action')
        batch.drop_column('source_key')
