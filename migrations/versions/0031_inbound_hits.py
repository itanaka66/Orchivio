"""FR-082: database-backed inbound webhook rate limiter for multi-replica deployments

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'inbound_hits',
        sa.Column('webhook_id', sa.Uuid(), nullable=False),
        sa.Column('hit_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_inbound_hits_wid_at', 'inbound_hits', ['webhook_id', 'hit_at'])


def downgrade() -> None:
    op.drop_index('ix_inbound_hits_wid_at', table_name='inbound_hits')
    op.drop_table('inbound_hits')
