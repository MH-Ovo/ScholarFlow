"""Research Statistics 聚合（首页 + 全局统计，按租户隔离）。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.auth import Principal, require_principal
from app.db.session import SessionLocal
from app.models.research import ResearchTask, Source

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def summary(principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        total_research = (
            await db.execute(
                select(func.count(ResearchTask.id)).where(
                    ResearchTask.tenant_id == principal.tenant_id
                )
            )
        ).scalar() or 0
        knowledge_sources = (
            await db.execute(
                select(func.count(Source.id))
                .join(ResearchTask, ResearchTask.id == Source.task_id)
                .where(ResearchTask.tenant_id == principal.tenant_id)
            )
        ).scalar() or 0
        done_tasks = (
            await db.execute(
                select(ResearchTask).where(
                    ResearchTask.tenant_id == principal.tenant_id,
                    ResearchTask.status == "done",
                )
            )
        ).scalars().all()

    docs_processed = sum(int((t.stats_json or {}).get("docs_processed", 0)) for t in done_tasks)
    relevance_vals = [
        int((t.stats_json or {}).get("relevance_avg", 0))
        for t in done_tasks
        if (t.stats_json or {}).get("relevance_avg")
    ]
    accuracy_rate = round(sum(relevance_vals) / len(relevance_vals)) if relevance_vals else 0

    return {
        "total_research": total_research,
        "knowledge_sources": knowledge_sources,
        "documents_processed": docs_processed,
        "accuracy_rate": accuracy_rate,
    }
