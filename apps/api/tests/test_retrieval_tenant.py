"""检索层：租户过滤条件不能被漏掉。"""
from app.rag.vector_store import QdrantVectorStore


def test_filter_has_tenant_default():
    f = QdrantVectorStore._filter(None, None, None)
    assert f is not None
    assert f.must[0].key == "tenant_id"
    assert f.must[0].match.value == "default"


def test_filter_has_tenant_and_doc():
    f = QdrantVectorStore._filter(3, None, "tenant-a")
    assert len(f.must) == 2
    assert f.must[0].match.value == "tenant-a"
    assert f.must[1].key == "document_id"
