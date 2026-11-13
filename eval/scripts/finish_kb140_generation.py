r"""补齐 140 篇知识库的生成层评测。

背景：2026-11-12 00:00–00:57 的 30 条生成任务在写结果前被中断，25 条报告已落库
（research_tasks / reports / sources）。本脚本直接复用这些任务，只重跑缺失题目，
再统一做 RAGAS 评分，避免重跑 50 分钟。

用法：
    cd apps/api
    ..\..\.venv\Scripts\python.exe ..\..\eval\scripts\finish_kb140_generation.py `
        --retrieval ..\..\eval\results\ragas_kb140_retrieval_merged_20261111_2358.json `
        --tag kb140_generation

DB 里没有的题（sf26/sf27/sf28/sf29/sf30）会调用本地 8900 API 重跑。
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "eval" / "scripts"))

import eval_ragas_academic as era  # noqa: E402

DB_PATH = ROOT / "apps" / "api" / "kra.db"
DEFAULT_DATASET = ROOT / "eval" / "dataset_scholarflow.json"
RESULTS = ROOT / "eval" / "results"


def load_from_db(items: list[dict], since: str) -> dict[str, dict]:
    """按题目文本匹配窗口内已完成的真实任务，重建 generation sample。"""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT t.id, t.query, t.status, t.duration_sec,
               (SELECT markdown FROM reports r WHERE r.task_id = t.id
                ORDER BY r.id DESC LIMIT 1) AS report
        FROM research_tasks t
        WHERE t.status = 'done' AND t.created_at >= ?
        ORDER BY t.created_at
        """,
        (since,),
    ).fetchall()

    by_question: dict[str, sqlite3.Row] = {}
    for r in rows:
        if (r["report"] or "").strip():
            by_question[r["query"]] = r  # 后写覆盖先写，保留最近一次

    out: dict[str, dict] = {}
    for it in items:
        r = by_question.get(it["question"])
        if r is None:
            continue
        ctxs, types = [], {}
        for s in con.execute(
            "SELECT type, snippet, meta_json FROM sources WHERE task_id = ? ORDER BY ref_no",
            (r["id"],),
        ):
            meta = json.loads(s["meta_json"] or "{}")
            parent = str(meta.get("parent_text") or "")
            child = str(s["snippet"] or "")
            ctxs.append((parent + "\n[child]\n" + child if parent else child)[:4000])
            types[s["type"]] = types.get(s["type"], 0) + 1
        out[it["id"]] = {
            "id": it["id"],
            "user_input": it["question"],
            "response": r["report"],
            "retrieved_contexts": ctxs,
            "reference": it.get("reference", ""),
            "task_id": r["id"],
            "status": r["status"],
            "elapsed_sec": r["duration_sec"],
            "report_chars": len(r["report"] or ""),
            "sources_count": len(ctxs),
            "source_types": types,
        }
    con.close()
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--retrieval", default="", help="检索层已评分的 JSON，原样并入结果")
    parser.add_argument("--tag", default="kb140_generation")
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-score", action="store_true")
    parser.add_argument(
        "--force-ids",
        default="",
        help="即使 DB 有报告也重跑（用于 429 摘录回退的补跑），逗号分隔",
    )
    parser.add_argument("--task-sleep", type=float, default=0.0, help="重跑任务之间的间隔秒数")
    parser.add_argument("--score-ids", default="", help="只评分这些 id（不重跑任务），逗号分隔")
    parser.add_argument("--merge-into", default="", help="把本次结果合并回已有 RAGAS JSON")
    parser.add_argument(
        "--since",
        default="2026-11-11 15:55",
        help="只复用该 UTC 时间之后的任务（默认取 140 篇知识库那轮生成）",
    )
    args = parser.parse_args()

    from app.core.config import get_settings

    items = era.load_items(Path(args.dataset))
    from_db = load_from_db(items, args.since)
    forced = {x.strip() for x in args.force_ids.split(",") if x.strip()}
    if forced:
        from_db = {k: v for k, v in from_db.items() if k not in forced}
    missing = [
        it for it in items if it["id"] in forced or it["id"] not in from_db
    ]
    print(
        f"[resume] db={len(from_db)} missing={[x['id'] for x in missing]}",
        flush=True,
    )

    samples = [from_db[it["id"]] for it in items if it["id"] in from_db]
    for n, it in enumerate(missing):
        samples += await era.run_generation([it], args.timeout)
        if n < len(missing) - 1:
            await asyncio.sleep(max(0.0, args.task_sleep))
    order = {it["id"]: n for n, it in enumerate(items)}
    samples.sort(key=lambda x: order.get(x["id"], 999))

    unanswerable = {it["id"] for it in items if it.get("unanswerable")}
    scorable = [
        x
        for x in samples
        if x.get("response")
        and x.get("status") == "done"
        and x["id"] not in unanswerable
        and not (x.get("response") or "").startswith("基于本地知识库的要点")
    ]
    print(f"[resume] samples={len(samples)} scorable={len(scorable)}", flush=True)
    # 补跑模式（--force-ids）只重评重跑的题；其余样本沿用 --merge-into 里的旧分数。
    score_ids = {x.strip() for x in args.score_ids.split(",") if x.strip()}
    if score_ids:
        score_pool = [x for x in scorable if x["id"] in score_ids]
    elif forced:
        score_pool = [x for x in scorable if x["id"] in forced]
    else:
        score_pool = scorable
    print(f"[resume] score_pool={len(score_pool)}", flush=True)

    gen = None
    if not args.skip_score:
        from ragas.metrics import context_recall, faithfulness

        judge_model = get_settings().eval_judge_model or get_settings().chat_model
        print(f"[resume] judge={judge_model}", flush=True)
        gen = era.score_sequential(
            score_pool, [context_recall, faithfulness], judge_model, delay=args.sleep
        )
        gen["_gen_meta"] = score_pool
        if args.merge_into:
            gen = era.merge_generation(args.merge_into, gen)
        print(f"[resume] metrics={gen['aggregate']}", flush=True)

    retr = None
    if args.retrieval:
        retr = json.loads(Path(args.retrieval).read_text(encoding="utf-8")).get("retrieval")

    ts = time.strftime("%Y%m%d_%H%M")
    payload = {
        "meta": {
            "tag": args.tag,
            "dataset": Path(args.dataset).name,
            "items": len(items),
            "judge_model": get_settings().eval_judge_model,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "reused_from_db": len(from_db),
            "rerun_ids": [x["id"] for x in missing],
            "retrieval_source": Path(args.retrieval).name if args.retrieval else None,
        },
        "retrieval": retr,
        "generation": gen,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS / f"ragas_{args.tag}_{ts}.json"
    md_path = RESULTS / f"ragas_{args.tag}_{ts}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        era.render_md(args.tag, Path(args.dataset).name, retr, gen), encoding="utf-8"
    )
    print(f"[resume] saved {json_path}", flush=True)
    print(f"[resume] saved {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
