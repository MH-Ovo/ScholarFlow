"""本地知识库摄入管线：解析 → 切片 → bge-m3 向量化 → Qdrant upsert → 状态机。

状态流转: pending → parsing → embedding → indexed / failed(error_msg)
以 FastAPI BackgroundTasks 异步执行，单文档失败不影响其他。

安全要点：
- 上传按 1MB 分块落盘，超限立即中止并删除半文件（不再整文件读入内存）
- 只接受扩展名 + magic bytes 双校验（PDF / DOCX / MD / TXT）
- 解析有超时，PDF 有页数上限
"""
import asyncio
import hashlib
import uuid
from pathlib import Path

from fastapi import UploadFile
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

MAX_FILE_BYTES = settings.max_upload_mb * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

_ALLOWED_EXT = {".pdf": "pdf", ".docx": "docx", ".md": "md", ".txt": "txt"}


class UploadRejected(ValueError):
    pass


def _detect_type(name: str) -> tuple[str, str]:
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise UploadRejected(f"不支持的文档类型: {ext or '(无扩展名)'}")
    return _ALLOWED_EXT[ext], ext


def _validate_magic(doc_type: str, path: Path) -> bool:
    """扩展名可伪造，落盘后再按文件头校验。"""
    with path.open("rb") as fh:
        head = fh.read(8192)
    if not head:
        return False
    if doc_type == "pdf":
        return head.startswith(b"%PDF-")
    if doc_type == "docx":
        if not head.startswith(b"PK\x03\x04"):
            return False
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                if len(infos) > 5000:
                    return False
                total = sum(i.file_size for i in infos)
                if total > max(MAX_FILE_BYTES * 5, 200 * 1024 * 1024):
                    return False
                return any(i.filename.startswith("word/") for i in infos)
        except zipfile.BadZipFile:
            return False
    # md/txt：拒绝二进制（NUL）与无法按 UTF-8/GBK 解码的垃圾
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("gbk")
        except UnicodeDecodeError:
            return False
    return True


async def save_upload_file(upload: UploadFile) -> tuple[str, str, int, str]:
    """流式保存上传文件，返回 (doc_type, relative_path, size, sha256)。"""
    name = upload.filename or "unnamed"
    doc_type, ext = _detect_type(name)
    filename = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / filename
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as fh:
            while True:
                chunk = await upload.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise UploadRejected(
                        f"{name} 超过 {settings.max_upload_mb}MB 限制"
                    )
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise UploadRejected(f"{name} 是空文件")
        if not _validate_magic(doc_type, path):
            raise UploadRejected(f"{name} 文件头校验失败（扩展名与实际格式不符）")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return doc_type, filename, size, digest.hexdigest()


def save_upload(name: str, content: bytes) -> tuple[str, str]:
    """兼容旧接口：内存 bytes 保存（评测脚本/内部调用）。"""
    doc_type, ext = _detect_type(name)
    filename = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(content)
    if not _validate_magic(doc_type, path):
        path.unlink(missing_ok=True)
        raise UploadRejected(f"{name} 文件头校验失败")
    return doc_type, filename


async def ingest_document(doc_id: int) -> None:
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        doc.status = "parsing"
        doc.error_msg = ""
        await db.commit()
        name, doc_type, file_path, tenant_id = (
            doc.name, doc.doc_type, doc.file_path, doc.tenant_id or settings.default_tenant_id
        )

    try:
        abs_path = UPLOAD_DIR / file_path
        if not abs_path.is_file():
            raise RuntimeError("源文件不存在（可能已被清理）")
        text = await asyncio.wait_for(
            asyncio.to_thread(parse_file, abs_path, doc_type),
            timeout=float(settings.parse_timeout_sec),
        )
        text = normalize_fullwidth(text)
        chunk_units = chunk_text_hierarchical(text)
        if not chunk_units:
            raise RuntimeError("解析后未提取到有效文本")
        total_chunks = len(chunk_units)
        truncated = settings.ingest_max_chunks > 0 and total_chunks > settings.ingest_max_chunks
        if truncated:
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
            doc.expected_chunks = total_chunks
            doc.is_partial = truncated
            await db.commit()

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
                    lambda: embedder.encode(
                        child_texts, batch_size=32, normalize_embeddings=True
                    ).tolist()
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
                "tenant_id": tenant_id,
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
            if doc is not None:
                doc.status = "indexed"
                doc.chunk_count = len(child_texts)
                await db.commit()
    except asyncio.TimeoutError:
        await _mark_failed(doc_id, f"解析超时（>{int(settings.parse_timeout_sec)}s）")
    except Exception as exc:  # noqa: BLE001
        await _mark_failed(doc_id, f"{type(exc).__name__}: {exc}"[:300])


async def _mark_failed(doc_id: int, message: str) -> None:
    async with SessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is not None:
            doc.status = "failed"
            doc.error_msg = message
            await db.commit()


async def find_duplicate(tenant_id: str, content_hash: str) -> Document | None:
    if not content_hash:
        return None
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.content_hash == content_hash,
                    Document.status == "indexed",
                ).limit(1)
            )
        ).scalars().first()
    return row
