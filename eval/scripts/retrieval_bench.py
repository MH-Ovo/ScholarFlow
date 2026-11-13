"""检索层离线基准：不调用 RAGAS 裁判，只有 ID / parent 覆盖指标。

一次检索（embedding + 精排）在进程内只跑一遍，随后按「原始 RRF 名次」切出
不同候选池大小，对比 child/parent 覆盖率与 parent 去重收益。

用法：
    python eval/scripts/retrieval_bench.py --top-k 20 40 60 --out eval/results/bench.json
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_run import (  # noqa: E402
    EVAL_CONTEXT_K,
    _enhance_cached,
    chunk_text_map,
)
from app.agents.nodes import (  # noqa: E402
    _apply_rerank,
    _expand_with_siblings,
    _multi_query_search,
)
from app.core.config import get_settings  # noqa: E402
from app.llm.gateway import get_llm  # noqa: E402
from app.rag.vector_store import get_vector_store  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1]
DATASET = EVAL_DIR / "dataset_scholarflow.json"


def parent_key_of(hit: dict) -> tuple:
    parent = hit.get("parent_text") or ""
    if parent:
        return (hit.get("document_id"), hash(parent))
    return ("ext", hit.get("type"), hit.get("url") or hit.get("title") or "")


def select_parent_diverse(hits: list[dict], limit: int) -> list[dict]:
    chosen: list[dict] = []
    seen: set[tuple] = set()
    rest: list[dict] = []
    for h in hits:
        key = parent_key_of(h)
        if key in seen:
            rest.append(h)
            continue
        seen.add(key)
        chosen.append(h)
        if len(chosen) >= limit:
            return chosen
    for h in rest:
        if len(chosen) >= limit:
            break
        chosen.append(h)
    return chosen


async def retrieve_once(question: str, llm, pool: int) -> dict:
    """跑一次增强+多查询+兄弟扩展+精排，打上 fused_rank 供切池。"""
    settings = get_settings()
    sub_qs, hyde_text, en_query = [], "", ""
    if llm is not None and settings.query_processing:
        processed = await _enhance_cached(llm, question)
        sub_qs = [
            s.strip()
            for s in (processed.get("sub_queries") or [])
            if isinstance(s, str) and s.strip() and s.strip() != question
        ][:2]
        if settings.hyde_enabled and settings.multi_query:
            hyde_text = str(processed.get("hyde") or "").strip()
        en_query = str(processed.get("en_query") or "").strip()

    pairs = [(question, None)]
    weights = [1.0]
    for s in sub_qs:
        pairs.append((s, None))
        weights.append(1.0)
    if hyde_text and hyde_text != question:
        pairs.append((hyde_text, None))
        weights.append(1.0)
    if settings.en_query_enabled and en_query and en_query != question:
        pairs.append((en_query, None))
        weights.append(float(settings.en_query_weight))

    store = get_vector_store()
    t0 = time.time()
    fused = await _multi_query_search(store, pairs, top_k=pool, weights=weights)
    t_search = time.time() - t0
    for i, h in enumerate(fused):
        h["fused_rank"] = i

    t0 = time.time()
    expanded = await _expand_with_siblings(fused) if settings.sibling_expand else fused
    t_sib = time.time() - t0
    t0 = time.time()
    ranked = _apply_rerank(question, expanded) if settings.rerank_enabled else expanded
    t_rerank = time.time() - t0
    return {
        "ranked": ranked,
        "timing": {"search": t_search, "sibling": t_sib, "rerank": t_rerank},
    }


def metrics(ranked: list[dict], item: dict, parent_map: dict[str, str], limit: int, dedupe: bool):
    hits = select_parent_diverse(ranked, limit) if dedupe else ranked[:limit]
    ids = [(h["document_id"], h["chunk_index"]) for h in hits]

    rel_ids = set()
    for k in item["relevant_chunks"]:
        if "#" in k:
            d, i = k.split("#", 1)
            rel_ids.add((int(d), int(i)))

    def pkey(doc: int, idx: int) -> tuple:
        p = parent_map.get(f"{doc}#{idx}") or ""
        return (doc, hash(p)) if p else ("missing", doc, idx)

    rel_parents = {pkey(*x) for x in rel_ids}
    hit_parents = {pkey(*x) for x in ids}
    hit_ids = set(ids)
    return {
        "child_recall": len(hit_ids & rel_ids) / len(rel_ids) if rel_ids else 0.0,
        "child_precision": len(hit_ids & rel_ids) / len(hit_ids) if hit_ids else 0.0,
        "parent_recall": (
            len(hit_parents & rel_parents) / len(rel_parents) if rel_parents else 0.0
        ),
        "unique_parents": len({parent_key_of(h) for h in hits}),
        "unique_docs": len({h["document_id"] for h in hits}),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", nargs="+", type=int, default=[20])
    parser.add_argument("--pool", type=int, default=0, help="检索候选池（默认取 top-k 最大值）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    items = json.loads(DATASET.read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]
    _, parent_map = await chunk_text_map()
    llm = get_llm() if get_settings().query_processing else None
    pool = args.pool or max(args.top_k)

    per_item = []
    for it in items:
        got = await retrieve_once(it["question"], llm, pool)
        ranked = got["ranked"]
        row = {
            "id": it["id"],
            "timing": got["timing"],
            "candidates": [
                {
                    "doc": h["document_id"],
                    "chunk": h["chunk_index"],
                    "fused_rank": h.get("fused_rank"),
                    "sibling": bool(h.get("sibling")),
                    "rerank": float(h.get("rerank_score") or 0.0),
                    "parent": repr(parent_key_of(h)),
                    "name": h.get("document_name", ""),
                }
                for h in ranked
            ],
        }
        for k in args.top_k:
            subset = [
                h for h in ranked if h.get("sibling") or int(h.get("fused_rank", 10**6)) < k
            ]
            for dedupe in (False, True):
                row[f"k{k}_d{int(dedupe)}"] = metrics(
                    subset, it, parent_map, EVAL_CONTEXT_K, dedupe
                )
        per_item.append(row)
        print(
            f"[bench] {it['id']} pool={len(ranked)} "
            f"search={got['timing']['search']:.1f}s rerank={got['timing']['rerank']:.1f}s",
            flush=True,
        )

    print("\n=== summary ===")
    summary = {}
    for k in args.top_k:
        for dedupe in (False, True):
            key = f"k{k}_d{int(dedupe)}"
            agg = {
                m: round(statistics.mean([r[key][m] for r in per_item]), 4)
                for m in (
                    "child_recall",
                    "child_precision",
                    "parent_recall",
                    "unique_parents",
                    "unique_docs",
                )
            }
            summary[key] = agg
            print(
                f"k={k:<3} dedupe={str(dedupe):<5} child={agg['child_recall']:.4f} "
                f"parent={agg['parent_recall']:.4f} parents={agg['unique_parents']:.2f} "
                f"docs={agg['unique_docs']:.2f}"
            )
    timings = {
        stage: round(statistics.mean([r["timing"][stage] for r in per_item]), 2)
        for stage in ("search", "sibling", "rerank")
    }
    print("timing(s):", timings)

    if args.out:
        path = PROJECT_ROOT / args.out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"pool": pool, "summary": summary, "timing": timings, "items": per_item},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print("saved", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
