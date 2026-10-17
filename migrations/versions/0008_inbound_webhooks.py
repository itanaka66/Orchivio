"""inbound webhooks, receipts, and the webhook job trigger

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')
NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    with op.batch_alter_table('job_runs') as batch:
        batch.add_column(sa.Column('source_ref', sa.String(length=200), nullable=True))
        batch.drop_constraint('trigger', type_='check')
        batch.create_check_constraint('trigger', "trigger IN ('schedule', 'manual', 'webhook')")
    op.create_table('inbound_webhooks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.String(length=63), nullable=False),
    sa.Column('flow', sa.String(length=130), nullable=False),
    sa.Column('run_as_role', sa.String(length=64), nullable=False),
    sa.Column('secret_ref', sa.String(length=300), nullable=False),
    sa.Column('arg_map', JSON, nullable=False),
    sa.Column('defaults', JSON, nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('active', 'paused')", name=op.f('ck_inbound_webhooks_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_inbound_webhooks_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inbound_webhooks')),
    sa.UniqueConstraint('application_id', 'key', name='uq_inbound_webhooks_key')
    )
    op.create_index(op.f('ix_inbound_webhooks_application_id'), 'inbound_webhooks', ['application_id'], unique=False)
    op.create_table('inbound_deliveries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('webhook_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['webhook_id'], ['inbound_webhooks.id'], name=op.f('fk_inbound_deliveries_webhook_id_inbound_webhooks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inbound_deliveries')),
    sa.UniqueConstraint('webhook_id', 'event_id', name='uq_inbound_deliveries_event')
    )
    op.create_index(op.f('ix_inbound_deliveries_webhook_id'), 'inbound_deliveries', ['webhook_id'], unique=False)


def downgrade() -> None:
    op.drop_table('inbound_deliveries')
    op.drop_table('inbound_webhooks')
    op.execute("DELETE FROM job_runs WHERE trigger = 'webhook'")
    with op.batch_alter_table('job_runs') as batch:
        batch.drop_constraint('trigger', type_='check')
        batch.create_check_constraint('trigger', "trigger IN ('schedule', 'manual')")
        batch.drop_column('source_ref')
