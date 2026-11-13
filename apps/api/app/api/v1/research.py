"""研究任务路由：创建 / 列表 / 详情 / SSE 流 / 追问 / 取消 / 重试 / 删除 / 导出。"""
import asyncio
import json
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select

from app.agents.events import EVENT_BUS
from app.core.auth import Principal, require_principal
from app.core.config import get_settings
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
settings = get_settings()


def _sse(payload: dict) -> str:
    return f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"


async def _task_status(task_id: str) -> str | None:
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
    return row.status if row else None


@router.post("", response_model=CreateResearchResponse)
async def create_research(
    body: CreateResearchRequest, principal: Principal = Depends(require_principal)
):
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="query 过短")
    task_id = await research_service.create_task(
        query, body.lang, principal.tenant_id, principal.user_id
    )
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        title = task.title if task else ""
    return CreateResearchResponse(task_id=task_id, title=title)


@router.get("", response_model=list[TaskSummaryOut])
async def list_research(
    limit: int = 20, principal: Principal = Depends(require_principal)
):
    limit = max(1, min(limit, 100))
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ResearchTask)
                .where(ResearchTask.tenant_id == principal.tenant_id)
                .order_by(ResearchTask.created_at.desc())
                .limit(limit)
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
async def get_research(task_id: str, principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id:
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
             "markdown": report.markdown, "version": report.version,
             "degraded": report.degraded, "degraded_reason": report.degraded_reason}
            if report else None
        ),
        reports=[
            {"id": r.id, "title": r.title, "summary": r.summary,
             "markdown": r.markdown, "version": r.version,
             "degraded": r.degraded, "degraded_reason": r.degraded_reason}
            for r in all_reports
        ],
        graph=task.graph_json or {"nodes": [], "edges": []},
        stats=task.stats_json or {},
        messages=[{"role": m.role, "content": m.content} for m in messages],
    )


@router.get("/{task_id}/stream")
async def stream_research(task_id: str, principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="task not found")

    async def event_gen():
        last_id = 0
        # 重放协议：历史含 report_token 时先发 report_reset，让前端清空后按序重建
        if await EVENT_BUS.has_event(task_id, "report_token"):
            yield _sse({"event": "report_reset", "data": {}})
        last_ping = time.monotonic()
        while True:
            rows = await EVENT_BUS.read_after(task_id, last_id)
            if rows:
                for row in rows:
                    last_id = max(last_id, int(row["id"]))
                    yield _sse(row)
                    if row["event"] == "stream_end":
                        return
                last_ping = time.monotonic()
                continue

            status = await _task_status(task_id)
            if status is None:
                yield _sse({"event": "stream_end", "data": {}})
                return
            if status in ("done", "failed", "cancelled"):
                # 终态但没读到 stream_end：可能是事件尚未落库，给 writer 一个批次时间
                await asyncio.sleep(0.1)
                rows = await EVENT_BUS.read_after(task_id, last_id)
                if rows:
                    continue
                yield _sse({"event": "stream_end", "data": {}})
                return

            if time.monotonic() - last_ping > 15:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(max(0.05, settings.event_poll_ms / 1000))

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{task_id}/followup")
async def followup_research(
    task_id: str, body: FollowupRequest, principal: Principal = Depends(require_principal)
):
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="query 过短")
    result = await research_service.followup(
        task_id, query, body.lang, principal.tenant_id
    )
    if result == "not_found":
        raise HTTPException(status_code=404, detail="task not found")
    if result == "running":
        raise HTTPException(status_code=409, detail="task is still running")
    return {"ok": True}


@router.post("/{task_id}/cancel")
async def cancel_research(task_id: str, principal: Principal = Depends(require_principal)):
    result = await research_service.cancel(task_id, principal.tenant_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="task not found")
    if result == "not_cancellable":
        raise HTTPException(status_code=409, detail="task already finished")
    return {"result": result}


@router.post("/{task_id}/retry")
async def retry_research(
    task_id: str,
    force: bool = False,
    principal: Principal = Depends(require_principal),
):
    result = await research_service.retry(task_id, principal.tenant_id, force=force)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="task not found")
    if result == "not_retryable":
        raise HTTPException(status_code=409, detail="only failed/cancelled tasks can retry")
    if result == "max_attempts":
        raise HTTPException(status_code=409, detail="max attempts reached")
    return {"result": result}


@router.delete("/{task_id}")
async def delete_research(task_id: str, principal: Principal = Depends(require_principal)):
    result = await research_service.delete_task(task_id, principal.tenant_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="task not found")
    if result == "running":
        raise HTTPException(status_code=409, detail="cancel the task before deleting")
    return {"ok": True}


@router.post("/{task_id}/sources/{ref_no}/fulltext", response_model=SourceFulltextOut)
async def fetch_source_fulltext(
    task_id: str,
    ref_no: int,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_principal),
):
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id:
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

        document_id = meta.get("document_id")
        if document_id is not None:
            try:
                doc = await db.get(Document, int(document_id))
            except (TypeError, ValueError):
                doc = None
            if doc is not None and doc.tenant_id == principal.tenant_id:
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

        source_id, title = src.id, src.title
        tenant_id = task.tenant_id

    result = await fulltext_service.prepare_cnki_document(
        source_id, record_ref, title, tenant_id
    )
    if result["state"] == "pending" and result.get("document_id"):
        background_tasks.add_task(
            fulltext_service.ingest_and_sync, result["document_id"], source_id
        )
    return SourceFulltextOut(**result)


@router.get("/{task_id}/export", response_class=PlainTextResponse)
async def export_research(task_id: str, principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="task not found")
        report = (
            await db.execute(
                select(Report).where(Report.task_id == task_id).order_by(Report.version.desc())
            )
        ).scalars().first()
    if report is None:
        raise HTTPException(status_code=404, detail="report not ready")
    headers = {"Content-Disposition": f'attachment; filename="report-{task_id[:8]}.md"'}
    return PlainTextResponse(content=report.markdown, headers=headers)
