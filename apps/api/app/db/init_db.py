"""数据库初始化：Alembic 迁移 + 兼容旧库的列补齐。

新库：直接 upgrade head（0001 baseline 建全部表）。
旧库（Phase 1 起的 create_all 库）：create_all 补建缺失表 → 逐列 ALTER 补齐 →
stamp 0001。这样 140 篇知识库不需要重建，也不会丢数据。
"""
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.db.session import SYNC_DATABASE_URL, Base, engine
from app.models import research as _models  # noqa: F401  确保全部模型注册

API_DIR = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", SYNC_DATABASE_URL.replace("%", "%%"))
    return cfg


def _has_table(conn, name: str) -> bool:
    return name in inspect(conn).get_table_names()


def _ensure_legacy_columns(conn) -> None:
    """SQLite/PG 通用的 ADD COLUMN；只补缺失列，不修改已有列。"""
    is_sqlite = conn.dialect.name == "sqlite"
    bool_false = "0" if is_sqlite else "false"

    specs: dict[str, list[tuple[str, str]]] = {
        "research_tasks": [
            ("tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
            ("user_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("lang", "VARCHAR(8) NOT NULL DEFAULT 'zh'"),
            ("attempt", "INTEGER NOT NULL DEFAULT 0"),
            ("cancel_requested", f"BOOLEAN NOT NULL DEFAULT {bool_false}"),
            ("error", "TEXT NOT NULL DEFAULT ''"),
            ("queued_at", "TIMESTAMP"),
            ("started_at", "TIMESTAMP"),
        ],
        "documents": [
            ("tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
            ("content_hash", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("expected_chunks", "INTEGER NOT NULL DEFAULT 0"),
            ("is_partial", f"BOOLEAN NOT NULL DEFAULT {bool_false}"),
        ],
        "citations": [
            ("supported", "BOOLEAN"),
        ],
        "reports": [
            ("tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
            ("degraded", f"BOOLEAN NOT NULL DEFAULT {bool_false}"),
            ("degraded_reason", "VARCHAR(300) NOT NULL DEFAULT ''"),
        ],
    }
    insp = inspect(conn)
    for table, columns in specs.items():
        if not _has_table(conn, table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    index_sql = [
        "CREATE INDEX IF NOT EXISTS ix_research_tasks_tenant_id ON research_tasks (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_documents_tenant_id ON documents (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_documents_content_hash ON documents (content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_reports_tenant_id ON reports (tenant_id)",
    ]
    for sql in index_sql:
        try:
            conn.execute(text(sql))
        except Exception:
            # 索引已存在/方言不支持 IF NOT EXISTS 时忽略，启动不因索引失败中断
            pass


async def init_db() -> None:
    legacy = False
    has_version = False
    async with engine.begin() as conn:
        legacy = await conn.run_sync(_has_table, "research_tasks")
        has_version = await conn.run_sync(_has_table, "alembic_version")

    if legacy:
        # 每次启动都做一次幂等补齐：防止迁移中断/手工改库造成列漂移
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_legacy_columns)

    await asyncio.to_thread(_run_alembic, legacy, has_version)


def _run_alembic(legacy: bool, has_version: bool) -> None:
    cfg = _alembic_config()
    if legacy and not has_version:
        # 旧库已由 create_all + ALTER 对齐到当前 schema，先打基线再升级到 head
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")
