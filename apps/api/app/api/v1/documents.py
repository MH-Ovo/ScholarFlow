"""知识库文档管理：上传 / 列表 / 详情(含 chunk 预览) / 删除 / 重试 / KB 统计。

所有查询按 principal.tenant_id 隔离；上传走流式落盘 + magic 校验 + 租户配额。
"""
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import delete, func, or_, select

from app.core.auth import Principal, require_principal
from app.core.config import get_settings
from app.core.timeutil import fmt_dt
from app.db.session import SessionLocal
from app.models.research import Document, DocumentChunk
from app.rag.vector_store import get_vector_store
from app.schemas.documents import (
    ChunkOut,
    DocumentDetailOut,
    DocumentOut,
    DocumentPageOut,
    KbStats,
    UploadResponse,
)
from app.services.ingestion_service import (
    UPLOAD_DIR,
    UploadRejected,
    find_duplicate,
    ingest_document,
    save_upload_file,
)

documents_router = APIRouter(prefix="/documents", tags=["documents"])
kb_router = APIRouter(prefix="/kb", tags=["kb"])

settings = get_settings()
MAX_FILES_PER_BATCH = 5


async def _tenant_usage(db, tenant_id: str) -> tuple[int, int]:
    row = (
        await db.execute(
            select(func.count(Document.id), func.coalesce(func.sum(Document.size_bytes), 0))
            .where(Document.tenant_id == tenant_id)
        )
    ).first()
    return int(row[0] or 0), int(row[1] or 0)


@documents_router.post("", response_model=UploadResponse)
async def upload_documents(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(require_principal),
):
    if len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(status_code=422, detail=f"单次最多上传 {MAX_FILES_PER_BATCH} 个文件")

    items: list[dict] = []
    max_bytes = int(settings.max_tenant_storage_mb) * 1024 * 1024
    async with SessionLocal() as db:
        count, used = await _tenant_usage(db, principal.tenant_id)
    if count + len(files) > int(settings.max_documents_per_tenant):
        raise HTTPException(
            status_code=422,
            detail=f"租户文档数已达上限 {settings.max_documents_per_tenant}",
        )

    for f in files:
        try:
            doc_type, relative, size, digest = await save_upload_file(f)
        except UploadRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if used + size > max_bytes:
            (UPLOAD_DIR / relative).unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=f"租户存储配额已满（{settings.max_tenant_storage_mb}MB）",
            )

        duplicate = await find_duplicate(principal.tenant_id, digest)
        if duplicate is not None:
            (UPLOAD_DIR / relative).unlink(missing_ok=True)
            items.append(
                {
                    "id": duplicate.id,
                    "name": duplicate.name,
                    "doc_type": duplicate.doc_type,
                    "status": duplicate.status,
                    "duplicate": True,
                }
            )
            continue

        async with SessionLocal() as db:
            doc = Document(
                name=f.filename or "unnamed",
                doc_type=doc_type,
                size_bytes=size,
                file_path=relative,
                status="pending",
                tenant_id=principal.tenant_id,
                content_hash=digest,
            )
            db.add(doc)
            await db.flush()
            doc_id = doc.id
            await db.commit()
        used += size
        items.append(
            {
                "id": doc_id,
                "name": doc.name,
                "doc_type": doc.doc_type,
                "status": "pending",
                "duplicate": False,
            }
        )

    for item in items:
        if not item.get("duplicate"):
            background_tasks.add_task(ingest_document, item["id"])
    return UploadResponse(items=items)


def _doc_out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        name=d.name,
        doc_type=d.doc_type,
        size_bytes=d.size_bytes,
        status=d.status,
        error_msg=d.error_msg,
        chunk_count=d.chunk_count,
        expected_chunks=d.expected_chunks,
        is_partial=d.is_partial,
        created_at=fmt_dt(d.created_at),
    )


@documents_router.get("", response_model=DocumentPageOut)
async def list_documents(
    q: str = Query("", max_length=100, description="按文件名搜索"),
    status: str = Query("", description="pending/parsing/embedding/indexed/failed"),
    doc_type: str = Query("", description="pdf/docx/md/txt"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_principal),
):
    async with SessionLocal() as db:
        stmt = select(Document).where(Document.tenant_id == principal.tenant_id)
        if q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Document.name.ilike(like), Document.error_msg.ilike(like)))
        if status.strip():
            stmt = stmt.where(Document.status == status.strip())
        if doc_type.strip():
            stmt = stmt.where(Document.doc_type == doc_type.strip())
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar() or 0
        rows = (
            await db.execute(
                stmt.order_by(Document.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    return DocumentPageOut(
        items=[_doc_out(d) for d in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@documents_router.get("/{doc_id}", response_model=DocumentDetailOut)
async def get_document(doc_id: int, principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None or doc.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="document not found")
        chunks = (
            await db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
                .order_by(DocumentChunk.chunk_index)
                .limit(50)
            )
        ).scalars().all()
    return DocumentDetailOut(
        id=doc.id,
        name=doc.name,
        doc_type=doc.doc_type,
        size_bytes=doc.size_bytes,
        status=doc.status,
        error_msg=doc.error_msg,
        chunk_count=doc.chunk_count,
        expected_chunks=doc.expected_chunks,
        is_partial=doc.is_partial,
        chunks=[ChunkOut(id=c.id, chunk_index=c.chunk_index, text=c.text) for c in chunks],
    )


@documents_router.post("/{doc_id}/retry")
async def retry_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_principal),
):
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None or doc.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="document not found")
        if doc.status != "failed":
            raise HTTPException(status_code=409, detail="only failed documents can retry")
        await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        doc.status = "pending"
        doc.error_msg = ""
        doc.chunk_count = 0
        doc.expected_chunks = 0
        doc.is_partial = False
        await db.commit()
    try:
        await get_vector_store().delete_by_document(doc_id)
    except Exception:
        pass
    background_tasks.add_task(ingest_document, doc_id)
    return {"ok": True}


@documents_router.post("/{doc_id}/reindex")
async def reindex_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_principal),
):
    """原地重建索引：任意非处理中状态可用，用于修复截断/旧解析器产物。"""
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None or doc.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="document not found")
        if doc.status in ("parsing", "embedding"):
            raise HTTPException(status_code=409, detail="document is busy")
        await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        doc.status = "pending"
        doc.error_msg = ""
        doc.chunk_count = 0
        doc.expected_chunks = 0
        doc.is_partial = False
        await db.commit()
    try:
        await get_vector_store().delete_by_document(doc_id)
    except Exception:
        pass
    background_tasks.add_task(ingest_document, doc_id)
    return {"ok": True}


@documents_router.delete("/{doc_id}")
async def delete_document(doc_id: int, principal: Principal = Depends(require_principal)):
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None or doc.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="document not found")
        file_path = doc.file_path
        await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        await db.execute(delete(Document).where(Document.id == doc_id))
        await db.commit()
    try:
        await get_vector_store().delete_by_document(doc_id)
    except Exception:
        pass
    if file_path:
        try:
            (UPLOAD_DIR / file_path).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@kb_router.get("/chunk")
async def kb_chunk(
    document_id: int,
    chunk_index: int,
    principal: Principal = Depends(require_principal),
):
    async with SessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None or doc.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="document not found")
        chunk = (
            await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.chunk_index == chunk_index,
                )
            )
        ).scalar_one_or_none()
        if chunk is None:
            raise HTTPException(status_code=404, detail="chunk not found")
        return {
            "document_id": document_id,
            "document_name": doc.name,
            "chunk_index": chunk.chunk_index,
            "text": chunk.text,
        }


@kb_router.get("/stats", response_model=KbStats)
async def kb_stats(principal: Principal = Depends(require_principal)):
    store = get_vector_store()
    try:
        await store.ensure_collection()
        ready = True
    except Exception:
        ready = False
    async with SessionLocal() as db:
        documents = (
            await db.execute(
                select(func.count(Document.id)).where(Document.tenant_id == principal.tenant_id)
            )
        ).scalar() or 0
        chunks = (
            await db.execute(
                select(func.count(DocumentChunk.id))
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(Document.tenant_id == principal.tenant_id)
            )
        ).scalar() or 0
        indexed = (
            await db.execute(
                select(func.count(Document.id)).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.status == "indexed",
                )
            )
        ).scalar() or 0
        processing = (
            await db.execute(
                select(func.count(Document.id)).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.status.in_(["pending", "parsing", "embedding"]),
                )
            )
        ).scalar() or 0
        failed = (
            await db.execute(
                select(func.count(Document.id)).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.status == "failed",
                )
            )
        ).scalar() or 0
    return KbStats(
        documents=documents,
        chunks=chunks,
        indexed=indexed,
        processing=processing,
        failed=failed,
        vector_store_ready=ready,
    )
