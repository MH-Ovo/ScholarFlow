"""document partial index flags and claim-level citation status

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('expected_chunks', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(
            sa.Column('is_partial', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table('citations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supported', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('citations', schema=None) as batch_op:
        batch_op.drop_column('supported')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('is_partial')
        batch_op.drop_column('expected_chunks')
