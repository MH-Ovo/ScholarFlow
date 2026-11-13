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

    durations = sorted(float(t.duration_sec) for t in done_tasks if (t.duration_sec or 0) > 0)
    avg_sec = round(sum(durations) / len(durations), 1) if durations else 0
    p95_sec = (
        round(durations[min(len(durations) - 1, int(0.95 * len(durations)))], 1)
        if durations else 0
    )
    tokens_in = sum(int((t.stats_json or {}).get("llm_tokens_in", 0)) for t in done_tasks)
    tokens_out = sum(int((t.stats_json or {}).get("llm_tokens_out", 0)) for t in done_tasks)
    llm_calls = sum(int((t.stats_json or {}).get("llm_calls", 0)) for t in done_tasks)
    cost_usd = round(
        sum(float((t.stats_json or {}).get("llm_cost_usd", 0) or 0) for t in done_tasks), 4
    )

    return {
        "total_research": total_research,
        "knowledge_sources": knowledge_sources,
        "documents_processed": docs_processed,
        "accuracy_rate": accuracy_rate,
        "latency": {"avg_sec": avg_sec, "p95_sec": p95_sec, "samples": len(durations)},
        "llm": {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "calls": llm_calls,
            "cost_usd": cost_usd,
        },
    }
