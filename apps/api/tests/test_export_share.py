"""P1 导出/分享：DOCX/PDF 生成与只读分享链接契约。"""
import asyncio

from app.services.export_service import markdown_to_docx, markdown_to_pdf

SAMPLE_MD = """# 测试报告

这是一段**加粗**文字与*斜体*，以及引用[1]。

## 一、背景

- 列表项一
- 列表项二

> 引用内容

| 指标 | 数值 |
|---|---|
| 召回率 | 0.87 |

```
print('hello')
```
"""


def test_docx_and_pdf_are_generated():
    docx = markdown_to_docx(SAMPLE_MD, "测试报告")
    pdf = markdown_to_pdf(SAMPLE_MD, "测试报告")
    assert docx[:2] == b"PK"
    assert pdf[:4] == b"%PDF"
    assert len(docx) > 5000
    assert len(pdf) > 1000


def test_export_and_share_roundtrip(client, key_a):
    from app.db.session import SessionLocal
    from app.models.research import Report, ResearchTask, Source

    created = client.post("/api/v1/research", json={"query": "测试分享"}, headers=key_a)
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    async def _seed():
        async with SessionLocal() as db:
            task = await db.get(ResearchTask, task_id)
            task.status = "done"
            report = Report(
                task_id=task_id,
                title="测试报告",
                summary="摘要",
                markdown=SAMPLE_MD,
                version=1,
                tenant_id="tenant-a",
            )
            db.add(report)
            await db.commit()
            await db.refresh(report)
            db.add(
                Source(
                    task_id=task_id,
                    ref_no=1,
                    type="web",
                    title="来源一",
                    url="https://example.com/a",
                    snippet="片段",
                    relevance=0.9,
                    source_label="网页",
                )
            )
            await db.commit()
            return report.id

    asyncio.run(_seed())

    md = client.get(f"/api/v1/research/{task_id}/export?format=md", headers=key_a)
    assert md.status_code == 200
    assert "测试报告" in md.text
    assert "参考来源" in md.text
    assert "来源一" in md.text
    assert "attachment" in md.headers["content-disposition"]

    docx = client.get(f"/api/v1/research/{task_id}/export?format=docx", headers=key_a)
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"

    pdf = client.get(f"/api/v1/research/{task_id}/export?format=pdf", headers=key_a)
    assert pdf.status_code == 200
    assert pdf.content[:4] == b"%PDF"

    bad = client.get(f"/api/v1/research/{task_id}/export?format=exe", headers=key_a)
    assert bad.status_code == 422

    share = client.post(f"/api/v1/research/{task_id}/share", headers=key_a)
    assert share.status_code == 200
    token = share.json()["token"]
    assert share.json()["path"] == f"/share/{token}"

    # 公开读取：不带任何认证头
    public = client.get(f"/api/v1/share/{token}")
    assert public.status_code == 200
    body = public.json()
    assert body["markdown"] == SAMPLE_MD
    assert body["title"] == "测试报告"
    assert body["sources"][0]["ref_no"] == 1
    assert "tenant_id" not in body

    # 过期链接不可读
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models.research import ReportShare

    async def _expire():
        async with SessionLocal() as db:
            row = (
                await db.execute(select(ReportShare).where(ReportShare.token == token))
            ).scalars().first()
            row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            await db.commit()

    asyncio.run(_expire())
    assert client.get(f"/api/v1/share/{token}").status_code == 404

    # 删除任务应同时清理分享记录（PG 下外键不允许残留）
    assert client.delete(f"/api/v1/research/{task_id}", headers=key_a).status_code == 200
    assert client.get(f"/api/v1/share/{token}").status_code == 404

    assert client.get("/api/v1/share/not-a-token").status_code == 404
