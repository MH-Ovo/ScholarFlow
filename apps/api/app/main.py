"""FastAPI 应用入口：uvicorn app.main:app --reload --port 8900"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import analytics, auth, documents, research, sources
from app.core.auth import resolve_request
from app.core.config import get_settings
from app.db.init_db import init_db
from app.llm.gateway import get_llm

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if not settings.auth_enabled:
        print("[kra][auth] 警告：AUTH_ENABLED=false，所有接口无鉴权（仅限本机临时使用）")
    elif not settings.api_keys.strip() and not settings.auth_localhost_bypass:
        raise RuntimeError(
            "AUTH_ENABLED=true 且 AUTH_LOCALHOST_BYPASS=false，但 API_KEYS 为空；"
            "请配置至少一个 key:tenant:user"
        )
    from app.agents.events import EVENT_BUS
    from app.services import research_service

    await EVENT_BUS.start()
    await research_service.recover_tasks()
    await research_service.start_workers()
    llm = get_llm()
    print(
        f"[kra] LLM mode = {llm.mode} | WebSearch = "
        f"{'deepseek' if settings.llm_enabled else 'mock'}"
    )
    if settings.warmup_models:
        asyncio.create_task(_warmup_models())
    if settings.cnki_enabled and (getattr(settings, "cnki_mode", "auto") or "auto").strip().lower() != "fallback":
        asyncio.create_task(_warmup_cnki())
    try:
        yield
    finally:
        await research_service.stop_workers()
        await EVENT_BUS.stop()


async def _warmup_cnki() -> None:
    """后台预热知网 MCP 长连接。

    该 MCP 每次冷启动都要拉起浏览器（实测 20–60 秒），放在启动阶段预热，
    避免第一次调研把这段时间算进用户等待。失败静默，首次真实调用会再尝试。
    """
    import time

    t0 = time.time()
    try:
        from app.integrations import cnki_mcp

        res = await cnki_mcp.warmup()
        if res.get("status") == "ok":
            print(f"[kra] 知网 MCP 预热完成（{time.time() - t0:.0f}s）", flush=True)
        else:
            print(f"[kra] 知网 MCP 未就绪：{res.get('detail')}", flush=True)
    except Exception as exc:
        print(f"[kra] 知网 MCP 预热跳过：{type(exc).__name__}: {exc}", flush=True)


async def _warmup_models() -> None:
    """后台预热本地模型：首问不再承担模型加载耗时（CPU 上约 20-40s）。

    模型加载/推理是同步 CPU 密集操作，必须放线程池执行——
    直接在协程里调用会阻塞事件循环（/health 等全部接口无响应）。失败静默（首次真实调用会再尝试）。
    """
    import time

    t0 = time.time()
    try:
        from app.rag.models import encode_hybrid, get_reranker, rerank_scores

        await asyncio.to_thread(encode_hybrid, ["预热"])
        reranker = await asyncio.to_thread(get_reranker)
        if reranker is not None:
            await asyncio.to_thread(rerank_scores, "预热", ["预热"])
        print(f"[kra] 模型预热完成（{time.time() - t0:.0f}s），首问无需冷加载", flush=True)
    except Exception as exc:
        print(f"[kra] 模型预热跳过：{type(exc).__name__}: {exc}", flush=True)




app = FastAPI(title="Knowledge Research Agent API", version="0.1.0", lifespan=lifespan)

_PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/api/v1/auth/login"}


@app.middleware("http")
async def _auth_middleware(request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path in _PUBLIC_PATHS or not path.startswith("/api/v1"):
        return await call_next(request)
    principal = resolve_request(request)
    if principal is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "未认证：请在 Authorization 中提供 Bearer token 或 X-API-Key"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(research.router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(documents.documents_router, prefix="/api/v1")
app.include_router(documents.kb_router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "llm_mode": get_llm().mode,
        "llm_model": settings.chat_model,
    }
