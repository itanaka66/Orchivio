"""FR-087: add resources and prompts discovery columns to mcp_servers

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('mcp_servers') as batch:
        batch.add_column(sa.Column('resources', JSON, nullable=False, server_default='[]'))
        batch.add_column(sa.Column('resource_policy', JSON, nullable=False, server_default='{}'))
        batch.add_column(sa.Column('prompts', JSON, nullable=False, server_default='[]'))
        batch.add_column(sa.Column('prompt_policy', JSON, nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('mcp_servers') as batch:
        batch.drop_column('prompt_policy')
        batch.drop_column('prompts')
        batch.drop_column('resource_policy')
        batch.drop_column('resources')
