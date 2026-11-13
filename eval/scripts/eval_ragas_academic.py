"""第 4 项：30 条学术集 RAGAS 评测（ContextRecall / Faithfulness）。

设计：
- 检索层：进程内跑生产 KB 检索（增强+多查询+兄弟扩展+精排），用章节级 parent 上下文
- 生成层：真实 API 跑完整 LangGraph，取最终报告 + 来源
- RAGAS：context_recall / faithfulness；逐条落盘，便于 A/B 与人工复核
- CNKI A/B：本脚本不切后端配置；用 --tag 区分两次运行
    baseline:  CNKI_ENABLED=false
    with_cnki: CNKI_ENABLED=true CNKI_FORCE=true

用法：
    .\\.venv\\Scripts\\python.exe eval\\scripts\\eval_ragas_academic.py --limit 3 --skip-generation
    .\\.venv\\Scripts\\python.exe eval\\scripts\\eval_ragas_academic.py --tag baseline
    .\\.venv\\Scripts\\python.exe eval\\scripts\\eval_ragas_academic.py --dataset eval\\dataset_scholarflow_cn.json --tag with_cnki
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time
import types
from pathlib import Path

import httpx

# ---- RAGAS 0.4.3 与 langchain-community 0.4 的兼容垫片 ----
# ragas.llms.base 仍 import langchain_community.chat_models.vertexai；
# 该模块在 langchain-community 0.4 已移除。DeepSeek 路径不会用到 VertexAI，
# 这里只补一个符号让 import 通过，不改变任何评测逻辑。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertex = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:  # pragma: no cover - compatibility only
        pass

    _vertex.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = _vertex

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = PROJECT_ROOT / "eval"
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
sys.path.insert(0, str(EVAL_DIR / "scripts"))

BASE = os.environ.get("SCHOLARFLOW_BASE", "http://127.0.0.1:8900")
SCHOLARFLOW_API_KEY = os.environ.get("SCHOLARFLOW_API_KEY", "")

def _auth_headers() -> dict:
    return {"X-API-Key": SCHOLARFLOW_API_KEY} if SCHOLARFLOW_API_KEY else {}
DEFAULT_DATASET = EVAL_DIR / "dataset_scholarflow.json"


def load_items(path: Path) -> list[dict]:
    items = json.loads(path.read_text(encoding="utf-8"))
    # 统一字段：reference / relevant_chunks
    for it in items:
        if "reference" not in it:
            it["reference"] = it.get("ground_truth", "")
    return items


def _reference_text(it: dict, child_map: dict[str, str]) -> str:
    texts = [child_map[k][:600] for k in it.get("relevant_chunks", []) if k in child_map]
    return "\n".join(texts).strip()


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(sum(values) / len(values), 4) if values else None


async def run_retrieval(
    items: list[dict], contexts: int = 12, parent_chars: int = 4000,
    include_child: bool = False,
) -> list[dict]:
    from eval_run import chunk_text_map, production_kb_candidates
    from app.agents.nodes import _pick_parent_diverse
    from app.llm.gateway import get_llm
    from app.rag.vector_store import get_vector_store

    store = get_vector_store()
    await store.ensure_collection()
    child_map, parent_map = await chunk_text_map()
    llm = get_llm()

    samples = []
    for i, it in enumerate(items, 1):
        q = it["question"]
        hits = await production_kb_candidates(store, q, llm)
        selected = _pick_parent_diverse(hits, contexts)
        ctxs = []
        for h in selected:
            key = f"{h['document_id']}#{h['chunk_index']}"
            child = (h.get("text") or "")[:800]
            parent = (parent_map.get(key) or "")[:parent_chars]
            body = parent
            if include_child and child and child not in parent:
                body = f"{parent}\n[child]\n{child}"
            ctxs.append(
                f"{h['document_name']} · 片段{h['chunk_index'] + 1}（章节上下文）\n{body}"
            )
        ref = _reference_text(it, child_map)
        samples.append(
            {
                "id": it["id"],
                "user_input": q,
                "reference": ref,
                "retrieved_contexts": ctxs,
                "doc_name": it.get("doc_name", ""),
                "type": it.get("type", ""),
                "unanswerable": bool(it.get("unanswerable")),
            }
        )
        print(f"[retrieval] [{i}/{len(items)}] {it['id']} hits={len(hits)}", flush=True)
    return samples


async def run_generation(
    items: list[dict], timeout: float, task_sleep: float = 0.0, max_retries: int = 2
) -> list[dict]:
    async with httpx.AsyncClient(
        base_url=BASE, timeout=httpx.Timeout(60.0, connect=10.0), headers=_auth_headers()
    ) as client:
        out = []
        for i, it in enumerate(items, 1):
            task_id = ""
            detail: dict = {}
            report = ""
            sources: list = []
            attempt = 0
            started = time.time()
            while True:
                resp = await client.post(
                    "/api/v1/research",
                    json={"query": it["question"], "lang": it.get("lang", "zh")},
                )
                resp.raise_for_status()
                task_id = resp.json()["task_id"]
                deadline = time.time() + timeout
                detail = {}
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    try:
                        r = await client.get(f"/api/v1/research/{task_id}")
                    except Exception:
                        continue
                    if r.status_code == 200:
                        detail = r.json()
                        if detail.get("status") in ("done", "failed", "cancelled"):
                            break
                report = (detail.get("report") or {}).get("markdown", "") or ""
                sources = detail.get("sources") or []
                degraded = bool(
                    (detail.get("report") or {}).get("degraded")
                    or (detail.get("stats") or {}).get("report_degraded")
                )
                bad = detail.get("status") != "done" or degraded or not report.strip()
                if bad and attempt < max_retries:
                    attempt += 1
                    print(
                        f"[generation] {it['id']} retry {attempt}/{max_retries} "
                        f"status={detail.get('status')} degraded={degraded}",
                        flush=True,
                    )
                    await asyncio.sleep(60)
                    continue
                break

            contexts = []
            for s in sources:
                meta = s.get("meta") or {}
                parent = str(meta.get("parent_text") or "")
                child = str(s.get("snippet") or "")
                # 报告写入时 KB 用的是章节级 parent；带上 child 便于 RAGAS 对齐引用句
                contexts.append((parent + "\n[child]\n" + child if parent else child)[:4000])
            out.append(
                {
                    "id": it["id"],
                    "user_input": it["question"],
                    "response": report,
                    "retrieved_contexts": contexts,
                    "reference": it.get("reference", ""),
                    "task_id": task_id,
                    "status": detail.get("status", "timeout"),
                    "degraded": bool(
                        (detail.get("report") or {}).get("degraded")
                        or (detail.get("stats") or {}).get("report_degraded")
                    ),
                    "elapsed_sec": round(time.time() - started, 1),
                    "report_chars": len(report),
                    "sources_count": len(sources),
                    "source_types": {
                        t: sum(1 for s in sources if s.get("type") == t)
                        for t in {s.get("type") for s in sources}
                    },
                }
            )
            print(
                f"[generation] [{i}/{len(items)}] {it['id']} {out[-1]['status']} "
                f"{out[-1]['elapsed_sec']}s chars={len(report)} sources={len(sources)} "
                f"degraded={out[-1]['degraded']}",
                flush=True,
            )
            if task_sleep > 0 and i < len(items):
                await asyncio.sleep(task_sleep)
        return out


def score(samples: list[dict], metrics: list, judge_model: str) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig
    from langchain_openai import ChatOpenAI
    from app.core.config import get_settings

    s = get_settings()
    chat = ChatOpenAI(
        model=judge_model,
        api_key=s.eval_judge_api_key or s.chat_api_key,
        base_url=s.eval_judge_base_url or s.chat_base_url,
        temperature=0.1,
        extra_body={"thinking": {"type": "disabled"}},
    )
    llm = LangchainLLMWrapper(chat)
    for m in metrics:
        m.llm = llm

    ds = EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=x["user_input"],
                retrieved_contexts=x["retrieved_contexts"],
                reference=x.get("reference") or "（无参考答案）",
                **({"response": x["response"]} if x.get("response") else {}),
            )
            for x in samples
        ]
    )
    # max_workers 默认 16：28 样本 × 2 指标会瞬时打出 56 个裁判请求，
    # DeepSeek 限流后大量样本变 NaN。降到 2 并发，牺牲几分钟换可复现。
    result = evaluate(
        ds,
        metrics=metrics,
        llm=llm,
        run_config=RunConfig(timeout=400, max_retries=3, max_workers=2),
    )
    df = result.to_pandas()
    by_question = {str(x["user_input"]): x for x in samples}
    rows = []
    for _, row in df.iterrows():
        sample = by_question.get(str(row.get("user_input", "")), {})
        item = {
            "id": sample.get("id", ""),
            "user_input": row.get("user_input", ""),
            "reference": row.get("reference", ""),
            "response": row.get("response", ""),
            "retrieved_contexts": list(row.get("retrieved_contexts", []) or []),
        }
        for m in metrics:
            key = m.name
            val = row.get(key)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                item[key] = None
            else:
                item[key] = round(float(val), 4)
        rows.append(item)

    agg = {}
    for m in metrics:
        vals = [r[m.name] for r in rows if isinstance(r.get(m.name), (int, float))]
        agg[m.name] = _mean(vals)
    return {"aggregate": agg, "items": rows}


def score_sequential(samples: list[dict], metrics: list, judge_model: str, delay: float = 2.0) -> dict:
    """逐条评分：商汤免费档 RPS/TPM 较低，整批并发会大量 429。

    每条之间 sleep，失败重试交给 RunConfig；这比一次 evaluate 28 条慢，
    但能保证每条都有值，不会用 3 条样本的均值冒充 28 条。
    """
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig
    from langchain_openai import ChatOpenAI
    from app.core.config import get_settings

    s = get_settings()
    chat = ChatOpenAI(
        model=judge_model,
        api_key=s.eval_judge_api_key or s.chat_api_key,
        base_url=s.eval_judge_base_url or s.chat_base_url,
        temperature=0.1,
        extra_body={"thinking": {"type": "disabled"}} if s.chat_provider == "deepseek" else None,
    )
    llm = LangchainLLMWrapper(chat)
    for m in metrics:
        m.llm = llm

    rows = []
    for idx, x in enumerate(samples, 1):
        sample_kwargs = {
            "user_input": x["user_input"],
            "retrieved_contexts": x["retrieved_contexts"],
            "reference": x.get("reference") or "（无参考答案）",
        }
        if x.get("response"):
            sample_kwargs["response"] = x["response"]
        ds = EvaluationDataset(samples=[SingleTurnSample(**sample_kwargs)])
        row_data = {
            "id": x.get("id", ""),
            "user_input": x["user_input"],
            "reference": x.get("reference", ""),
            "response": x.get("response", ""),
            "retrieved_contexts": x["retrieved_contexts"],
        }
        # RAGAS 会把裁判侧的 429 吞成 NaN，evaluate() 本身不抛异常。
        # 只靠 RunConfig 的重试不够，这里按条目再重试，直到拿到非空值或次数用尽。
        attempts = 4
        for attempt in range(1, attempts + 1):
            try:
                result = evaluate(
                    ds,
                    metrics=metrics,
                    llm=llm,
                    run_config=RunConfig(
                        timeout=400, max_retries=6, max_wait=90, max_workers=1
                    ),
                )
                row = result.to_pandas().iloc[0]
                for m in metrics:
                    val = row.get(m.name)
                    row_data[m.name] = (
                        None
                        if val is None or (isinstance(val, float) and math.isnan(val))
                        else round(float(val), 4)
                    )
                if any(row_data.get(m.name) is not None for m in metrics):
                    print(
                        f"[score] [{idx}/{len(samples)}] {x.get('id')} "
                        + " ".join(f"{m.name}={row_data.get(m.name)}" for m in metrics),
                        flush=True,
                    )
                    break
                raise RuntimeError("all_metrics_nan")
            except Exception as exc:  # noqa: BLE001
                if attempt >= attempts:
                    for m in metrics:
                        row_data[m.name] = None
                    print(
                        f"[score] [{idx}/{len(samples)}] {x.get('id')} FAILED "
                        f"{type(exc).__name__}: {str(exc)[:160]}",
                        flush=True,
                    )
                else:
                    wait = min(90, 10 * attempt)
                    print(
                        f"[score] [{idx}/{len(samples)}] {x.get('id')} retry "
                        f"{attempt}/{attempts - 1} after {wait}s: {str(exc)[:80]}",
                        flush=True,
                    )
                    time.sleep(wait)
        rows.append(row_data)
        if idx < len(samples):
            time.sleep(max(0.0, delay))

    agg = {}
    for m in metrics:
        vals = [r[m.name] for r in rows if isinstance(r.get(m.name), (int, float))]
        agg[m.name] = _mean(vals)
    return {"aggregate": agg, "items": rows}


def merge_generation(old_path: str, new_gen: dict) -> dict:
    """把补评的少量样本合并回已有 28 条结果，并按合并后的有效值重算聚合。"""
    old = json.loads(Path(old_path).read_text(encoding="utf-8"))
    old_gen = old.get("generation") or {}
    old_items = old_gen.get("items") or []
    order = [x.get("id") for x in old_items]
    by_id = {x.get("id"): x for x in old_items}
    for x in new_gen.get("items") or []:
        old_item = by_id.get(x.get("id"))
        if old_item is None:
            by_id[x.get("id")] = x
            continue
        # 补评只跑一个指标时，不要把另一个指标的已有值覆盖成 None
        for k, v in x.items():
            if k in ("context_recall", "faithfulness") and v is None:
                continue
            old_item[k] = v

    items = [by_id[i] for i in order if i in by_id]
    for x in new_gen.get("items") or []:
        if x.get("id") not in order:
            items.append(x)

    for metric in ("context_recall", "faithfulness"):
        vals = [
            x.get(metric) for x in items
            if isinstance(x.get(metric), (int, float))
        ]
        new_gen.setdefault("aggregate", {})[metric] = _mean(vals)

    old_meta = {m.get("id"): m for m in (old_gen.get("_gen_meta") or [])}
    for m in new_gen.get("_gen_meta") or []:
        old_meta[m.get("id")] = m
    new_gen["_gen_meta"] = [old_meta[i] for i in order if i in old_meta]
    new_gen["_gen_meta"] += [
        m for i, m in old_meta.items() if i not in order
    ]
    new_gen["items"] = items
    return new_gen


def render_md(tag: str, dataset: str, retr: dict | None, gen: dict | None) -> str:
    lines = [
        f"# RAGAS 学术集评测（{tag}）",
        "",
        f"> 数据集 {dataset} | 时间 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    if retr:
        a = retr["aggregate"]
        lines += [
            "## 检索层（生产 KB 路径，章节级 parent 上下文）",
            "",
            "| 指标 | 值 | 样本 |",
            "|---|---|---|",
            f"| context_recall | {a.get('context_recall')} | {len(retr['items'])} |",
            "",
            "| id | context_recall |",
            "|---|---|",
        ]
        for r in retr["items"]:
            lines.append(f"| {r['id']} | {r.get('context_recall')} |")
        lines.append("")
    if gen:
        a = gen["aggregate"]
        lines += [
            "## 生成层（真实 API 报告 + 来源）",
            "",
            "| 指标 | 值 | 样本 |",
            "|---|---|---|",
            f"| context_recall | {a.get('context_recall')} | {len(gen['items'])} |",
            f"| faithfulness | {a.get('faithfulness')} | {len(gen['items'])} |",
            "",
            "| id | context_recall | faithfulness | 报告字数 | 来源 | 耗时 |",
            "|---|---|---|---|---|---|",
        ]
        for r, g in zip(gen["items"], gen.get("_gen_meta", [])):
            lines.append(
                f"| {r['id']} | {r.get('context_recall')} | {r.get('faithfulness')} | "
                f"{g.get('report_chars')} | {g.get('sources_count')} | {g.get('elapsed_sec')}s |"
            )
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--tag", default="baseline")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="", help="逗号分隔的题目 id 子集")
    parser.add_argument("--contexts", type=int, default=12, help="检索上下文条数")
    parser.add_argument("--parent-chars", type=int, default=4000, help="parent 文本截断长度")
    parser.add_argument("--include-child", action="store_true", help="上下文中额外拼 child 文本")
    parser.add_argument("--sequential", action="store_true", help="逐条评分，适配商汤低 RPS 免费档")
    parser.add_argument("--sleep", type=float, default=2.0, help="逐条评分间隔秒数")
    parser.add_argument("--merge-into", default="", help="把本次补评结果合并回已有 RAGAS JSON")
    parser.add_argument("--faithfulness-only", action="store_true", help="补评时只算 faithfulness")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--task-sleep", type=float, default=0.0, help="生成任务之间的间隔秒数")
    parser.add_argument("--max-retries", type=int, default=2, help="报告降级/失败时的重跑次数")
    parser.add_argument("--skip-retrieval", action="store_true")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument(
        "--reuse-generation",
        default="",
        help="复用已有 RAGAS JSON 的 generation.items，只重新评分（避免重跑 30 个任务）",
    )
    args = parser.parse_args()

    dataset = Path(args.dataset)
    items = load_items(dataset)
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [x for x in items if x["id"] in wanted]
    if args.limit > 0:
        items = items[: args.limit]

    from app.core.config import get_settings

    s = get_settings()
    judge_model = s.eval_judge_model or s.chat_model
    print(f"[ragas] dataset={dataset.name} items={len(items)} tag={args.tag} judge={judge_model}", flush=True)

    retr = None
    gen = None
    if not args.skip_retrieval:
        from ragas.metrics import context_recall

        # 2 条 unanswerable 没有 relevant_chunks，不能算 ContextRecall；
        # 保留在 30 条集中用于“该拒答是否拒答”的人工检查。
        retrieval_items = [it for it in items if not it.get("unanswerable")]
        samples = await run_retrieval(
            retrieval_items,
            contexts=args.contexts,
            parent_chars=args.parent_chars,
            include_child=args.include_child,
        )
        if args.sequential:
            retr = score_sequential(
                samples, [context_recall], judge_model, delay=args.sleep
            )
        else:
            retr = score(samples, [context_recall], judge_model)
        print(f"[ragas] retrieval context_recall={retr['aggregate']} n={len(samples)}", flush=True)

    if not args.skip_generation:
        from ragas.metrics import context_recall, faithfulness

        if args.reuse_generation:
            # 复用已经跑完的报告，只重新评分：避免 30 个任务重跑 20 分钟
            old = json.loads(Path(args.reuse_generation).read_text(encoding="utf-8"))
            gen_old = old.get("generation") or {}
            scorable = gen_old.get("items") or []
            gen_meta = gen_old.get("_gen_meta") or []
            if args.ids:
                wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
                scorable = [x for x in scorable if x.get("id") in wanted]
                gen_meta = [x for x in gen_meta if x.get("id") in wanted]
            if not scorable:
                print(f"[ragas] {args.reuse_generation} 没有 generation.items", flush=True)
                return 2
            metrics = [faithfulness] if args.faithfulness_only else [context_recall, faithfulness]
            if args.sequential:
                gen = score_sequential(
                    scorable, metrics, judge_model, delay=args.sleep
                )
            else:
                gen = score(scorable, metrics, judge_model)
            gen["_gen_meta"] = gen_meta
            if args.merge_into:
                gen = merge_generation(args.merge_into, gen)
            print(f"[ragas] reused generation metrics={gen['aggregate']}", flush=True)
        else:
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as c:
                    h = (await c.get(f"{BASE}/health")).json()
                print(f"[ragas] backend {h}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[ragas] 后端不可用：{exc}", flush=True)
                return 2
            samples = await run_generation(
                items, args.timeout, args.task_sleep, args.max_retries
            )
            # 只对有报告、非 unanswerable 的样本算 faithfulness
            unanswerable_ids = {it["id"] for it in items if it.get("unanswerable")}
            scorable = [
                x for x in samples
                if x.get("response") and x.get("status") == "done"
                and x.get("id") not in unanswerable_ids
                # 摘录回退（LLM 限流/失败时的兜底）不是模型生成的报告，
                # 计入 Faithfulness 会把"限流"误算成"报告质量"。
                and not (x.get("response") or "").startswith("基于本地知识库的要点")
            ]
            if scorable:
                # 生成层同时算 ContextRecall：CNKI A/B 看的是“报告实际用到的来源”是否覆盖参考答案
                if args.sequential:
                    gen = score_sequential(
                        scorable, [context_recall, faithfulness], judge_model, delay=args.sleep
                    )
                else:
                    gen = score(scorable, [context_recall, faithfulness], judge_model)
                gen["_gen_meta"] = scorable
                print(f"[ragas] generation metrics={gen['aggregate']}", flush=True)

    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "tag": args.tag,
            "dataset": dataset.name,
            "items": len(items),
            "judge_model": judge_model,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        },
        "retrieval": retr,
        "generation": gen,
    }
    json_path = out_dir / f"ragas_{args.tag}_{ts}.json"
    md_path = out_dir / f"ragas_{args.tag}_{ts}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(args.tag, dataset.name, retr, gen), encoding="utf-8")
    print(f"[ragas] saved {json_path}", flush=True)
    print(f"[ragas] saved {md_path}", flush=True)
    print("RAGAS_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
