"""module and template registry

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')
NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('registry_entries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=63), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('license', sa.String(length=100), nullable=False),
    sa.Column('visibility', sa.String(length=10), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('key_id', sa.String(length=32), nullable=False),
    sa.Column('artifact_key', sa.String(length=200), nullable=False),
    sa.Column('manifest', JSON, nullable=False),
    sa.Column('imported', sa.Boolean(), nullable=False),
    sa.Column('yanked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('yanked_reason', sa.String(length=300), nullable=True),
    sa.Column('published_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("kind IN ('module', 'template')", name=op.f('ck_registry_entries_kind')),
    sa.CheckConstraint("visibility IN ('org', 'instance')", name=op.f('ck_registry_entries_visibility')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_registry_entries_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_registry_entries')),
    sa.UniqueConstraint('organization_id', 'name', 'version', name='uq_registry_entries_version')
    )
    op.create_index(op.f('ix_registry_entries_organization_id'), 'registry_entries', ['organization_id'], unique=False)
    op.create_table('registry_installs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('package_ref', sa.String(length=130), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('module_key', sa.String(length=63), nullable=False),
    sa.Column('module_hash', sa.String(length=80), nullable=False),
    sa.Column('proposal_id', sa.Uuid(), nullable=True),
    sa.Column('installed_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_registry_installs_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_registry_installs'))
    )
    op.create_index(op.f('ix_registry_installs_application_id'), 'registry_installs', ['application_id'], unique=False)


def downgrade() -> None:
    op.drop_table('registry_installs')
    op.drop_table('registry_entries')
