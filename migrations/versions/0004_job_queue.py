"""background job queue and schedule state

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('job_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('flow', sa.String(length=130), nullable=False),
    sa.Column('args', JSON, nullable=False),
    sa.Column('run_as_role', sa.String(length=64), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('backoff_seconds', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('model_hash', sa.String(length=80), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('result', JSON, nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'dead', 'cancelled')", name=op.f('ck_job_runs_status')),
    sa.CheckConstraint("trigger IN ('schedule', 'manual')", name=op.f('ck_job_runs_trigger')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_job_runs_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_runs')),
    sa.UniqueConstraint('application_id', 'flow', 'scheduled_for', name='uq_job_runs_occurrence')
    )
    op.create_index(op.f('ix_job_runs_application_id'), 'job_runs', ['application_id'], unique=False)
    op.create_index('ix_job_runs_due', 'job_runs', ['status', 'available_at'], unique=False)
    op.create_table('schedule_state',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('flow', sa.String(length=130), nullable=False),
    sa.Column('last_planned_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_schedule_state_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_schedule_state')),
    sa.UniqueConstraint('application_id', 'flow', name='uq_schedule_state_flow')
    )
    op.create_index(op.f('ix_schedule_state_application_id'), 'schedule_state', ['application_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_schedule_state_application_id'), table_name='schedule_state')
    op.drop_table('schedule_state')
    op.drop_index('ix_job_runs_due', table_name='job_runs')
    op.drop_index(op.f('ix_job_runs_application_id'), table_name='job_runs')
    op.drop_table('job_runs')
