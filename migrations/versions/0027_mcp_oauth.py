"""FR-087: OAuth2 client-credentials fields for MCP servers

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('mcp_servers') as batch:
        batch.add_column(sa.Column('oauth_client_id', sa.String(300), nullable=True))
        batch.add_column(sa.Column('oauth_token_endpoint', sa.String(2000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('mcp_servers') as batch:
        batch.drop_column('oauth_token_endpoint')
        batch.drop_column('oauth_client_id')
