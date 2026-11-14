"""追问/重试必须先清理 citation：否则删除 source 会触发外键失败（线上 500）。"""
import asyncio

from sqlalchemy import func, select


def _seed_task_with_citation(task_id: str, status: str) -> None:
    from app.db.session import SessionLocal
    from app.models.research import Citation, Report, ResearchTask, Source

    async def _run():
        async with SessionLocal() as db:
            task = await db.get(ResearchTask, task_id)
            task.status = status
            source = Source(
                task_id=task_id, ref_no=1, type="web", title="来源一", url="https://example.com"
            )
            db.add(source)
            await db.commit()
            await db.refresh(source)
            report = Report(
                task_id=task_id,
                title="报告",
                markdown="正文[1]。",
                version=1,
                tenant_id="tenant-a",
            )
            db.add(report)
            await db.commit()
            await db.refresh(report)
            db.add(
                Citation(
                    report_id=report.id,
                    ref_no=1,
                    source_id=source.id,
                    claim_text="正文。",
                    supported=True,
                )
            )
            await db.commit()

    asyncio.run(_run())


def _citation_count(task_id: str) -> int:
    from app.db.session import SessionLocal
    from app.models.research import Citation, Report

    async def _run():
        async with SessionLocal() as db:
            return (
                await db.execute(
                    select(func.count(Citation.id))
                    .join(Report, Report.id == Citation.report_id)
                    .where(Report.task_id == task_id)
                )
            ).scalar() or 0

    return asyncio.run(_run())


def test_followup_clears_old_citations(client, key_a):
    created = client.post("/api/v1/research", json={"query": "第一问"}, headers=key_a)
    task_id = created.json()["task_id"]
    _seed_task_with_citation(task_id, "done")
    assert _citation_count(task_id) == 1

    resp = client.post(
        f"/api/v1/research/{task_id}/followup",
        json={"query": "第二问", "lang": "zh"},
        headers=key_a,
    )
    assert resp.status_code == 200
    assert _citation_count(task_id) == 0


def test_retry_clears_old_citations(client, key_a):
    created = client.post("/api/v1/research", json={"query": "第一问"}, headers=key_a)
    task_id = created.json()["task_id"]
    _seed_task_with_citation(task_id, "failed")

    resp = client.post(
        f"/api/v1/research/{task_id}/retry?force=true", headers=key_a
    )
    assert resp.status_code == 200
    assert _citation_count(task_id) == 0
