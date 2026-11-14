"""report share links

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14 10:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # 旧库每次启动会 create_all 先建出新表，这里必须幂等
    if sa.inspect(bind).has_table('report_shares'):
        return

    op.create_table(
        'report_shares',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('report_id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id']),
        sa.ForeignKeyConstraint(['task_id'], ['research_tasks.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('report_shares', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_shares_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_shares_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_shares_token'), ['token'], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table('report_shares'):
        return
    with op.batch_alter_table('report_shares', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_report_shares_token'))
        batch_op.drop_index(batch_op.f('ix_report_shares_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_report_shares_task_id'))
    op.drop_table('report_shares')
