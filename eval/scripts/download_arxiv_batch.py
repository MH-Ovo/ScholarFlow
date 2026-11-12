r"""批量下载 arXiv PDF，用于 120+ 篇知识库与 GROBID 评测。

只下载开放获取的 arXiv PDF；默认保存到 data/papers/arxiv/。
arXiv API 在部分网络下 429，改抓 cs.IR recent 列表页的 /pdf/ 链接。
用法：
    .\.venv\Scripts\python.exe eval/scripts/download_arxiv_batch.py --limit 130
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "papers" / "arxiv"
LIST_BASE = "https://arxiv.org/list"
USER_AGENT = "Mozilla/5.0 (compatible; ScholarFlow-eval/1.0)"


async def fetch_entries(limit: int, category: str = "cs.IR") -> list[dict]:
    ids: list[str] = []
    list_url = f"{LIST_BASE}/{category}/recent"
    async with httpx.AsyncClient(timeout=60.0, headers={"User-Agent": USER_AGENT}) as c:
        for skip in range(0, limit + 25, 25):
            r = await c.get(list_url, params={"skip": skip, "show": 25})
            if r.status_code != 200:
                await asyncio.sleep(3)
                continue
            ids.extend(re.findall(r"/pdf/([0-9]{4}\.[0-9]{4,5})", r.text))
            ids = list(dict.fromkeys(ids))
            if len(ids) >= limit:
                break
            await asyncio.sleep(1.2)
    return [{"id": x, "title": ""} for x in ids[:limit]]


async def download_one(client: httpx.AsyncClient, item: dict, sem: asyncio.Semaphore) -> dict:
    dest = OUT / f"{item['id'].replace('/', '_')}.pdf"
    if dest.exists() and dest.stat().st_size > 20_000:
        return {**item, "path": str(dest), "status": "exists", "bytes": dest.stat().st_size}
    url = f"https://arxiv.org/pdf/{item['id']}.pdf"
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, follow_redirects=True, timeout=90.0)
                if r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 20_000:
                    dest.write_bytes(r.content)
                    return {**item, "path": str(dest), "status": "ok", "bytes": len(r.content)}
                last = f"http_{r.status_code}_bytes_{len(r.content)}"
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}:{str(exc)[:80]}"
            await asyncio.sleep(1.5 * (attempt + 1))
    return {**item, "path": "", "status": f"failed:{last}", "bytes": 0}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=130)
    parser.add_argument("--category", default="cs.IR")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    entries = await fetch_entries(args.limit, args.category)
    print(f"[arxiv] entries={len(entries)}")
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(
        timeout=90.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        rows = []
        for i in range(0, len(entries), 20):
            batch = entries[i : i + 20]
            got = await asyncio.gather(*(download_one(client, x, sem) for x in batch))
            rows.extend(got)
            print(
                f"[arxiv] {min(i + 20, len(entries))}/{len(entries)} "
                f"ok={sum(1 for x in rows if x['status'] in ('ok', 'exists'))}",
                flush=True,
            )

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M"),
        "query": 'all:"retrieval augmented generation"',
        "total": len(rows),
        "ok": sum(1 for x in rows if x["status"] in ("ok", "exists")),
        "items": rows,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[arxiv] downloaded={manifest['ok']}/{manifest['total']} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
