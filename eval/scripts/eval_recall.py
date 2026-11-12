"""检索层召回评测：Recall@K / HitRate / DocHitRate。

只测「知识库检索」这一层，不调用报告生成，速度快（30 条约 5 分钟）。
生成层与端到端评测见 eval_run.py 与《人工评测手册》。

用法：
    python eval/scripts/eval_recall.py                          # 默认英文 30 条
    python eval/scripts/eval_recall.py --dataset eval/dataset_scholarflow_cn.json
    python eval/scripts/eval_recall.py --topk 12 --detail       # 打印每条命中
    python eval/scripts/eval_recall.py --judge                  # 对未命中题调 LLM 判定能否回答

指标口径：
    Recall@K   每个标注切片是否进了 top-K，按条算比例后取宏平均
    HitRate    至少命中 1 个标注切片的题目占比（反映"完全失效"比例）
    DocHitRate 至少命中标注所属文档任一相关切片的题目占比（宽松口径）
"""
import argparse
import asyncio
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.chdir(ROOT / "apps" / "api")

import eval_run as er  # noqa: E402
from app.llm.gateway import get_llm  # noqa: E402
from app.rag.vector_store import get_vector_store  # noqa: E402

JUDGE_PROMPT = """判断下面这些资料片段能否回答用户问题。

用户问题：{q}

资料片段：
{blocks}

要求：
- 只有当片段中**明确包含**回答问题所需的具体信息（方法名、机制、数字、结论）时，才算"可以回答"
- 仅仅主题相关、泛泛提及，不算
- 只依据片段内容判断，不要用你自己的知识补充

输出 JSON（不要输出其他内容）：
{{"answerable": true/false, "usable_refs": [可回答该问题的片段编号], "reason": "一句话说明"}}"""


def parse_rel(item: dict) -> set[tuple[int, int]]:
    out = set()
    for key in item.get("relevant_chunks") or []:
        d, _, i = str(key).partition("#")
        if d.isdigit() and i.isdigit():
            out.add((int(d), int(i)))
    return out


async def judge(question: str, hits: list[dict], llm) -> dict:
    blocks = []
    for n, h in enumerate(hits, 1):
        text = (h.get("text") or "")[:420].replace("\n", " ")
        blocks.append(f"[{n}] ({h.get('document_name', '?')[:16]}) {text}")
    payload = await llm.extract_json(
        JUDGE_PROMPT.format(q=question, blocks="\n\n".join(blocks)),
        {"answerable": False, "usable_refs": [], "reason": "judge_failed"},
    )
    return payload if isinstance(payload, dict) else {}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "eval" / "dataset_scholarflow.json"))
    ap.add_argument("--topk", type=int, default=12)
    ap.add_argument("--detail", action="store_true", help="打印每条的命中情况")
    ap.add_argument("--judge", action="store_true", help="对未命中题调 LLM 判定召回是否可回答")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    args = ap.parse_args()

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    # 跳过标注为"知识库未覆盖"的题：这些题的知识库里确实没有答案，
    # 计入召回率只会污染指标（属标注侧问题，不是检索能力）。
    total_all = len(data)
    data = [it for it in data if not it.get("unanswerable")]
    skipped = total_all - len(data)

    store, llm = get_vector_store(), get_llm()
    K = args.topk

    recall_sum = 0.0
    hit = doc_hit = 0
    rows: list[str] = []
    misses: list[tuple[dict, list[dict]]] = []

    print(f"数据集: {Path(args.dataset).name}  ({len(data)} 条)   K={K}", flush=True)
    if skipped:
        print(f"已跳过 {skipped} 条标记为「知识库未覆盖」的题（不计入召回率）", flush=True)
    print(flush=True)

    for n, item in enumerate(data, 1):
        rel = parse_rel(item)
        fused = await er.production_kb_candidates(store, item["question"], llm)
        top = fused[:K]
        got = {(h["document_id"], h["chunk_index"]) for h in top}

        inter = rel & got
        recall = len(inter) / len(rel) if rel else 0.0
        recall_sum += recall

        ok = bool(inter)
        doc_ok = bool({d for d, _ in rel} & {d for d, _ in got})
        hit += ok
        doc_hit += doc_ok

        if not ok:
            misses.append((item, top))

        if args.detail or not ok:
            flag = "OK  " if ok else ("DOC " if doc_ok else "MISS")
            print(
                f"[{n:>2}/{len(data)}] {flag} {item['id']:<6} recall={recall:.0%}  "
                f"标注={sorted(rel)} 命中={sorted(inter)}",
                flush=True,
            )
            if not ok and doc_ok:
                same = sorted(i for d, i in got if d in {x for x, _ in rel})
                print(f"          同文档召回={same}", flush=True)

    total = len(data)
    chunk_recall = recall_sum / total if total else 0.0

    print("\n" + "=" * 58)
    print(f"Recall@{K}（宏平均） = {chunk_recall:.3f}")
    print(f"HitRate            = {hit}/{total} = {hit/total:.1%}")
    print(f"DocHitRate         = {doc_hit}/{total} = {doc_hit/total:.1%}")
    if misses:
        print(f"完全未命中          = {len(misses)} 条 -> {[m[0]['id'] for m in misses]}")

    if args.judge and misses:
        print("\n" + "=" * 58)
        print("未命中题的可回答性判定（区分'标注问题'与'检索缺失'）")
        for item, top in misses[:8]:
            result = await judge(item["question"], top, llm)
            print(
                f"  {item['id']}: answerable={result.get('answerable')} "
                f"usable={result.get('usable_refs')} | {str(result.get('reason'))[:70]}",
                flush=True,
            )


asyncio.run(main())
