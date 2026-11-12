r"""任务级单源故障注入：验证一个源故障时完整 LangGraph 任务仍能完成。

与 `eval_source_resilience.py` 的区别：
- 前者测适配器是否返回结构化 SourceResult；
- 本脚本跑完整 StateGraph（KB + 目标源 + 报告），统计任务完成率。
- 默认强制 Mock LLM，避免模型限流影响“故障是否中断任务”的判断；
  报告只用于验证链路完成，不评价答案质量。

前置：Qdrant 已启动（kb_chunks 454 点）。
用法：
    .\.venv\Scripts\python.exe eval\scripts\eval_source_resilience_task.py --reps 2
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

QUERIES = {
    "arxiv": "请分析 RAG 论文的学术进展",
    "web": "RAG 最新进展",
    "cnki": "国内研究 知网 RAG 综述",
}
MODES = ["timeout", "rate_limit", "auth", "error", "empty"]


async def run_case(graph, source: str, mode: str, rep: int) -> dict:
    settings = get_settings()
    settings.fault_inject_source = source
    settings.fault_inject_mode = mode
    settings.fault_inject_rate = 1.0
    q = QUERIES[source]
    tid = f"diag-task-{source}-{mode}-{rep}"
    try:
        out = await graph.ainvoke({
            "task_id": tid,
            "query": q,
            "original_query": q,
            "lang": "zh",
            "metrics": {"t0": time.time()},
            "errors": [],
        })
        status_key = "arxiv_status" if source == "arxiv" else f"{source}_status"
        report = out.get("report_md") or ""
        return {
            "source": source,
            "mode": mode,
            "rep": rep,
            "ok": bool(report.strip()),
            "status": out.get("metrics", {}).get(status_key),
            "report_chars": len(report),
            "errors": len(out.get("errors") or []),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "source": source,
            "mode": mode,
            "rep": rep,
            "ok": False,
            "status": f"exception:{type(exc).__name__}",
            "report_chars": 0,
            "errors": 0,
        }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=2)
    args = parser.parse_args()

    settings = get_settings()
    # 强制 Mock，避免真实模型限流/费用影响故障隔离结论
    settings.llm_api_key = ""
    settings.deepseek_api_key = ""
    graph = get_research_graph()

    rows = []
    for source in ("arxiv", "web", "cnki"):
        for mode in MODES:
            for rep in range(max(1, args.reps)):
                row = await run_case(graph, source, mode, rep)
                rows.append(row)
                print(row, flush=True)

    settings.fault_inject_source = ""
    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {"ts": ts, "reps": args.reps, "llm": "mock", "graph": "full"},
        "items": rows,
    }
    (out_dir / f"source_resilience_task_{ts}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    by_source = {}
    for r in rows:
        d = by_source.setdefault(r["source"], [0, 0])
        d[0] += int(r["ok"])
        d[1] += 1
    lines = [
        "# 任务级单源故障注入与降级成功率",
        "",
        f"> 时间 {ts} | 完整 LangGraph | Mock LLM | 每 source×mode 重复 {args.reps} 次",
        "",
        f"**任务完成率：{ok}/{total} = {ok / total:.1%}**",
        "",
        "| 源 | 完成/总数 | 完成率 |",
        "|---|---|---|",
    ]
    for source, (a, b) in by_source.items():
        lines.append(f"| {source} | {a}/{b} | {a / b:.1%} |")
    lines += [
        "",
        "> 判定：注入一个源故障后，LangGraph 仍返回非空报告即算完成；不评价答案质量。",
    ]
    (out_dir / f"source_resilience_task_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[task-resilience] {ok}/{total} saved {out_dir}/source_resilience_task_{ts}.json/.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
