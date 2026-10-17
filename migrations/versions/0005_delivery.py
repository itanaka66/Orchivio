"""environments, constants, deployment packages and deployments

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')
NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('environments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('stage_order', sa.Integer(), nullable=False),
    sa.Column('requires_approval', sa.Boolean(), nullable=False),
    sa.Column('require_tests', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_environments_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_environments')),
    sa.UniqueConstraint('application_id', 'name', name='uq_environments_app_name'),
    sa.UniqueConstraint('application_id', 'stage_order', name='uq_environments_app_stage')
    )
    op.create_index(op.f('ix_environments_application_id'), 'environments', ['application_id'], unique=False)
    op.create_table('environment_constants',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('environment_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.String(length=2000), nullable=True),
    sa.Column('secret_ref', sa.String(length=300), nullable=True),
    sa.CheckConstraint('(value IS NULL) <> (secret_ref IS NULL)', name=op.f('ck_environment_constants_value_xor_secret_ref')),
    sa.ForeignKeyConstraint(['environment_id'], ['environments.id'], name=op.f('fk_environment_constants_environment_id_environments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_environment_constants')),
    sa.UniqueConstraint('environment_id', 'key', name='uq_environment_constants_key')
    )
    op.create_index(op.f('ix_environment_constants_environment_id'), 'environment_constants', ['environment_id'], unique=False)
    op.create_table('deployment_packages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('runtime_version', sa.String(length=40), nullable=False),
    sa.Column('model_hash', sa.String(length=80), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('key_id', sa.String(length=32), nullable=False),
    sa.Column('signature', sa.String(length=200), nullable=False),
    sa.Column('artifact_key', sa.String(length=200), nullable=False),
    sa.Column('manifest', JSON, nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_deployment_packages_application_id_applications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revision_id'], ['app_revisions.id'], name=op.f('fk_deployment_packages_revision_id_app_revisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployment_packages')),
    sa.UniqueConstraint('digest', name='uq_deployment_packages_digest')
    )
    op.create_index(op.f('ix_deployment_packages_application_id'), 'deployment_packages', ['application_id'], unique=False)
    op.create_index(op.f('ix_deployment_packages_revision_id'), 'deployment_packages', ['revision_id'], unique=False)
    op.create_table('package_deployments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('environment_id', sa.Uuid(), nullable=False),
    sa.Column('package_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('requested_by', sa.Uuid(), nullable=True),
    sa.Column('decided_by', sa.Uuid(), nullable=True),
    sa.Column('comment', sa.String(length=1000), nullable=True),
    sa.Column('risks_acknowledged', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending_approval', 'deployed', 'rejected', 'superseded')", name=op.f('ck_package_deployments_status')),
    sa.ForeignKeyConstraint(['environment_id'], ['environments.id'], name=op.f('fk_package_deployments_environment_id_environments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['package_id'], ['deployment_packages.id'], name=op.f('fk_package_deployments_package_id_deployment_packages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_package_deployments'))
    )
    op.create_index(op.f('ix_package_deployments_environment_id'), 'package_deployments', ['environment_id'], unique=False)
    op.create_index(op.f('ix_package_deployments_package_id'), 'package_deployments', ['package_id'], unique=False)


def downgrade() -> None:
    op.drop_table('package_deployments')
    op.drop_table('deployment_packages')
    op.drop_table('environment_constants')
    op.drop_table('environments')
