"""队列原子领取与重启恢复。"""
from datetime import timedelta

import pytest

from app.db.session import SessionLocal
from app.models.research import ResearchTask, utcnow
from app.services import research_service


async def _clear_queue():
    from sqlalchemy import update

    async with SessionLocal() as db:
        await db.execute(
            update(ResearchTask)
            .where(ResearchTask.status == "queued")
            .values(status="cancelled")
        )
        await db.commit()


@pytest.mark.asyncio
async def test_claim_is_atomic(client):
    await _clear_queue()
    task_id = await research_service.create_task("claim test", "en", "tenant-a", "user-a")
    row = await research_service._claim()
    assert row is not None and row["id"] == task_id
    assert await research_service._claim() is None

    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        assert task.status == "running"
        assert task.attempt == 1
        task.status = "cancelled"
        await db.commit()
    await research_service.delete_task(task_id, "tenant-a")


@pytest.mark.asyncio
async def test_reap_stale_running(client):
    await _clear_queue()
    task_id = await research_service.create_task("reap", "en", "tenant-a", "user-a")
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        row.status = "running"
        row.started_at = utcnow() - timedelta(hours=2)
        await db.commit()
    research_service._LAST_REAP = 0.0
    await research_service._maybe_reap()
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        assert row.status in ("queued", "failed")
        row.status = "cancelled"
        await db.commit()
    await research_service.delete_task(task_id, "tenant-a")


@pytest.mark.asyncio
async def test_recover_requeues_recent_and_fails_stale(client):
    await _clear_queue()
    recent = await research_service.create_task("recent", "en", "tenant-a", "user-a")
    stale = await research_service.create_task("stale", "en", "tenant-a", "user-a")
    async with SessionLocal() as db:
        r = await db.get(ResearchTask, recent)
        r.status = "running"
        r.started_at = utcnow()
        s = await db.get(ResearchTask, stale)
        s.status = "running"
        s.started_at = utcnow() - timedelta(hours=2)
        await db.commit()

    await research_service.recover_tasks()

    async with SessionLocal() as db:
        assert (await db.get(ResearchTask, recent)).status == "queued"
        assert (await db.get(ResearchTask, stale)).status == "failed"
    await research_service.delete_task(recent, "tenant-a")
    await research_service.delete_task(stale, "tenant-a")
