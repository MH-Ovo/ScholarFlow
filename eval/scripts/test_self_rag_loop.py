"""Self-RAG 回边冒烟：低分 → rewrite → 重查；retry=2 后 → done。

不启动后端；Mock LLM，只验证路由与计数。
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT / "apps" / "api")
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.agents import nodes
from app.core.config import get_settings


async def main() -> int:
    s = get_settings()
    s.llm_api_key = ""
    s.deepseek_api_key = ""

    sources = [
        {"ref_no": i, "source_label": "本地知识库", "title": f"low-{i}",
         "snippet": "不相关片段", "relevance": 0.2}
        for i in (1, 2, 3)
    ]
    state = {
        "task_id": "diag-self-rag",
        "query": "RAG 的低相关重查",
        "original_query": "RAG 的低相关重查",
        "merged_sources": sources,
        "metrics": {"t0": 0},
        "retry_count": 0,
    }
    g1 = await nodes.grade_documents_node(state)
    print("grade1", g1.get("route"), g1.get("metrics"))
    if g1.get("route") != "rewrite":
        print("FAILED: expected rewrite")
        return 1

    state.update(g1)
    rw = await nodes.rewrite_node(state)
    print("rewrite", rw.get("retry_count"), rw.get("metrics", {}).get("rewrite_count"))

    state.update(rw)
    state["merged_sources"] = sources
    g2 = await nodes.grade_documents_node(state)
    print("grade2", g2.get("route"), g2.get("metrics"))
    if g2.get("route") != "rewrite":
        print("FAILED: expected second rewrite at retry=1")
        return 1

    state.update(g2)
    rw2 = await nodes.rewrite_node(state)
    print("rewrite2", rw2.get("retry_count"), rw2.get("metrics", {}).get("rewrite_count"))
    state.update(rw2)
    state["merged_sources"] = sources
    g3 = await nodes.grade_documents_node(state)
    print("grade3", g3.get("route"), g3.get("metrics"))
    if g3.get("route") != "done":
        print("FAILED: expected done at retry=2")
        return 1

    print("SELF_RAG_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
