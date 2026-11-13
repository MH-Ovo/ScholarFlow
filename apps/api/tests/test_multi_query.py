"""多路检索：批编码只调用一次模型，并发检索结果按 RRF 正确合并。"""

import pytest

from app.agents import nodes


class _FakeStore:
    def __init__(self):
        self.dense_calls = 0
        self.sparse_calls = 0

    async def search_dense(self, vec, top_k=10, document_id=None, document_ids=None,
                           tenant_id=None):
        self.dense_calls += 1
        tag = int(vec[0])
        return [
            {
                "id": tag * 10 + i,
                "document_id": tag,
                "chunk_index": i,
                "document_name": f"doc{tag}",
                "text": f"text {tag}-{i}",
                "score": 0.9 - i * 0.01,
            }
            for i in range(3)
        ]

    async def search_sparse(self, vec, top_k=10, document_id=None, document_ids=None,
                            tenant_id=None):
        self.sparse_calls += 1
        return []

    async def retrieve_dense(self, ids):
        return {}


@pytest.mark.asyncio
async def test_multi_query_encodes_in_one_batch(monkeypatch):
    batches: list[list[str]] = []

    def fake_encode(texts):
        batches.append(list(texts))
        return ([[float(i + 1)] for i in range(len(texts))], [None] * len(texts))

    monkeypatch.setattr("app.rag.models.encode_hybrid", fake_encode)
    store = _FakeStore()
    merged = await nodes._multi_query_search(
        store,
        [("alpha", None), ("beta", None), ("gamma", None)],
        top_k=3,
        min_score=0.0,
    )

    assert batches == [["alpha", "beta", "gamma"]]
    assert store.dense_calls == 3
    assert len(merged) == 9
    assert all("rrf_multi" in h for h in merged)
