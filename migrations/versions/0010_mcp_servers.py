"""external MCP servers for the AI Builder

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('mcp_servers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=63), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('secret_ref', sa.String(length=300), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('tools', JSON, nullable=False),
    sa.Column('tool_policy', JSON, nullable=False),
    sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f('ck_mcp_servers_status')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_mcp_servers_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_servers')),
    sa.UniqueConstraint('organization_id', 'name', name='uq_mcp_servers_name')
    )
    op.create_index(op.f('ix_mcp_servers_organization_id'), 'mcp_servers', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_table('mcp_servers')
