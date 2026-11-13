"""P1 功能：知识库分页契约、逐句引用拆分、多轮历史块。"""
from app.agents.nodes import _extract_claims, _history_block


def test_extract_claims_keeps_refs_and_strips_markers():
    report = "RAG 分为三类[1][2]。这是没有引用的句子。第二句有依据[3]。"
    claims = _extract_claims(report)
    assert [c[0] for c in claims] == ["RAG 分为三类。", "第二句有依据。"]
    assert claims[0][1] == [1, 2]
    assert claims[1][1] == [3]


def test_history_block_omits_empty_and_limits():
    history = [
        {"role": "user", "content": "第一问"},
        {"role": "agent", "content": ""},
        {"role": "agent", "content": "第一答"},
    ]
    block = _history_block(history)
    assert "用户：第一问" in block
    assert "助手：第一答" in block
    assert block.count("助手：") == 1


def test_documents_page_contract(client, key_a):
    resp = client.get("/api/v1/documents?page=1&page_size=10&q=pdf", headers=key_a)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert isinstance(body["items"], list)


def test_followup_appends_user_message(client, key_a):
    import asyncio

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.research import Message, ResearchTask

    created = client.post("/api/v1/research", json={"query": "第一问"}, headers=key_a)
    task_id = created.json()["task_id"]

    async def _mark_done():
        async with SessionLocal() as db:
            task = await db.get(ResearchTask, task_id)
            task.status = "done"
            await db.commit()

    asyncio.run(_mark_done())
    resp = client.post(
        f"/api/v1/research/{task_id}/followup",
        json={"query": "第二问", "lang": "zh"},
        headers=key_a,
    )
    assert resp.status_code == 200

    async def _messages():
        async with SessionLocal() as db:
            return (
                await db.execute(select(Message).where(Message.task_id == task_id))
            ).scalars().all()

    messages = asyncio.run(_messages())
    assert [m.content for m in messages] == ["第一问", "第二问"]
