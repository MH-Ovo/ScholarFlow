"""本地知识库摄入管线：解析 → 切片 → bge-m3 向量化 → Qdrant upsert → 状态机。

状态流转: pending → parsing → embedding → indexed / failed(error_msg)
以 FastAPI BackgroundTasks 异步执行，单文档失败不影响其他。
"""
import asyncio
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.text import normalize_fullwidth
from app.db.session import SessionLocal
from app.models.research import Document, DocumentChunk
from app.rag.chunker import chunk_page_nos, chunk_text_hierarchical
from app.rag.models import get_embedder
from app.rag.parsers import parse_file
from app.rag.vector_store import get_vector_store

settings = get_settings()

UPLOAD_DIR = Path(__file__).resolve().parents[1] / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_BYTES = settings.max_upload_mb * 1024 * 1024  # 单文件上限（.env MAX_UPLOAD_MB）


def save_upload(name: str, content: bytes) -> tuple[str, str]:
    """返回 (doc_type, 相对路径)。"""
    ext = Path(name).suffix.lower()
    if ext not in {".pdf", ".docx", ".md", ".txt"}:
        raise ValueError(f"不支持的文档类型: {ext or '(无扩展名)'}")
    filename = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(content)
    return ext.lstrip("."), filename


async def ingest_document(doc_id: int) -> None:
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        doc.status = "parsing"
        await db.commit()
        name, doc_type, file_path = doc.name, doc.doc_type, doc.file_path

    try:
        abs_path = UPLOAD_DIR / file_path
        text = await asyncio.to_thread(parse_file, abs_path, doc_type)
        # 知网 PDF 含全角数字（如 ２０２５），入库前统一转半角，否则按 "2025" 检索不到
        text = normalize_fullwidth(text)
        chunk_units = chunk_text_hierarchical(text)  # [{parent, child}]
        if not chunk_units:
            raise RuntimeError("解析后未提取到有效文本")
        if settings.ingest_max_chunks > 0:
            chunk_units = chunk_units[: settings.ingest_max_chunks]
        child_texts = [u["child"] for u in chunk_units]
        parent_texts = [u["parent"] for u in chunk_units]

        embedder = get_embedder()
        if embedder is None:
            raise RuntimeError("向量模型不可用，请确认 models/ 目录已下载 bge-m3")

        async with SessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if doc is None:
                return
            doc.status = "embedding"
            doc.chunk_count = len(child_texts)
            await db.commit()

        # 混合编码：bge-m3 dense + sparse 词法权重（一次前向）；模型不可用降级纯 dense
        from app.rag.models import encode_hybrid

        if settings.ingest_dense_only:
            dense_vecs = await asyncio.to_thread(
                lambda: embedder.encode(
                    child_texts, batch_size=32, normalize_embeddings=True
                ).tolist()
            )
            sparse_vecs = None
        else:
            dense_vecs, sparse_vecs = await asyncio.to_thread(encode_hybrid, child_texts)
            if dense_vecs is None:
                dense_vecs = await asyncio.to_thread(
                    lambda: embedder.encode(child_texts, batch_size=32, normalize_embeddings=True).tolist()
                )
                sparse_vecs = None

        async with SessionLocal() as db:
            new_chunks: list[DocumentChunk] = [
                DocumentChunk(
                    document_id=doc_id, chunk_index=i,
                    text=child_texts[i], parent_text=parent_texts[i],
                )
                for i in range(len(child_texts))
            ]
            db.add_all(new_chunks)
            await db.commit()
            ids = [c.id for c in new_chunks]

        points = [
            {
                "id": chunk_id,
                "dense_vector": dense_vecs[i],
                "sparse_vector": (sparse_vecs[i] if sparse_vecs else None),
                "document_id": doc_id,
                "chunk_index": i,
                "document_name": name,
                "text": child_texts[i],
                "parent_text": parent_texts[i],
                "page_nos": chunk_page_nos(child_texts[i]),
            }
            for i, chunk_id in enumerate(ids)
        ]
        store = get_vector_store()
        await store.ensure_collection()
        await store.upsert_chunks(points)

        async with SessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if doc:
                doc.status = "indexed"
                await db.commit()
    except Exception as exc:  # noqa: BLE001  单文档失败不影响其他
        async with SessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if doc:
                doc.status = "failed"
                doc.error_msg = str(exc)[:300]
                await db.commit()
