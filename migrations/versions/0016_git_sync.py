"""git synchronisation settings per application

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('git_syncs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('remote_url', sa.String(length=500), nullable=False),
    sa.Column('base_branch', sa.String(length=100), nullable=False),
    sa.Column('push_branch', sa.String(length=100), nullable=False),
    sa.Column('path', sa.String(length=100), nullable=False),
    sa.Column('token_ref', sa.String(length=300), nullable=True),
    sa.Column('provider', sa.String(length=10), nullable=False),
    sa.Column('last_push_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_push_commit', sa.String(length=40), nullable=True),
    sa.Column('last_push_revision_id', sa.Uuid(), nullable=True),
    sa.Column('last_pull_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_pull_commit', sa.String(length=40), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_git_syncs_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_git_syncs')),
    sa.UniqueConstraint('application_id', name=op.f('uq_git_syncs_application_id'))
    )


def downgrade() -> None:
    op.drop_table('git_syncs')
