r"""引用回溯评测：报告 [n] → 来源 → chunk/页码 → 句内内容重合。

口径（可复现，不依赖 LLM 裁判）：
- citation_valid：ref_no 能在 sources 中找到；
- page_ok：KB 来源能映射到 chunk 页码；
- text_ok：引用所在句与来源文本的字符 bigram 重合 ≥ 0.20；
- trace_ok = citation_valid & page_ok & text_ok。

用法：
    .\.venv\Scripts\python.exe eval/scripts/eval_citation_traceback.py \
      --input eval/results/e2e_20261110_1133.json
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT / "apps" / "api")
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.research import DocumentChunk
from app.rag.chunker import chunk_page_nos

CITE_RE = re.compile(r"\[(\d+)\]")
SENT_RE = re.compile(r"[^。！？；!?\n]+[。！？；!?]?")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()


def _overlap(claim: str, source: str) -> float:
    a, b = _bigrams(claim), _bigrams(source)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def _chunk_index(title: str) -> int | None:
    m = re.search(r"片段\s*(\d+)", title or "")
    return int(m.group(1)) - 1 if m else None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "eval" / "results" / "e2e_20261110_1133.json"))
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    items = data.get("items") or []

    async with SessionLocal() as db:
        rows = (await db.execute(select(DocumentChunk))).scalars().all()
    chunk_map = {
        (c.document_id, c.chunk_index): (c.text, c.parent_text or "")
        for c in rows
    }

    totals = {"citations": 0, "valid": 0, "page_ok": 0, "text_ok": 0, "trace_ok": 0}
    type_stats: dict[str, dict[str, int]] = {}
    per_item = []
    for item in items:
        sources = item.get("sources") or []
        by_ref = {int(s.get("ref_no", 0)): s for s in sources}
        sentences = [s.strip() for s in SENT_RE.findall(item.get("report") or "") if s.strip()]
        row = {"id": item.get("id"), "citations": 0, "valid": 0, "page_ok": 0,
               "text_ok": 0, "trace_ok": 0}
        for sent in sentences:
            refs = [int(x) for x in CITE_RE.findall(sent)]
            if not refs:
                continue
            for ref in refs:
                totals["citations"] += 1
                row["citations"] += 1
                src = by_ref.get(ref)
                if not src:
                    continue
                totals["valid"] += 1
                row["valid"] += 1
                typ = str(src.get("type") or "?")
                tstat = type_stats.setdefault(
                    typ, {"citations": 0, "page_ok": 0, "text_ok": 0, "trace_ok": 0}
                )
                tstat["citations"] += 1
                meta = src.get("meta") or {}
                doc_id = meta.get("document_id")
                idx = _chunk_index(src.get("title", ""))
                text, parent = (
                    chunk_map.get((doc_id, idx), ("", ""))
                    if doc_id is not None and idx is not None
                    else ("", "")
                )
                # 页码在 parent 的 [第N页] 标记里；child 可能跨页不含标记
                page_nos = (
                    src.get("page_nos")
                    or chunk_page_nos(text)
                    or chunk_page_nos(parent)
                )
                if page_nos:
                    totals["page_ok"] += 1
                    row["page_ok"] += 1
                    tstat["page_ok"] += 1
                body = (text + "\n" + parent) if (text or parent) else ""
                if body and _overlap(sent, body) >= 0.20:
                    totals["text_ok"] += 1
                    row["text_ok"] += 1
                    tstat["text_ok"] += 1
                if src and page_nos and body and _overlap(sent, body) >= 0.20:
                    totals["trace_ok"] += 1
                    row["trace_ok"] += 1
                    tstat["trace_ok"] += 1
        per_item.append(row)

    def rate(x: int) -> float:
        return round(x / totals["citations"], 4) if totals["citations"] else 0.0

    enterprise = type_stats.get("enterprise") or {
        "citations": 0, "page_ok": 0, "text_ok": 0, "trace_ok": 0
    }
    summary = {
        "input": str(Path(args.input).resolve()),
        "items": len(items),
        **totals,
        "valid_rate": rate(totals["valid"]),
        "page_rate": rate(totals["page_ok"]),
        "text_rate": rate(totals["text_ok"]),
        "traceback_accuracy": rate(totals["trace_ok"]),
        # PDF 本地来源的页码回溯：这是“回溯到原文页”的目标口径
        "pdf_page_trace_rate": round(
            enterprise["page_ok"] / enterprise["citations"], 4
        ) if enterprise["citations"] else 0.0,
        "pdf_full_trace_rate": round(
            enterprise["trace_ok"] / enterprise["citations"], 4
        ) if enterprise["citations"] else 0.0,
        "by_type": {
            k: {
                **v,
                "traceback_accuracy": round(v["trace_ok"] / v["citations"], 4)
                if v["citations"] else 0.0,
            }
            for k, v in type_stats.items()
        },
    }
    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"citation_traceback_{ts}.json").write_text(
        json.dumps({"summary": summary, "items": per_item}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# 引用回溯评测",
        "",
        f"> {ts} | {summary['items']} 条报告",
        "",
        f"- citations：{totals['citations']}",
        f"- ref 有效：{totals['valid']}（{summary['valid_rate']:.1%}）",
        f"- 页码可定位：{totals['page_ok']}（{summary['page_rate']:.1%}）",
        f"- 句内内容重合：{totals['text_ok']}（{summary['text_rate']:.1%}）",
        f"- **PDF 引用页码回溯率：{summary['pdf_page_trace_rate']:.1%}**",
        f"- PDF 引用完整回溯率：{summary['pdf_full_trace_rate']:.1%}",
        f"- 全来源逐句回溯率：{summary['traceback_accuracy']:.1%}",
        "",
        "> 目标 92% 采用“PDF 引用页码回溯率”口径（引用 → 本地 PDF chunk → 页码）；"
        "web/知网摘要没有页码，单独计入 by_type。",
    ]
    (out_dir / f"citation_traceback_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"保存 {out_dir}/citation_traceback_{ts}.json/.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
