"""段落/页码级检索命中率 vs chunk-ID 命中率。

- baseline：top-k 命中标注 chunk（Recall@k）
- paragraph：top-k 命中标注 chunk 所在页码（段落级/页码级）
用于验证“parent-child + chunk—页码映射”是否让定位更细。
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT / "apps" / "api")
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "eval" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eval_run import chunk_text_map, production_kb_candidates
from app.llm.gateway import get_llm
from app.rag.chunker import chunk_page_nos
from app.rag.vector_store import get_vector_store


async def main() -> int:
    items = json.loads((ROOT / "eval" / "dataset_scholarflow.json").read_text(encoding="utf-8"))
    items = [x for x in items if not x.get("unanswerable") and x.get("relevant_chunks")]
    child, parent = await chunk_text_map()
    store = get_vector_store()
    await store.ensure_collection()

    rows = []
    for it in items:
        # 关闭 LLM 查询增强，避免评测被模型限流影响；主查询仍走生产混合检索+精排
        hits = await production_kb_candidates(store, it["question"], None)
        keys = [f"{h['document_id']}#{h['chunk_index']}" for h in hits]
        gold = set(it.get("relevant_chunks") or [])
        gold_pages: set[int] = set()
        for k in gold:
            text = child.get(k) or ""
            par = parent.get(k) or ""
            gold_pages.update(chunk_page_nos(text))
            gold_pages.update(chunk_page_nos(par))
        for h in hits:
            if h.get("page_nos"):
                continue
            k = f"{h['document_id']}#{h['chunk_index']}"
            h["page_nos"] = sorted(
                set(chunk_page_nos(child.get(k) or ""))
                | set(chunk_page_nos(parent.get(k) or ""))
            )
        # 页码回溯模式：只把带 page_nos 的切片放进段落级候选；
        # 全量 chunk 命中仍作为 baseline，用于对比“可回溯段落”的定位收益。
        paged_hits = [h for h in hits if h.get("page_nos")]
        row = {"id": it["id"], "gold": sorted(gold), "gold_pages": sorted(gold_pages)}
        for k in (3, 4, 5, 8, 12):
            row[f"chunk_hit@{k}"] = any(x in gold for x in keys[:k])
            row[f"page_hit@{k}"] = any(
                set(h.get("page_nos") or []) & gold_pages for h in paged_hits[:k]
            )
        rows.append(row)
        print(row, flush=True)

    summary = {"items": len(rows)}
    for k in (3, 4, 5, 8, 12):
        c = sum(1 for r in rows if r[f"chunk_hit@{k}"]) / len(rows)
        p = sum(1 for r in rows if r[f"page_hit@{k}"]) / len(rows)
        summary[f"chunk_hit@{k}"] = round(c, 4)
        summary[f"page_hit@{k}"] = round(p, 4)
        summary[f"relative_gain@{k}"] = round((p - c) / c, 4) if c else 0.0

    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "eval" / "results"
    payload = {"meta": {"ts": ts, "dataset": "dataset_scholarflow.json"}, "summary": summary, "items": rows}
    (out_dir / f"paragraph_hit_{ts}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 段落/页码级检索命中率",
        "",
        f"> {ts} | {len(rows)} 条可评分题 | top-k 检索",
        "",
        "| K | chunk-ID 命中 | 页码命中 | 相对提升 |",
        "|---|---|---|---|",
    ]
    for k in (3, 4, 5, 8, 12):
        lines.append(
            f"| {k} | {summary[f'chunk_hit@{k}']:.1%} | "
            f"{summary[f'page_hit@{k}']:.1%} | {summary[f'relative_gain@{k}']:+.1%} |"
        )
    (out_dir / f"paragraph_hit_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"保存 {out_dir}/paragraph_hit_{ts}.json/.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
