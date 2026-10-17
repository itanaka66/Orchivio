"""integration destinations (base URL and credential reference per application)

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('integration_destinations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=63), nullable=False),
    sa.Column('base_url', sa.String(length=2000), nullable=False),
    sa.Column('auth_type', sa.String(length=10), nullable=False),
    sa.Column('auth_header', sa.String(length=64), nullable=True),
    sa.Column('secret_ref', sa.String(length=300), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("auth_type IN ('none', 'bearer', 'header')", name=op.f('ck_integration_destinations_auth_type')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_integration_destinations_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_destinations')),
    sa.UniqueConstraint('application_id', 'name', name='uq_integration_destinations_name')
    )
    op.create_index(op.f('ix_integration_destinations_application_id'), 'integration_destinations', ['application_id'], unique=False)


def downgrade() -> None:
    op.drop_table('integration_destinations')
