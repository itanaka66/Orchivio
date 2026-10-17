"""allow proposals without a base revision (first revision of an application)

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('proposals') as batch:
        batch.alter_column('base_revision_id', existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Proposals for a first revision have no base; they cannot satisfy NOT NULL.
    op.execute("DELETE FROM proposals WHERE base_revision_id IS NULL")
    with op.batch_alter_table('proposals') as batch:
        batch.alter_column('base_revision_id', existing_type=sa.Uuid(), nullable=False)
