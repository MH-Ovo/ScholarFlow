"""上传：流式落盘、magic bytes、超限拒绝、PDF 页数/切片基础行为。"""
import io
import zipfile

import pytest
from starlette.datastructures import UploadFile

from app.services import ingestion_service as ing


def _upload(data: bytes, name: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


@pytest.mark.asyncio
async def test_pdf_magic_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "UPLOAD_DIR", tmp_path)
    doc_type, rel, size, digest = await ing.save_upload_file(
        _upload(b"%PDF-1.4\nhello", "a.pdf")
    )
    assert doc_type == "pdf" and size == len(b"%PDF-1.4\nhello") and digest
    assert (tmp_path / rel).is_file()


@pytest.mark.asyncio
async def test_fake_pdf_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "UPLOAD_DIR", tmp_path)
    with pytest.raises(ing.UploadRejected):
        await ing.save_upload_file(_upload(b"not a pdf", "fake.pdf"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_oversize_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(ing, "MAX_FILE_BYTES", 8)
    with pytest.raises(ing.UploadRejected):
        await ing.save_upload_file(_upload(b"%PDF-1.4\n0123456789", "big.pdf"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_docx_zip_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(ing, "UPLOAD_DIR", tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<xml/>")
    data = buf.getvalue()
    doc_type, rel, _, _ = await ing.save_upload_file(_upload(data, "a.docx"))
    assert doc_type == "docx"
    assert (tmp_path / rel).is_file()


def test_chunk_page_nos():
    from app.rag.chunker import chunk_page_nos

    assert chunk_page_nos("[第2页] a [第3页] b [第2页]") == [2, 3]
