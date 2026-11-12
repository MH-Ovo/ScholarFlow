"""研究任务路由：创建 / 列表 / 详情 / SSE 流 / 追问 / 导出。"""
import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select

from app.agents.events import EVENT_BUS
from app.core.timeutil import fmt_dt
from app.db.session import SessionLocal
from app.models.research import Document, Message, Report, ResearchStep, ResearchTask, Source
from app.schemas.research import (
    CreateResearchRequest,
    CreateResearchResponse,
    FollowupRequest,
    SourceFulltextOut,
    TaskDetailOut,
    TaskSummaryOut,
)
from app.services import fulltext_service, research_service

router = APIRouter(prefix="/research", tags=["research"])


def _sse(payload: dict) -> str:
    return f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"


@router.post("", response_model=CreateResearchResponse)
async def create_research(body: CreateResearchRequest):
    query = body.query.strip()
    # pydantic min_length 校验在 strip 之前，纯空格串会绕过；strip 后复检
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="query 过短")
    task_id = await research_service.create_task(query, body.lang)
    research_service.launch(task_id, query, body.lang)
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        title = task.title if task else ""
    return CreateResearchResponse(task_id=task_id, title=title)


@router.get("", response_model=list[TaskSummaryOut])
async def list_research(limit: int = 20):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ResearchTask).order_by(ResearchTask.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        TaskSummaryOut(
            id=t.id, title=t.title, query=t.query, status=t.status,
            created_at=fmt_dt(t.created_at),
        )
        for t in rows
    ]


@router.get("/{task_id}", response_model=TaskDetailOut)
async def get_research(task_id: str):
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        steps = (
            await db.execute(
                select(ResearchStep)
                .where(ResearchStep.task_id == task_id)
                .order_by(ResearchStep.order_index)
            )
        ).scalars().all()
        sources = (
            await db.execute(
                select(Source).where(Source.task_id == task_id).order_by(Source.ref_no)
            )
        ).scalars().all()
        report = (
            await db.execute(
                select(Report).where(Report.task_id == task_id).order_by(Report.version.desc())
            )
        ).scalars().first()
        all_reports = (
            await db.execute(
                select(Report).where(Report.task_id == task_id).order_by(Report.version.asc())
            )
        ).scalars().all()
        messages = (
            await db.execute(
                select(Message).where(Message.task_id == task_id).order_by(Message.created_at)
            )
        ).scalars().all()

    return TaskDetailOut(
        id=task.id,
        title=task.title,
        query=task.query,
        status=task.status,
        steps=[
            {"step_key": s.step_key, "label": s.label, "order_index": s.order_index,
             "status": s.status, "meta": s.meta_json or {}}
            for s in steps
        ],
        sources=[
            {"ref_no": s.ref_no, "type": s.type, "title": s.title, "url": s.url,
             "snippet": s.snippet, "relevance": s.relevance, "source_label": s.source_label,
             "page_nos": (s.meta_json or {}).get("page_nos", []),
             "meta": s.meta_json or {}}
            for s in sources
        ],
        report=(
            {"id": report.id, "title": report.title, "summary": report.summary,
             "markdown": report.markdown, "version": report.version}
            if report else None
        ),
        reports=[
            {"id": r.id, "title": r.title, "summary": r.summary,
             "markdown": r.markdown, "version": r.version}
            for r in all_reports
        ],
        graph=task.graph_json or {"nodes": [], "edges": []},
        stats=task.stats_json or {},
        messages=[{"role": m.role, "content": m.content} for m in messages],
    )


@router.get("/{task_id}/stream")
async def stream_research(task_id: str):
    # 不存在的任务直接 404：否则订阅一个永远不会产出事件的队列，连接只能靠 ping 悬挂
    async with SessionLocal() as db:
        if await db.get(ResearchTask, task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")

    async def event_gen():
        queue = EVENT_BUS.subscribe(task_id)
        # 重放协议：历史全量重放（含 report_token），先发 report_reset 让前端清空
        # 报告缓冲后按序重建——重连/刷新后文本无破洞；重放结束发 replay_end。
        # （旧方案重放跳过 report_token + 前端快照锁，导致重连前 token 永久缺失、
        #   流式文本破洞只能靠刷新）
        history = list(EVENT_BUS.history(task_id))
        if any(p["event"] == "report_token" for p in history):
            yield _sse({"event": "report_reset", "data": {}})
        seen = {id(p) for p in history}  # 同一 payload 对象引用去重，防重放竞态
        for payload in history:
            yield _sse(payload)
            if payload["event"] == "stream_end":
                yield _sse({"event": "replay_end", "data": {}})
                return
        yield _sse({"event": "replay_end", "data": {}})
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if id(payload) in seen:
                continue
            yield _sse(payload)
            if payload["event"] == "stream_end":
                return

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{task_id}/followup")
async def followup_research(task_id: str, body: FollowupRequest):
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="query 过短")
    result = await research_service.followup(task_id, query, body.lang)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="task not found")
    if result == "running":
        # 任务仍在流水线中：拒绝并发重跑（否则步骤/来源被双写、SSE 事件交错）
        raise HTTPException(status_code=409, detail="task is still running")
    return {"ok": True}


@router.post("/{task_id}/sources/{ref_no}/fulltext", response_model=SourceFulltextOut)
async def fetch_source_fulltext(task_id: str, ref_no: int, background_tasks: BackgroundTasks):
    """对知网来源触发全文获取：下载 PDF → 登记文档 → 后台解析入库。

    调研主流程只取摘要；该接口是用户主动触发的独立能力。下载是同步的
    （实测 13–40 秒），解析/切片/向量化交给后台任务，返回后由前端轮询
    `GET /documents/{document_id}` 或本接口获取最终状态。
    """
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        src = (
            await db.execute(
                select(Source).where(Source.task_id == task_id, Source.ref_no == ref_no)
            )
        ).scalars().first()
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        if src.type != "cnki":
            raise HTTPException(status_code=422, detail="仅知网来源支持获取全文")

        meta = src.meta_json or {}
        record_ref = str(meta.get("record_ref") or "").strip()
        if not record_ref:
            raise HTTPException(
                status_code=422,
                detail="该来源没有 record_ref（可能是站内降级结果），无法下载全文",
            )

        # 已登记过：直接返回当前状态，避免重复下载
        document_id = meta.get("document_id")
        if document_id is not None:
            try:
                doc = await db.get(Document, int(document_id))
            except (TypeError, ValueError):
                doc = None
            if doc is not None:
                if doc.status == "indexed":
                    return SourceFulltextOut(
                        state="indexed", document_id=doc.id, document_name=doc.name,
                        size_bytes=doc.size_bytes, chunk_count=doc.chunk_count,
                    )
                if doc.status in {"pending", "parsing", "embedding"}:
                    return SourceFulltextOut(
                        state="in_progress", document_id=doc.id, document_name=doc.name,
                        size_bytes=doc.size_bytes, chunk_count=doc.chunk_count,
                    )
                # failed → 继续往下走，由 service 清理残留并重试

        source_id, title = src.id, src.title

    result = await fulltext_service.prepare_cnki_document(source_id, record_ref, title)
    if result["state"] == "pending" and result.get("document_id"):
        background_tasks.add_task(
            fulltext_service.ingest_and_sync, result["document_id"], source_id
        )
    return SourceFulltextOut(**result)


@router.get("/{task_id}/export", response_class=PlainTextResponse)
async def export_research(task_id: str):
    async with SessionLocal() as db:
        report = (
            await db.execute(
                select(Report).where(Report.task_id == task_id).order_by(Report.version.desc())
            )
        ).scalars().first()
    if report is None:
        raise HTTPException(status_code=404, detail="report not ready")
    headers = {"Content-Disposition": f'attachment; filename="report-{task_id[:8]}.md"'}
    return PlainTextResponse(content=report.markdown, headers=headers)
