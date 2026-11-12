"""知网全文获取 → 知识库入库闭环。

调研主流程只取题录 + 摘要（evidence_level=abstract）；用户对某篇知网来源
触发「获取全文」时，才走这条独立路径：

    download_paper → 归档 data/papers/cn → 复制进 uploads → 建 Document
    → 后台解析/切片/向量化 → 来源 evidence_level 升级为 fulltext

为什么不在调研主流程里自动下载：单篇实测 13–40 秒，自动调用会让每次调研都
超时，且显著增加触发知网限流的概率（见《知网适配层技术实现文档》第 6 节）。
"""
import hashlib
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.integrations.cnki_client import fetch_fulltext
from app.models.research import Document, DocumentChunk, Source
from app.rag.vector_store import get_vector_store
from app.services.ingestion_service import MAX_FILE_BYTES, UPLOAD_DIR, ingest_document


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_name(title: str, record_ref: str) -> str:
    name = (title or "").strip() or f"知网文献_{record_ref[:12]}"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:300]


def _result(
    state: str,
    *,
    document_id: int | None = None,
    document_name: str = "",
    size_bytes: int = 0,
    chunk_count: int = 0,
    reason: str | None = None,
) -> dict:
    return {
        "state": state,
        "document_id": document_id,
        "document_name": document_name,
        "size_bytes": size_bytes,
        "chunk_count": chunk_count,
        "reason": reason,
    }


async def _reset_failed_document(doc_id: int) -> None:
    """重试前清掉上一次失败留下的切片与向量，避免重复入库。"""
    try:
        await get_vector_store().delete_by_document(doc_id)
    except Exception:
        pass
    async with SessionLocal() as db:
        await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        await db.commit()


async def prepare_cnki_document(source_id: int, record_ref: str, title: str) -> dict:
    """下载 PDF 并登记 Document（不执行解析）。

    返回 state：
      pending     — 已登记，调用方负责调度 ingest_and_sync
      in_progress — 已有同文件文档正在解析/向量化
      indexed     — 已入库，无需重复处理
      failed      — 下载或登记失败，reason 说明原因
    """
    if not record_ref:
        return _result("failed", reason="缺少 record_ref")

    # 来源已登记过文档：先按现有状态返回，避免重复下载（下载一次 13–40 秒）
    async with SessionLocal() as db:
        src = await db.get(Source, source_id)
        existing_id = (src.meta_json or {}).get("document_id") if src is not None else None
        if existing_id is not None:
            try:
                doc = await db.get(Document, int(existing_id))
            except (TypeError, ValueError):
                doc = None
            if doc is not None:
                if doc.status == "indexed":
                    return _result(
                        "indexed",
                        document_id=doc.id,
                        document_name=doc.name,
                        size_bytes=doc.size_bytes,
                        chunk_count=doc.chunk_count,
                    )
                if doc.status in {"pending", "parsing", "embedding"}:
                    return _result(
                        "in_progress",
                        document_id=doc.id,
                        document_name=doc.name,
                        size_bytes=doc.size_bytes,
                        chunk_count=doc.chunk_count,
                    )
                # failed → 继续走下载/重试

    res = await fetch_fulltext(record_ref)
    if res.get("state") != "completed":
        return _result("failed", reason=str(res.get("reason") or "下载失败"))

    pdf = Path(str(res.get("path") or ""))
    if not pdf.is_file():
        return _result("failed", reason="下载文件不存在")
    if pdf.suffix.lower() != ".pdf":
        return _result("failed", reason=f"不支持的格式：{pdf.suffix or '(无扩展名)'}")

    size = pdf.stat().st_size
    if size > MAX_FILE_BYTES:
        return _result(
            "failed",
            reason=f"PDF 超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限",
        )

    sha = str(res.get("sha256") or "") or _sha256_file(pdf)
    filename = f"cnki_{sha[:12]}.pdf"
    dest = UPLOAD_DIR / filename
    if not dest.exists():
        shutil.copy2(pdf, dest)

    retry_failed = False
    async with SessionLocal() as db:
        existing = (
            await db.execute(select(Document).where(Document.file_path == filename))
        ).scalars().first()

        if existing is not None:
            if existing.status == "indexed":
                return _result(
                    "indexed",
                    document_id=existing.id,
                    document_name=existing.name,
                    size_bytes=existing.size_bytes,
                    chunk_count=existing.chunk_count,
                )
            if existing.status in {"pending", "parsing", "embedding"}:
                return _result(
                    "in_progress",
                    document_id=existing.id,
                    document_name=existing.name,
                    size_bytes=existing.size_bytes,
                    chunk_count=existing.chunk_count,
                )
            # failed → 清理残留后重试
            doc = existing
            doc_id = doc.id
            retry_failed = True
        else:
            doc = Document(
                name=_display_name(title, record_ref),
                doc_type="pdf",
                size_bytes=size,
                file_path=filename,
                status="pending",
            )
            db.add(doc)
            await db.flush()
            doc_id = doc.id

        doc_name = doc.name
        await db.commit()

    if retry_failed:
        await _reset_failed_document(doc_id)
        async with SessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if doc:
                doc.status = "pending"
                doc.error_msg = ""
                doc.chunk_count = 0
                await db.commit()

    async with SessionLocal() as db:
        src = await db.get(Source, source_id)
        if src is not None:
            meta = dict(src.meta_json or {})
            meta.update(
                {
                    "document_id": doc_id,
                    "document_name": doc_name,
                    "document_status": "pending",
                    "evidence_level": "abstract",  # 入库完成后由 ingest_and_sync 升级
                    "fulltext_available": "yes",
                }
            )
            src.meta_json = meta
            await db.commit()

    return _result(
        "pending",
        document_id=doc_id,
        document_name=doc_name,
        size_bytes=size,
    )


async def ingest_and_sync(doc_id: int, source_id: int) -> None:
    """后台执行解析入库，并把最终状态回写到来源 meta。"""
    await ingest_document(doc_id)

    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        src = await db.get(Source, source_id)
        if doc is None or src is None:
            return
        meta = dict(src.meta_json or {})
        meta["document_status"] = doc.status
        meta["chunk_count"] = doc.chunk_count
        meta["evidence_level"] = "fulltext" if doc.status == "indexed" else "abstract"
        if doc.status == "failed":
            meta["document_error"] = doc.error_msg
        else:
            meta.pop("document_error", None)
        src.meta_json = meta
        await db.commit()
