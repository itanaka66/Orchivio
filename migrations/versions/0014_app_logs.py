"""application log lines

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('app_logs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('level', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('message', sa.String(length=300), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=True),
    sa.Column('fields', JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name=op.f('ck_app_logs_level')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_app_logs_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_logs'))
    )
    op.create_index('ix_app_logs_app_time', 'app_logs', ['application_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_table('app_logs')
