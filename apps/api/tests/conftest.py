"""pytest 全局配置：在导入 app 之前固定测试环境，避免读到开发库/真实 Qdrant。"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="scholarflow-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}")
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("AUTH_LOCALHOST_BYPASS", "false")
os.environ.setdefault("API_KEYS", "key-a:tenant-a:user-a,key-b:tenant-b:user-b")
os.environ.setdefault("RESEARCH_WORKER_COUNT", "0")
os.environ.setdefault("CNKI_ENABLED", "false")
os.environ.setdefault("RERANK_ENABLED", "false")
os.environ.setdefault("QUERY_PROCESSING", "false")
os.environ.setdefault("RERANK_ENABLED", "false")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:1")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    import app.main as main

    async def _noop() -> None:
        return None

    # 不在测试里加载 bge-m3 / reranker（4GB+，CI 上没有模型）
    main._warmup_models = _noop  # type: ignore[assignment]
    main._warmup_cnki = _noop  # type: ignore[assignment]

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def key_a() -> dict[str, str]:
    return {"X-API-Key": "key-a"}


@pytest.fixture()
def key_b() -> dict[str, str]:
    return {"X-API-Key": "key-b"}
