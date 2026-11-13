"""Qdrant 向量存储：本地知识库 chunk 的写入/检索/删除（混合检索：dense + sparse）。

Collection: kb_chunks
  dense  : bge-m3 1024 维稠密向量（Cosine）
  sparse : bge-m3 词法权重（SparseVectorParams）
Payload: {document_id, chunk_index, document_name, text}

检索采用应用层 RRF 融合（可控阈值，融合分不参与展示）：
  双路各 Top-20 → 按 point id 合并 → 阈值过滤 → RRF(k=60) 排序
"""
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.core.config import get_settings

settings = get_settings()

COLLECTION = "kb_chunks"
VECTOR_SIZE = 1024
RRF_K = 60


class QdrantVectorStore:
    def __init__(self, url: str) -> None:
        self._client = AsyncQdrantClient(url=url, timeout=10.0)
        self._tenant_ready = False

    async def ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        names = {c.name for c in collections.collections}
        if COLLECTION in names:
            # 迁移：旧集合无 sparse 字段 → 删除重建（由 rebuild 脚本重嵌）
            info = await self._client.get_collection(COLLECTION)
            params = info.config.params
            has_sparse = bool(getattr(params, "sparse_vectors", None)) or bool(
                getattr(params, "sparse_vectors_config", None)
            )
            if not has_sparse:
                await self._client.delete_collection(COLLECTION)
                await self._create()
        else:
            await self._create()
        if not self._tenant_ready:
            await self._backfill_tenant()
            self._tenant_ready = True

    async def _create(self) -> None:
        await self._client.create_collection(
            collection_name=COLLECTION,
            vectors_config={"dense": VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )

    async def _backfill_tenant(self) -> None:
        """旧库没有 tenant_id：补成 default，避免升级后全部检索为空。"""
        from app.core.config import get_settings

        default = get_settings().default_tenant_id
        missing = Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="tenant_id"))])
        try:
            result = await self._client.count(
                collection_name=COLLECTION, count_filter=missing, exact=True
            )
            if not int(result.count):
                return
            offset = None
            while True:
                points, offset = await self._client.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=missing,
                    limit=256,
                    with_payload=False,
                    with_vectors=False,
                )
                ids = [p.id for p in points]
                if ids:
                    await self._client.set_payload(
                        collection_name=COLLECTION,
                        payload={"tenant_id": default},
                        points=ids,
                    )
                if offset is None or not points:
                    break
            print(f"[kra][qdrant] 已回填 tenant_id={default}（{result.count} 点）")
        except Exception as exc:
            print(f"[kra][qdrant] tenant 回填跳过：{type(exc).__name__}: {exc}")

    async def drop(self) -> None:
        try:
            await self._client.delete_collection(COLLECTION)
        except Exception:
            pass

    async def upsert_chunks(self, points: list[dict[str, Any]]) -> None:
        """points: [{id, dense_vector, sparse_vector{indices,values}, document_id, chunk_index, document_name, text, page_nos}]

        分块写入（每批 200 点）并放宽超时，避免大批量 upsert 超时。
        """
        if not points:
            return
        batch_size = 200
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            structs = []
            for p in batch:
                vector: dict[str, Any] = {"dense": p["dense_vector"]}
                sparse = p.get("sparse_vector")
                if sparse and sparse.get("indices"):
                    vector["sparse"] = SparseVector(
                        indices=sparse["indices"], values=sparse["values"]
                    )
                structs.append(
                    PointStruct(
                        id=p["id"],
                        vector=vector,
                        payload={
                            "document_id": p["document_id"],
                            "chunk_index": p["chunk_index"],
                            "document_name": p["document_name"],
                            "tenant_id": p.get("tenant_id") or "default",
                            "text": p["text"][:2000],
                            "parent_text": (p.get("parent_text") or "")[:4000],
                            "page_nos": p.get("page_nos") or [],
                        },
                    )
                )
            await self._client.upsert(collection_name=COLLECTION, points=structs)

    async def search_dense(
        self,
        vector: list[float],
        top_k: int = 10,
        document_id: int | None = None,
        document_ids: list[int] | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._search(
            vector, "dense", top_k, document_id, document_ids, tenant_id
        )

    async def search_sparse(
        self,
        sparse: dict[str, Any],
        top_k: int = 10,
        document_id: int | None = None,
        document_ids: list[int] | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query_filter = self._filter(document_id, document_ids, tenant_id)
        response = await self._client.query_points(
            collection_name=COLLECTION,
            query=SparseVector(indices=sparse["indices"], values=sparse["values"]),
            using="sparse",
            limit=top_k,
            query_filter=query_filter,
        )
        return [self._to_hit(h) for h in response.points]

    async def _search(
        self,
        vector: list[float],
        using: str,
        top_k: int,
        document_id: int | None,
        document_ids: list[int] | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._client.query_points(
            collection_name=COLLECTION,
            query=vector,
            using=using,
            limit=top_k,
            query_filter=self._filter(document_id, document_ids, tenant_id),
        )
        return [self._to_hit(h) for h in response.points]

    @staticmethod
    def _filter(
        document_id: int | None,
        document_ids: list[int] | None = None,
        tenant_id: str | None = None,
    ) -> Filter | None:
        """构造文档过滤条件。

        `document_ids`（多文档）用于语言感知检索：英文问题只查英文文档，
        避免中文文档在向量空间里把英文原文整体挤出候选集
        （实测：不限定时英文标注命中 0.500，限定后 1.000）。
        """
        must = []
        # 默认租户兜底：任何忘了传 tenant 的调用也不会跨租户检索
        must.append(
            FieldCondition(
                key="tenant_id",
                match=MatchValue(value=tenant_id or settings.default_tenant_id),
            )
        )
        if document_ids:
            must.append(
                FieldCondition(
                    key="document_id", match=MatchAny(any=list(document_ids))
                )
            )
        elif document_id is not None:
            must.append(
                FieldCondition(key="document_id", match=MatchValue(value=document_id))
            )
        return Filter(must=must) if must else None

    @staticmethod
    def _to_hit(h) -> dict[str, Any]:
        return {
            "id": h.id,
            "document_id": h.payload.get("document_id"),
            "chunk_index": h.payload.get("chunk_index", 0),
            "document_name": h.payload.get("document_name", ""),
            "text": h.payload.get("text", ""),
            "parent_text": h.payload.get("parent_text", ""),
            "page_nos": h.payload.get("page_nos") or [],
            "score": float(h.score),
        }

    async def delete_by_document(self, document_id: int) -> None:
        await self._client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
            ),
        )

    async def retrieve_dense(self, ids: list[int]) -> dict[int, list[float] | None]:
        """按 point id 取回 dense 向量（本地余弦补算用）。兼容 list / numpy 返回。"""
        records = await self._client.retrieve(
            collection_name=COLLECTION, ids=ids, with_vectors=True
        )
        result: dict[int, list[float] | None] = {}
        for r in records:
            vec = r.vector
            if isinstance(vec, dict):
                vec = vec.get("dense")
            if vec is not None:
                vec = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            result[r.id] = vec
        return result

    async def count(self) -> int:
        info = await self._client.count(collection_name=COLLECTION, exact=True)
        return int(info.count)

    async def ping(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False


_store: QdrantVectorStore | None = None


def get_vector_store() -> QdrantVectorStore:
    global _store
    if _store is None:
        _store = QdrantVectorStore(settings.qdrant_url)
    return _store
