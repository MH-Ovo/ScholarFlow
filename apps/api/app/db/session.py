"""异步 SQLAlchemy 会话（SQLite 起步，可通过 DATABASE_URL 切换 PostgreSQL）。

SQLite 关键 PRAGMA：
- journal_mode=WAL：读写不互斥，SSE 轮询不会被后台写事务阻塞
- busy_timeout=30s：并发写（worker/入库/事件落库）时等待而不是立刻 database is locked
- synchronous=NORMAL：WAL 下的安全/性能折中
- foreign_keys=ON：SQLite 默认关闭外键，删除任务时依赖它级联/校验
"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
_database_url = settings.database_url
if _is_sqlite and ":///./" in _database_url:
    # 相对路径统一相对 apps/api，避免从仓库根目录启动时读到空库
    from app.core.config import BASE_DIR

    _database_url = _database_url.replace(
        ":///./", f":///{BASE_DIR.as_posix()}/"
    )
_connect_args = {"timeout": 30} if _is_sqlite else {}
engine = create_async_engine(
    _database_url,
    echo=False,
    future=True,
    pool_pre_ping=not _is_sqlite,
    connect_args=_connect_args,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Alembic 用同步驱动；asyncpg/aiosqlite 不能直接给 engine_from_config
SYNC_DATABASE_URL = (
    _database_url.replace("+aiosqlite", "").replace("+asyncpg", "").replace("+psycopg", "")
)


if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record) -> None:  # pragma: no cover - 连接级副作用
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session
