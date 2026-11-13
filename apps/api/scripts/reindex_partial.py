"""按文档原地重建索引，修复历史上被 ingest_max_chunks 截断的文档。

默认只处理可疑文档：chunk_count == 10 且未记录 expected_chunks（140 篇库里这批文档
是限额 10 时入库的，实际切片数应大于 10）。重建后 expected_chunks 会写入真实值，
is_partial 由入库逻辑重新判定。

用法：
    cd apps/api
    ../../.venv/Scripts/python.exe scripts/reindex_partial.py --dry-run
    ../../.venv/Scripts/python.exe scripts/reindex_partial.py
    ../../.venv/Scripts/python.exe scripts/reindex_partial.py --ids 12,34
    ../../.venv/Scripts/python.exe scripts/reindex_partial.py --all
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, or_, select

from app.db.session import SessionLocal
from app.models.research import Document, DocumentChunk
from app.rag.vector_store import get_vector_store
from app.services.ingestion_service import ingest_document


async def _candidates(args) -> list[Document]:
    async with SessionLocal() as db:
        stmt = select(Document).order_by(Document.id)
        if args.ids:
            wanted = [int(x) for x in args.ids.split(",") if x.strip()]
            stmt = stmt.where(Document.id.in_(wanted))
        elif not args.all:
            stmt = stmt.where(
                or_(
                    Document.is_partial.is_(True),
                    (Document.expected_chunks == 0) & (Document.chunk_count == 10),
                )
            )
        return list((await db.execute(stmt)).scalars().all())


async def _reset(doc_id: int) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        doc.status = "pending"
        doc.error_msg = ""
        doc.chunk_count = 0
        doc.expected_chunks = 0
        doc.is_partial = False
        await db.commit()
    try:
        await get_vector_store().delete_by_document(doc_id)
    except Exception as exc:
        print(f"[reindex]   qdrant delete skipped: {type(exc).__name__}: {exc}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="重建全部文档")
    parser.add_argument("--ids", default="", help="只重建这些 id（逗号分隔）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 条（断点续跑）")
    args = parser.parse_args()

    docs = await _candidates(args)
    if args.offset:
        docs = docs[args.offset :]
    if args.limit:
        docs = docs[: args.limit]
    print(f"[reindex] 待处理 {len(docs)} 篇", flush=True)
    if args.dry_run:
        for d in docs[:20]:
            print(f"  {d.id}\t{d.name}\tchunks={d.chunk_count}")
        if len(docs) > 20:
            print(f"  ... 其余 {len(docs) - 20} 篇")
        return 0

    ok = failed = 0
    for i, d in enumerate(docs, 1):
        print(f"[reindex] [{i}/{len(docs)}] {d.id} {d.name} (old chunks={d.chunk_count})", flush=True)
        try:
            await _reset(d.id)
            await ingest_document(d.id)
            async with SessionLocal() as db:
                row = await db.get(Document, d.id)
                if row and row.status == "indexed":
                    ok += 1
                    print(
                        f"[reindex]   ok chunks={row.chunk_count}/{row.expected_chunks} "
                        f"partial={row.is_partial}",
                        flush=True,
                    )
                else:
                    failed += 1
                    print(
                        f"[reindex]   FAILED status={row.status if row else 'missing'} "
                        f"err={(row.error_msg if row else '')}",
                        flush=True,
                    )
        except Exception as exc:
            failed += 1
            print(f"[reindex]   EXC {type(exc).__name__}: {exc}", flush=True)

    print(f"[reindex] done ok={ok} failed={failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
