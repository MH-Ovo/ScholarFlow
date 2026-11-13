"""分析 retrieval_bench 输出：候选池大小 × 预去重 × 最终 parent 去重 的质量曲线。

用法：
    python eval/scripts/bench_analyze.py eval/results/retrieval_bench_20261112.json
"""

import asyncio
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_run import chunk_text_map  # noqa: E402

DATASET = PROJECT_ROOT / "eval" / "dataset_scholarflow.json"
TOP = 12
CAPS = [12, 16, 20, 24, 32, 40, 48, 64, 96, 100000]
PRE_OPTIONS = [1, 2, 3, 100000]


def parent_key(doc: int, idx: int, parent_map: dict[str, str]) -> str:
    import hashlib

    text = (parent_map.get(f"{doc}#{idx}") or "").strip()[:4000]
    if not text:
        return f"missing:{doc}:{idx}"
    digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{doc}:{digest}"


def select(cands: list[dict], limit: int, dedupe: bool) -> list[dict]:
    if not dedupe:
        return cands[:limit]
    chosen, seen, rest = [], set(), []
    for c in cands:
        if c["pkey"] in seen:
            rest.append(c)
            continue
        seen.add(c["pkey"])
        chosen.append(c)
        if len(chosen) >= limit:
            return chosen
    chosen.extend(rest[: max(0, limit - len(chosen))])
    return chosen


def prefilter(subset: list[dict], keep_per_parent: int) -> list[dict]:
    """按 RRF 顺序，每个 parent 最多保留 N 个 child，再按精排分排序。"""
    if keep_per_parent >= 10**6:
        return subset
    kept, seen = [], {}
    for c in sorted(subset, key=lambda x: x["fused_rank"] if x["fused_rank"] is not None else 10**6):
        n = seen.get(c["pkey"], 0)
        if n < keep_per_parent:
            kept.append(c)
            seen[c["pkey"]] = n + 1
    return sorted(kept, key=lambda x: x["rerank"], reverse=True)


def evaluate(rows, cap: int, pre: int, dedupe: bool) -> dict:
    child, parent, parents, docs, counts = [], [], [], [], []
    for _, cands, rel, rel_parents in rows:
        subset = [
            c for c in cands if c["sibling"] or (c["fused_rank"] or 10**6) < cap
        ]
        subset = prefilter(subset, pre)
        counts.append(len(subset))
        top = select(subset, TOP, dedupe)
        ids = {(c["doc"], c["chunk"]) for c in top}
        hits = {c["pkey"] for c in top}
        child.append(len(ids & rel) / len(rel) if rel else 0.0)
        parent.append(len(hits & rel_parents) / len(rel_parents) if rel_parents else 0.0)
        parents.append(len({c["pkey"] for c in top}))
        docs.append(len({c["doc"] for c in top}))
    return {
        "child": statistics.mean(child),
        "parent": statistics.mean(parent),
        "parents": statistics.mean(parents),
        "docs": statistics.mean(docs),
        "n": statistics.mean(counts),
    }


async def main(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = json.loads(DATASET.read_text(encoding="utf-8"))
    by_id = {it["id"]: it for it in items}
    _, parent_map = await chunk_text_map()

    rows = []
    for case in data["items"]:
        it = by_id[case["id"]]
        rel = set()
        for k in it["relevant_chunks"]:
            if "#" in k:
                d, i = k.split("#", 1)
                rel.add((int(d), int(i)))
        rel_parents = {parent_key(d, i, parent_map) for d, i in rel}
        cands = sorted(case["candidates"], key=lambda c: c["rerank"], reverse=True)
        for c in cands:
            c["pkey"] = parent_key(c["doc"], c["chunk"], parent_map)
        rows.append((case["id"], cands, rel, rel_parents))

    print(
        f"{'cap':>6} {'pre':>4} {'dedupe':>7} {'childR':>7} {'parentR':>8} "
        f"{'parents':>8} {'docs':>6} {'n':>5}"
    )
    best = None
    for cap in CAPS:
        for pre in PRE_OPTIONS:
            for dedupe in (False, True):
                m = evaluate(rows, cap, pre, dedupe)
                print(
                    f"{cap:>6} {str(pre if pre < 10**6 else 'all'):>4} {str(dedupe):>7} "
                    f"{m['child']:>7.4f} {m['parent']:>8.4f} {m['parents']:>8.2f} "
                    f"{m['docs']:>6.2f} {m['n']:>5.0f}"
                )
                score = (m["parent"], m["child"])
                if best is None or score > best[0]:
                    best = (score, cap, pre, dedupe)
    print(f"\nbest by parent_recall: cap={best[1]} pre={best[2]} dedupe={best[3]} {best[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1
                         else "eval/results/retrieval_bench_20261112.json"))
    )
