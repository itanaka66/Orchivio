"""outbound webhook subscriptions and deliveries

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')
NOW = sa.text('(CURRENT_TIMESTAMP)')


def upgrade() -> None:
    op.create_table('webhook_subscriptions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('secret_ref', sa.String(length=300), nullable=False),
    sa.Column('events', JSON, nullable=False),
    sa.Column('fields', JSON, nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('paused_reason', sa.String(length=300), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint("status IN ('active', 'paused')", name=op.f('ck_webhook_subscriptions_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_webhook_subscriptions_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_subscriptions')),
    sa.UniqueConstraint('application_id', 'name', name='uq_webhook_subscriptions_name')
    )
    op.create_index(op.f('ix_webhook_subscriptions_application_id'), 'webhook_subscriptions', ['application_id'], unique=False)
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('subscription_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.String(length=36), nullable=False),
    sa.Column('event_type', sa.String(length=200), nullable=False),
    sa.Column('payload', JSON, nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'sending', 'delivered', 'dead')", name=op.f('ck_webhook_deliveries_status')),
    sa.ForeignKeyConstraint(['subscription_id'], ['webhook_subscriptions.id'], name=op.f('fk_webhook_deliveries_subscription_id_webhook_subscriptions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries')),
    sa.UniqueConstraint('subscription_id', 'event_id', name='uq_webhook_deliveries_event')
    )
    op.create_index(op.f('ix_webhook_deliveries_subscription_id'), 'webhook_deliveries', ['subscription_id'], unique=False)
    op.create_index('ix_webhook_deliveries_due', 'webhook_deliveries', ['status', 'available_at'], unique=False)


def downgrade() -> None:
    op.drop_table('webhook_deliveries')
    op.drop_table('webhook_subscriptions')
