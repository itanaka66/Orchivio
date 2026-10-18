"""process instances, user tasks and process events

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('process_instances',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('process', sa.String(length=130), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('state', JSON, nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('next_wake_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('initiator', sa.String(length=64), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('running', 'completed', 'failed', 'cancelled')", name=op.f('ck_process_instances_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_process_instances_application_id_applications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revision_id'], ['app_revisions.id'], name=op.f('fk_process_instances_revision_id_app_revisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_process_instances'))
    )
    op.create_index(op.f('ix_process_instances_application_id'), 'process_instances', ['application_id'], unique=False)
    op.create_index('ix_process_instances_wake', 'process_instances', ['next_wake_at'], unique=False)
    op.create_table('user_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('instance_id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('step_id', sa.String(length=40), nullable=False),
    sa.Column('token_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('assignee_role', sa.String(length=64), nullable=True),
    sa.Column('assignee_user', sa.String(length=64), nullable=True),
    sa.Column('delegated_by', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('outcomes', JSON, nullable=False),
    sa.Column('form', JSON, nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('escalate_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('escalate_to_role', sa.String(length=64), nullable=True),
    sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_by', sa.String(length=64), nullable=True),
    sa.Column('outcome', sa.String(length=31), nullable=True),
    sa.Column('result', JSON, nullable=True),
    sa.CheckConstraint("status IN ('open', 'completed', 'cancelled')", name=op.f('ck_user_tasks_status')),
    sa.ForeignKeyConstraint(['instance_id'], ['process_instances.id'], name=op.f('fk_user_tasks_instance_id_process_instances'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_tasks'))
    )
    op.create_index(op.f('ix_user_tasks_instance_id'), 'user_tasks', ['instance_id'], unique=False)
    op.create_index('ix_user_tasks_role', 'user_tasks', ['application_id', 'status', 'assignee_role'], unique=False)
    op.create_index('ix_user_tasks_user', 'user_tasks', ['application_id', 'status', 'assignee_user'], unique=False)
    op.create_index('ix_user_tasks_escalation', 'user_tasks', ['status', 'escalate_at'], unique=False)
    op.create_table('process_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('instance_id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('step_id', sa.String(length=40), nullable=True),
    sa.Column('actor', sa.String(length=64), nullable=True),
    sa.Column('data', JSON, nullable=False),
    sa.ForeignKeyConstraint(['instance_id'], ['process_instances.id'], name=op.f('fk_process_events_instance_id_process_instances'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_process_events'))
    )
    op.create_index(op.f('ix_process_events_instance_id'), 'process_events', ['instance_id'], unique=False)


def downgrade() -> None:
    op.drop_table('process_events')
    op.drop_table('user_tasks')
    op.drop_table('process_instances')
