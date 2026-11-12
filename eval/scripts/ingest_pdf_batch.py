r"""批量入库 PDF：GROBID 解析 → parent-child → Qdrant。

用法：
    .\.venv\Scripts\python.exe eval/scripts/ingest_pdf_batch.py --dir data/papers/arxiv --limit 118
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT / "apps" / "api")
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.research import Document
from app.services.ingestion_service import UPLOAD_DIR, ingest_document


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(ROOT / "data" / "papers" / "arxiv"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pattern", default="*.pdf")
    args = parser.parse_args()

    pdfs = sorted(Path(args.dir).glob(args.pattern))
    if args.limit > 0:
        pdfs = pdfs[: args.limit]

    async with SessionLocal() as db:
        existing = {
            (x.name, x.file_path)
            for x in (await db.execute(select(Document))).scalars().all()
        }
    existing_names = {n for n, _ in existing}
    rows = []
    for i, pdf in enumerate(pdfs, 1):
        if pdf.name in existing_names:
            rows.append({"file": pdf.name, "status": "skipped_exists"})
            continue
        filename = f"{uuid.uuid4().hex}.pdf"
        dest = UPLOAD_DIR / filename
        shutil.copy2(pdf, dest)
        async with SessionLocal() as db:
            doc = Document(
                name=pdf.name,
                doc_type="pdf",
                file_path=filename,
                status="pending",
            )
            db.add(doc)
            await db.commit()
            doc_id = doc.id
        started = time.time()
        try:
            await ingest_document(doc_id)
            async with SessionLocal() as db:
                doc = await db.get(Document, doc_id)
                rows.append({
                    "file": pdf.name,
                    "doc_id": doc_id,
                    "status": doc.status if doc else "missing",
                    "chunks": doc.chunk_count if doc else 0,
                    "seconds": round(time.time() - started, 1),
                })
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "file": pdf.name,
                "doc_id": doc_id,
                "status": f"error:{type(exc).__name__}:{str(exc)[:100]}",
                "seconds": round(time.time() - started, 1),
            })
        print(rows[-1], flush=True)

    ts = time.strftime("%Y%m%d_%H%M")
    out = ROOT / "eval" / "results" / f"ingest_pdf_batch_{ts}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in rows if r.get("status") == "indexed")
    print(f"[ingest] indexed={ok}/{len(rows)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
