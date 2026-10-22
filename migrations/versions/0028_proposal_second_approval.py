"""FR-015: second approver for risky (A3+) proposals

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('proposals') as batch:
        batch.drop_constraint('status', type_='check')
        batch.alter_column('status', type_=sa.String(32), existing_type=sa.String(16), existing_nullable=False)
        batch.create_check_constraint(
            'status',
            "status IN ('pending', 'pending_second_approval', 'approved', 'rejected', 'applied', 'superseded')"
        )
        batch.add_column(sa.Column('second_approved_by', sa.Uuid(), sa.ForeignKey('users.id'), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('proposals') as batch:
        batch.drop_column('second_approved_by')
        batch.drop_constraint('status', type_='check')
        batch.alter_column('status', type_=sa.String(16), existing_type=sa.String(32), existing_nullable=False)
        batch.create_check_constraint(
            'status',
            "status IN ('pending', 'approved', 'rejected', 'applied', 'superseded')"
        )
