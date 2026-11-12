r"""SendAPI 动态路由 vs 固定扇出：调度源数量对比。

默认 Mock LLM + 外部源故障注入 empty，避免网络/模型限流影响“路由本身”的对比。
指标：
- scheduled_sources：每轮实际分发的源数量（固定扇出=4；动态=plan 大小）；
- reduction：动态相对固定的调度源减少比例；
- invalid_rounds / rewrite_count：Self-RAG 反思轮次（仅记录，不强行凑 40%）。

用法：
    .\.venv\Scripts\python.exe eval\scripts\eval_dynamic_routing.py --limit 8
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT / "apps" / "api")
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.agents.graph import get_research_graph
from app.core.config import get_settings

QUERIES = [
    "请分析 RAG 论文和网页最新进展",
    "对比 GraphRAG 与 RAG 的论文研究",
    "国内研究 知网 RAG 综述",
    "RAG 论文 知网 中文文献 最新进展",
    "GraphRAG 行业新闻 与 论文",
    "检索 RAG 学术论文",
    "搜索 RAG 最新网页",
    "请比较 RAG 与 Fine-tuning 的论文",
]


async def run_one(graph, query: str, tag: str, idx: int) -> dict:
    tid = f"diag-route-{tag}-{idx}"
    try:
        out = await graph.ainvoke({
            "task_id": tid,
            "query": query,
            "original_query": query,
            "lang": "zh",
            "metrics": {"t0": time.time()},
            "errors": [],
        })
        m = out.get("metrics") or {}
        return {
            "id": idx,
            "query": query,
            "mode": tag,
            "report_chars": len(out.get("report_md") or ""),
            "scheduled_sources": int(m.get("scheduled_sources_total", 0)),
            "scheduled_external_sources": int(m.get("scheduled_external_total", 0)),
            "last_round_sources": int(m.get("last_round_sources", 0)),
            "retrieval_rounds": int(m.get("retrieval_rounds", 0)),
            "invalid_rounds": int(m.get("invalid_rounds", 0)),
            "rewrite_count": int(m.get("rewrite_count", 0)),
            "dispatch_mode": m.get("dispatch_mode", ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": idx, "query": query, "mode": tag, "report_chars": 0,
            "scheduled_sources": 0, "scheduled_external_sources": 0, "last_round_sources": 0,
            "retrieval_rounds": 0, "invalid_rounds": 0, "rewrite_count": 0,
            "dispatch_mode": f"exception:{type(exc).__name__}",
        }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=len(QUERIES))
    args = parser.parse_args()
    queries = QUERIES[: max(1, args.limit)]

    s = get_settings()
    s.llm_api_key = ""
    s.deepseek_api_key = ""
    s.fault_inject_source = "arxiv,web,cnki"
    s.fault_inject_mode = "empty"
    s.fault_inject_rate = 1.0

    rows = []
    for dynamic in (True, False):
        s.dynamic_routing = dynamic
        graph = get_research_graph(dynamic)
        tag = "send" if dynamic else "fixed"
        for i, q in enumerate(queries, 1):
            row = await run_one(graph, q, tag, i)
            rows.append(row)
            print(row, flush=True)

    s.fault_inject_source = ""
    dyn = [r for r in rows if r["mode"] == "send"]
    fixed = [r for r in rows if r["mode"] == "fixed"]
    # 只比较“外部源”调度：KB 每轮必跑，不算无效分支
    dyn_avg = sum(r["scheduled_external_sources"] for r in dyn) / len(dyn)
    fixed_avg = sum(r["scheduled_external_sources"] for r in fixed) / len(fixed)
    reduction = 1 - dyn_avg / fixed_avg if fixed_avg else 0

    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "ts": ts, "llm": "mock", "external_fault": "empty",
            "queries": len(queries), "dynamic_avg": round(dyn_avg, 2),
            "fixed_avg": round(fixed_avg, 2), "reduction": round(reduction, 4),
        },
        "items": rows,
    }
    (out_dir / f"dynamic_routing_{ts}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# SendAPI 动态路由 vs 固定扇出",
        "",
        f"> 时间 {ts} | {len(queries)} 条 query | Mock LLM | 外部源注入 empty",
        "",
        "| 模式 | 平均外部源调度数 | 平均检索轮次 | 平均无效轮次 | 平均改写次数 |",
        "|---|---|---|---|---|",
    ]
    for tag, rows_ in (("send", dyn), ("fixed", fixed)):
        lines.append(
            f"| {tag} | {sum(r['scheduled_external_sources'] for r in rows_) / len(rows_):.2f} | "
            f"{sum(r['retrieval_rounds'] for r in rows_) / len(rows_):.2f} | "
            f"{sum(r['invalid_rounds'] for r in rows_) / len(rows_):.2f} | "
            f"{sum(r['rewrite_count'] for r in rows_) / len(rows_):.2f} |"
        )
    lines += [
        "",
        f"**动态路由减少外部源调度：{reduction:.1%}**（{dyn_avg:.2f} / {fixed_avg:.2f}）",
        "",
        "> 口径：KB 每轮必跑，不计入“无效分支”；比较的是论文/网页/知网外部源的分发数。"
        "Self-RAG 的 invalid_rounds 仍单独记录，不混为同一个数字。",
    ]
    (out_dir / f"dynamic_routing_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[routing] dynamic={dyn_avg:.2f} fixed={fixed_avg:.2f} reduction={reduction:.1%}")
    print(f"[routing] saved {out_dir}/dynamic_routing_{ts}.json/.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
