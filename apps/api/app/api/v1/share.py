"""只读分享：公开 token 读取报告，不需要登录，不暴露任务内部状态。"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.core.timeutil import fmt_dt
from app.db.session import SessionLocal
from app.models.research import Citation, Report, ReportShare, ResearchTask, Source
from app.schemas.research import ClaimOut, SharedReportOut, SourceOut

router = APIRouter(prefix="/share", tags=["share"])


def _expired(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:  # SQLite 读回来是 naive，按 UTC 解释
        value = value.replace(tzinfo=timezone.utc)
    return value < datetime.now(timezone.utc)


@router.get("/{token}", response_model=SharedReportOut)
async def get_shared_report(token: str):
    async with SessionLocal() as db:
        share = (
            await db.execute(select(ReportShare).where(ReportShare.token == token))
        ).scalars().first()
        if share is None or share.revoked or _expired(share.expires_at):
            raise HTTPException(status_code=404, detail="分享链接不存在或已过期")
        report = await db.get(Report, share.report_id)
        task = await db.get(ResearchTask, share.task_id)
        if report is None or task is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        sources = (
            await db.execute(
                select(Source).where(Source.task_id == share.task_id).order_by(Source.ref_no)
            )
        ).scalars().all()
        claims = (
            await db.execute(
                select(Citation).where(Citation.report_id == report.id).order_by(Citation.id)
            )
        ).scalars().all()

    return SharedReportOut(
        token=share.token,
        title=report.title or task.title,
        query=task.query,
        summary=report.summary,
        markdown=report.markdown,
        version=report.version,
        degraded=report.degraded,
        created_at=fmt_dt(report.created_at),
        sources=[
            SourceOut(
                ref_no=s.ref_no,
                type=s.type,
                title=s.title,
                url=s.url,
                snippet=s.snippet,
                relevance=s.relevance,
                source_label=s.source_label,
                page_nos=(s.meta_json or {}).get("page_nos", []),
            )
            for s in sources
        ],
        claims=[
            ClaimOut(claim_text=c.claim_text, ref_no=c.ref_no, supported=c.supported)
            for c in claims
            if c.claim_text
        ],
    )
