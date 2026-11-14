"""评测语料快照：把“这次评测跑在哪份知识库上”变成可校验的清单。

快照只记录影响检索结果的字段（文档 id / 内容 hash / 切片数 / 是否截断），不含正文，
因此文件很小，可以随评测结果一起归档。

用法（在 apps/api 目录下执行，与评测脚本相同）：
    ../../.venv/Scripts/python.exe ../../eval/scripts/corpus_manifest.py --write
    ../../.venv/Scripts/python.exe ../../eval/scripts/corpus_manifest.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_DIR = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.research import Document  # noqa: E402
from app.rag.vector_store import COLLECTION  # noqa: E402

DEFAULT_PATH = PROJECT_ROOT / "eval" / "corpus_manifest.json"


def _doc_row(doc: Document) -> dict:
    return {
        "id": doc.id,
        "name": doc.name,
        "size_bytes": doc.size_bytes,
        "content_hash": doc.content_hash,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "expected_chunks": doc.expected_chunks,
        "is_partial": bool(doc.is_partial),
    }


def digest_of(documents: list[dict], embedding_model: str) -> str:
    payload = {
        "embedding_model": embedding_model,
        "collection": COLLECTION,
        "documents": sorted(documents, key=lambda x: x["id"]),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def build_manifest() -> dict:
    settings = get_settings()
    async with SessionLocal() as db:
        docs = (await db.execute(select(Document).order_by(Document.id))).scalars().all()
    documents = [_doc_row(d) for d in docs]
    indexed = [d for d in documents if d["status"] == "indexed"]
    manifest = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "embedding_model": settings.embedding_model,
        "collection": COLLECTION,
        "summary": {
            "documents": len(documents),
            "indexed": len(indexed),
            "partial": sum(1 for d in documents if d["is_partial"]),
            "chunks": sum(d["chunk_count"] for d in documents),
        },
        "documents": documents,
    }
    manifest["digest"] = digest_of(documents, settings.embedding_model)
    return manifest


def diff_manifests(old: dict, new: dict) -> list[str]:
    """返回人类可读的差异；空列表表示一致。"""
    changes: list[str] = []
    if old.get("embedding_model") != new.get("embedding_model"):
        changes.append(
            f"embedding_model: {old.get('embedding_model')} -> {new.get('embedding_model')}"
        )
    old_docs = {d["id"]: d for d in old.get("documents", [])}
    new_docs = {d["id"]: d for d in new.get("documents", [])}
    for doc_id in sorted(new_docs.keys() - old_docs.keys()):
        changes.append(f"+ document {doc_id} {new_docs[doc_id]['name']}")
    for doc_id in sorted(old_docs.keys() - new_docs.keys()):
        changes.append(f"- document {doc_id} {old_docs[doc_id]['name']}")
    for doc_id in sorted(old_docs.keys() & new_docs.keys()):
        before, after = old_docs[doc_id], new_docs[doc_id]
        for key in ("content_hash", "chunk_count", "expected_chunks", "is_partial", "status"):
            if before.get(key) != after.get(key):
                changes.append(
                    f"~ document {doc_id} {after['name']}: {key} "
                    f"{before.get(key)} -> {after.get(key)}"
                )
    return changes


async def _run(args) -> int:
    path = Path(args.path)
    manifest = await build_manifest()
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[corpus] snapshot written to {path} "
            f"(docs={manifest['summary']['documents']} chunks={manifest['summary']['chunks']} "
            f"digest={manifest['digest'][:12]})"
        )
        return 0

    if not path.exists():
        print(f"[corpus] snapshot not found: {path}（先执行 --write）")
        return 2
    old = json.loads(path.read_text(encoding="utf-8"))
    changes = diff_manifests(old, manifest)
    if not changes:
        print(f"[corpus] matches snapshot {path} (digest={manifest['digest'][:12]})")
        return 0
    print(f"[corpus] 语料与快照不一致（{len(changes)} 处）：")
    for line in changes[:40]:
        print(f"  {line}")
    if len(changes) > 40:
        print(f"  ... 共 {len(changes)} 处")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="生成/覆盖快照")
    group.add_argument("--check", action="store_true", help="校验当前语料是否与快照一致")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
