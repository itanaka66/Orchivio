"""requirements, stories, trace links, preview feedback

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('requirements',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('draft', 'accepted', 'implemented', 'rejected')", name=op.f('ck_requirements_status')),
    sa.CheckConstraint("source IN ('manual', 'ai', 'feedback')", name=op.f('ck_requirements_source')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_requirements_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_requirements'))
    )
    op.create_index(op.f('ix_requirements_project_id'), 'requirements', ['project_id'], unique=False)
    op.create_table('stories',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('requirement_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('acceptance', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('proposed', 'backlog', 'ready', 'in_progress', 'done', 'rejected')", name=op.f('ck_stories_status')),
    sa.CheckConstraint("source IN ('manual', 'ai', 'feedback')", name=op.f('ck_stories_source')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_stories_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requirement_id'], ['requirements.id'], name=op.f('fk_stories_requirement_id_requirements'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stories'))
    )
    op.create_index(op.f('ix_stories_project_id'), 'stories', ['project_id'], unique=False)
    op.create_table('trace_links',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('from_type', sa.String(length=16), nullable=False),
    sa.Column('from_id', sa.String(length=200), nullable=False),
    sa.Column('to_type', sa.String(length=16), nullable=False),
    sa.Column('to_id', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("from_type IN ('requirement', 'story')", name=op.f('ck_trace_links_from_type')),
    sa.CheckConstraint("to_type IN ('requirement', 'story', 'element', 'revision', 'test')", name=op.f('ck_trace_links_to_type')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_trace_links_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trace_links')),
    sa.UniqueConstraint('project_id', 'from_type', 'from_id', 'to_type', 'to_id', name='uq_trace_links_edge')
    )
    op.create_index(op.f('ix_trace_links_project_id'), 'trace_links', ['project_id'], unique=False)
    op.create_table('feedback_items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('page_key', sa.String(length=200), nullable=False),
    sa.Column('element_id', sa.String(length=64), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('screenshot', sa.LargeBinary(), nullable=True),
    sa.Column('screenshot_type', sa.String(length=16), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('story_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('new', 'clustered', 'dismissed')", name=op.f('ck_feedback_items_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_feedback_items_application_id_applications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], name=op.f('fk_feedback_items_story_id_stories'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_feedback_items'))
    )
    op.create_index(op.f('ix_feedback_items_application_id'), 'feedback_items', ['application_id'], unique=False)


def downgrade() -> None:
    op.drop_table('feedback_items')
    op.drop_table('trace_links')
    op.drop_table('stories')
    op.drop_table('requirements')
