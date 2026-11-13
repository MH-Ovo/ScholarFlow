"""事件总线：DB 持久化、增量读取、SSE 重放。"""
import pytest

from app.agents.events import EVENT_BUS


@pytest.mark.asyncio
async def test_emit_flush_and_read(client):
    task_id = "event-test-task"
    # task_events 有外键；先建一条任务避免 FK 失败
    from app.db.session import SessionLocal
    from app.models.research import ResearchTask

    async with SessionLocal() as db:
        db.add(ResearchTask(id=task_id, query="q", title="t", status="queued"))
        await db.commit()

    await EVENT_BUS.start()
    EVENT_BUS.emit(task_id, "step_started", {"step": "kb_search"})
    EVENT_BUS.emit(task_id, "report_token", {"delta": "hello"})
    await EVENT_BUS.flush()

    rows = await EVENT_BUS.read_after(task_id, 0)
    assert [r["event"] for r in rows] == ["step_started", "report_token"]
    assert rows[-1]["data"]["delta"] == "hello"
    assert await EVENT_BUS.has_event(task_id, "report_token") is True

    last = rows[-1]["id"]
    assert await EVENT_BUS.read_after(task_id, last) == []

    await EVENT_BUS.reset(task_id)
    assert await EVENT_BUS.history(task_id) == []

    from app.services import research_service

    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        row.status = "cancelled"
        await db.commit()
    await research_service.delete_task(task_id, "default")


def test_sse_replays_after_cancel(client, key_a):
    r = client.post(
        "/api/v1/research", json={"query": "sse replay", "lang": "en"}, headers=key_a
    )
    task_id = r.json()["task_id"]
    client.post(f"/api/v1/research/{task_id}/cancel", headers=key_a)

    with client.stream("GET", f"/api/v1/research/{task_id}/stream", headers=key_a) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: cancelled" in body
    assert "event: stream_end" in body
