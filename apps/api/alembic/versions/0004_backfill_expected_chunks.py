"""backfill expected_chunks for legacy complete documents

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14 11:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # expected_chunks 是 P1 新增字段；旧库完整文档该列为 0，导致“部分索引”语义不完整。
    # 仅回填 is_partial=false 的已索引文档：它们的 chunk_count 就是解析后的真实切片数。
    documents = sa.table(
        "documents",
        sa.column("expected_chunks", sa.Integer),
        sa.column("chunk_count", sa.Integer),
        sa.column("is_partial", sa.Boolean),
        sa.column("status", sa.String),
    )
    op.execute(
        documents.update()
        .where(
            documents.c.expected_chunks == 0,
            documents.c.is_partial == sa.false(),
            documents.c.chunk_count > 0,
            documents.c.status == "indexed",
        )
        .values(expected_chunks=documents.c.chunk_count)
    )


def downgrade() -> None:
    # 数据回填无法区分原始 0 与真实值，保留现状
    pass
