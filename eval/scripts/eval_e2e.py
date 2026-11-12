"""端到端评测：跑完整 LangGraph 流程，评报告质量（L3）。

流程：POST /research → 轮询任务 → 取报告/来源 → LLM 评分 → Markdown 报告 + 人工评分表。

用法：
    python eval/scripts/eval_e2e.py --limit 2 --no-judge   # 冒烟（不调评分模型）
    python eval/scripts/eval_e2e.py                        # 全量 20 条 + LLM 评分
    python eval/scripts/eval_e2e.py --timeout 420           # 单任务超时 7 分钟

指标口径：
    correctness   0–5  与参考答案的关键事实是否一致
    completeness  0–5  是否覆盖参考答案的关键点
    groundedness  0–1  报告论断能否在来源片段中找到依据
    citation      机械统计：[n] 是否都指向真实来源
    pass          合格 = correctness ≥ 3 且 groundedness ≥ 0.8 且无无效引用
"""
import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

EVAL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = EVAL_DIR / "dataset_e2e.json"
BASE = "http://127.0.0.1:8900"
CITE_RE = re.compile(r"\[(\d+)\]")

JUDGE_PROMPT = """你是学术调研报告的严格评审。请根据问题、参考答案、报告和来源片段评分。
只依据给定材料判断，不要用你自己的知识补充。

【问题】
{q}

【参考答案】
{ref}

【报告】
{report}

【来源片段】
{sources}

评分规则（0–5）：
- correctness：报告是否回答了问题，关键事实是否与参考答案一致。5=完全正确；3=部分正确或有明显遗漏；0=错误或答非所问。
- completeness：是否覆盖参考答案的关键点。5=完整；3=覆盖主要点；0=缺失关键内容。
- groundedness：报告中的事实性论断是否都能在来源片段中找到依据。1=全部有依据；0=大量编造。
  注意：参考答案只用于判断对错，不能当作"来源依据"；论断必须能在【来源片段】里找到。
- citation_support：[n] 引用是否指向支持该论断的来源。1=全部支持；0=大量错引。
  必须逐条核对：只要存在一个引用编号与所支持论断不匹配，citation_support 最高 0.5；
  不要用"整体论断有来源支持"来豁免单条错引。

输出纯 JSON（不要代码块、不要解释）：
{{"correctness": 0, "completeness": 0, "groundedness": 0.0, "citation_support": 0.0, "unsupported_claims": [], "reason": "一句话说明主要扣分点"}}"""


def _extract_json(text: str) -> dict | None:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = match.group(1) if match else text
    for cand in (candidate.strip(),):
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _source_block(sources: list[dict], limit: int = 12) -> str:
    lines = []
    for s in sources[:limit]:
        lines.append(
            f"[{s.get('ref_no', '?')}] ({s.get('type', '')}) {str(s.get('title', ''))[:120]}\n"
            f"{str(s.get('snippet', ''))[:500]}"
        )
    return "\n\n".join(lines) or "（无来源）"


async def _judge(chat, item: dict, report: str, sources: list[dict]) -> dict:
    prompt = JUDGE_PROMPT.format(
        q=item["question"],
        ref=item.get("reference") or "（无参考答案）",
        report=report[:7000] or "（报告为空）",
        sources=_source_block(sources),
    )
    try:
        resp = await chat.ainvoke(prompt)
        data = _extract_json(str(resp.content))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not data:
        return {"error": "judge_json_parse_failed"}
    try:
        return {
            "correctness": float(data.get("correctness", 0)),
            "completeness": float(data.get("completeness", 0)),
            "groundedness": float(data.get("groundedness", 0)),
            "citation_support": float(data.get("citation_support", 0)),
            "unsupported_claims": list(data.get("unsupported_claims") or [])[:5],
            "reason": str(data.get("reason") or "")[:300],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"judge_bad_payload: {type(exc).__name__}: {str(exc)[:100]}"}


async def _run_one(client: httpx.AsyncClient, item: dict, timeout: float) -> dict:
    started = time.time()
    task_id = ""
    detail: dict | None = None
    timed_out = False
    try:
        resp = await client.post(
            "/api/v1/research",
            json={"query": item["question"], "lang": item.get("lang", "zh")},
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                r = await client.get(f"/api/v1/research/{task_id}")
            except Exception:
                # 长任务期间后端事件循环可能短暂阻塞（已知 B2），GET 超时继续轮询
                continue
            if r.status_code != 200:
                continue
            detail = r.json()
            if detail.get("status") in ("done", "failed"):
                break
        else:
            timed_out = True
            try:
                detail = (await client.get(f"/api/v1/research/{task_id}")).json()
            except Exception:
                detail = None
    except Exception as exc:  # noqa: BLE001
        return {
            "id": item["id"], "question": item["question"], "type": item.get("type", ""),
            "source": item.get("source", ""), "reference": item.get("reference", ""),
            "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "timed_out": False, "elapsed_sec": round(time.time() - started, 1),
            "task_id": task_id, "report": "", "sources": [], "stats": {},
            "report_chars": 0, "citation_refs": [], "invalid_refs": [],
            "failed_steps": [], "source_types": {},
        }

    detail = detail or {}
    report = (detail.get("report") or {}).get("markdown", "") or ""
    sources = detail.get("sources") or []
    stats = detail.get("stats") or {}
    refs = sorted({int(n) for n in CITE_RE.findall(report)})
    source_refs = {int(s.get("ref_no", 0)) for s in sources}
    invalid_refs = [n for n in refs if n not in source_refs]
    failed_steps = [
        s.get("step_key") for s in (detail.get("steps") or [])
        if s.get("status") == "failed"
    ]
    source_types = Counter(s.get("type", "") for s in sources)

    return {
        "id": item["id"],
        "question": item["question"],
        "type": item.get("type", ""),
        "source": item.get("source", ""),
        "reference": item.get("reference", ""),
        "task_id": task_id,
        "status": detail.get("status", "timeout" if timed_out else "unknown"),
        "timed_out": timed_out,
        "elapsed_sec": round(time.time() - started, 1),
        "duration_sec": stats.get("duration_sec"),
        "report": report,
        "report_chars": len(report),
        "sources": [
            {
                "ref_no": s.get("ref_no"), "type": s.get("type"),
                "title": s.get("title"), "snippet": s.get("snippet"),
                "source_label": s.get("source_label"),
                "meta": s.get("meta") or {},
            }
            for s in sources
        ],
        "source_types": dict(source_types),
        "stats": stats,
        "citation_refs": refs,
        "invalid_refs": invalid_refs,
        "failed_steps": failed_steps,
        "error": str(stats.get("error") or ""),
    }


def _avg(results: list[dict], key: str, sub: str = "judge") -> float | None:
    vals = []
    for r in results:
        v = (r.get(sub) or {}).get(key)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return round(sum(vals) / len(vals), 3) if vals else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(len(values) * 0.95)) - 1)
    return round(values[max(0, idx)], 1)


def _is_pass(r: dict) -> bool:
    j = r.get("judge") or {}
    return (
        r.get("status") == "done"
        and r.get("report_chars", 0) > 0
        and isinstance(j.get("correctness"), (int, float))
        and j["correctness"] >= 3
        and isinstance(j.get("groundedness"), (int, float))
        and j["groundedness"] >= 0.8
        and not r.get("invalid_refs")
    )


def _summary(results: list[dict]) -> dict:
    done = [r for r in results if r.get("status") == "done"]
    durations = [float(r["elapsed_sec"]) for r in done]
    return {
        "total": len(results),
        "done": len(done),
        "failed": len(results) - len(done),
        "pass": sum(1 for r in results if _is_pass(r)),
        "avg_correctness": _avg(results, "correctness"),
        "avg_completeness": _avg(results, "completeness"),
        "avg_groundedness": _avg(results, "groundedness"),
        "avg_citation_support": _avg(results, "citation_support"),
        "avg_duration_sec": round(sum(durations) / len(durations), 1) if durations else None,
        "p95_duration_sec": _p95(durations),
        "avg_sources": round(
            sum(len(r.get("sources") or []) for r in done) / len(done), 1
        ) if done else None,
        "items_with_invalid_refs": sum(1 for r in results if r.get("invalid_refs")),
        "items_with_cnki": sum(1 for r in results if (r.get("source_types") or {}).get("cnki")),
        "items_with_web": sum(1 for r in results if (r.get("source_types") or {}).get("web")),
        "items_with_paper": sum(1 for r in results if (r.get("source_types") or {}).get("paper")),
    }


def _md(results: list[dict], summary: dict, meta: dict) -> str:
    lines = [
        "# 端到端评测报告",
        "",
        f"> 时间 {meta['ts']} | 数据集 {meta['dataset']} | 共 {summary['total']} 条 | "
        f"评分模型 {meta['judge_model']}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 完成 / 总数 | {summary['done']} / {summary['total']} |",
        f"| 失败 / 超时 | {summary['failed']} |",
        f"| 合格条数（correctness≥3 且 groundedness≥0.8 且无无效引用） | {summary['pass']} |",
        f"| correctness 均值（0–5） | {summary['avg_correctness']} |",
        f"| completeness 均值（0–5） | {summary['avg_completeness']} |",
        f"| groundedness 均值（0–1） | {summary['avg_groundedness']} |",
        f"| citation_support 均值（0–1） | {summary['avg_citation_support']} |",
        f"| 平均耗时 / P95 | {summary['avg_duration_sec']}s / {summary['p95_duration_sec']}s |",
        f"| 平均来源数 | {summary['avg_sources']} |",
        f"| 含无效引用的条数 | {summary['items_with_invalid_refs']} |",
        f"| 命中知网 / 网页 / 论文的任务数 | {summary['items_with_cnki']} / "
        f"{summary['items_with_web']} / {summary['items_with_paper']} |",
        "",
        "## 分题型",
        "",
        "| 题型 | 条数 | correctness | groundedness | 合格 |",
        "|---|---|---|---|---|",
    ]
    for t in sorted({r.get("type", "") for r in results}):
        subset = [r for r in results if r.get("type") == t]
        lines.append(
            f"| {t} | {len(subset)} | {_avg(subset, 'correctness')} | "
            f"{_avg(subset, 'groundedness')} | {sum(1 for r in subset if _is_pass(r))} |"
        )

    lines += [
        "",
        "## 逐条结果",
        "",
        "| # | id | 题型 | 状态 | 正确 | 完整 | 有据 | 无效引用 | 来源 | 耗时 | 主要扣分点 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        j = r.get("judge") or {}
        reason = str(j.get("reason") or r.get("error") or "")[:80].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {i} | {r['id']} | {r.get('type','')} | {r.get('status','')} | "
            f"{j.get('correctness','-')} | {j.get('completeness','-')} | {j.get('groundedness','-')} | "
            f"{len(r.get('invalid_refs') or [])} | {len(r.get('sources') or [])} | "
            f"{r.get('elapsed_sec','-')}s | {reason} |"
        )

    def _needs_review(r: dict) -> bool:
        j = r.get("judge") or {}
        c, g = j.get("correctness"), j.get("groundedness")
        return (
            r.get("status") != "done"
            or (isinstance(c, (int, float)) and c < 3)
            or (isinstance(g, (int, float)) and g < 0.8)
            or bool(r.get("invalid_refs"))
            or r.get("report_chars", 0) < 500
        )

    low = [r for r in results if _needs_review(r)]
    lines += ["", "## 需要人工复核", ""]
    if low:
        for r in low:
            j = r.get("judge") or {}
            why = j.get("reason") or r.get("error") or "报告过短/无来源"
            lines.append(f"- **{r['id']}**（{r.get('type','')}）：{why}")
    else:
        lines.append("- 无")

    lines += [
        "",
        "## 人工评分表",
        "",
        "| id | 问题 | 自动 correctness | 人工 correctness (1–5) | 人工备注 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        q = r["question"].replace("|", "/").replace("\n", " ")[:60]
        auto = (r.get("judge") or {}).get("correctness", "-")
        lines.append(f"| {r['id']} | {q} | {auto} |  |  |")

    lines += [
        "",
        "> 完整报告正文见同目录 JSON；人工评分后建议单独记录，不与自动分混算。",
        "",
    ]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--ids", default="", help="逗号分隔的题目 id 子集（定向回归）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟）")
    parser.add_argument("--timeout", type=float, default=300.0, help="单任务超时（秒）")
    parser.add_argument("--no-judge", action="store_true", help="只跑流程，不调 LLM 评分")
    args = parser.parse_args()

    items = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [x for x in items if x["id"] in wanted]
    if args.limit > 0:
        items = items[: args.limit]

    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            h = (await c.get(f"{BASE}/health")).json()
        print(f"[e2e] 后端 OK: {h}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[e2e] 后端不可用（{BASE}）：{type(exc).__name__}: {exc}", flush=True)
        print("[e2e] 请先启动：cd apps/api && ..\\..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8900", flush=True)
        return 2

    from app.core.config import get_settings

    settings = get_settings()
    judge_model = settings.eval_judge_model or settings.chat_model

    chat = None
    if not args.no_judge:
        from langchain_openai import ChatOpenAI

        chat = ChatOpenAI(
            model=judge_model,
            api_key=settings.eval_judge_api_key or settings.chat_api_key,
            base_url=settings.eval_judge_base_url or settings.chat_base_url,
            temperature=0.1,
            max_tokens=1200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        print(f"[e2e] 评分模型: {judge_model}", flush=True)

    ts = time.strftime("%Y%m%d_%H%M")
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"e2e_{ts}.json"
    md_path = out_dir / f"e2e_{ts}.md"
    payload = {
        "meta": {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "dataset": Path(args.dataset).name,
            "judge_model": None if args.no_judge else judge_model,
            "backend": BASE,
            "timeout_sec": args.timeout,
        },
        "items": [],
    }

    async with httpx.AsyncClient(
        base_url=BASE, timeout=httpx.Timeout(60.0, connect=10.0)
    ) as client:
        for i, item in enumerate(items, 1):
            print(f"[e2e] [{i}/{len(items)}] {item['id']} 开始…", flush=True)
            result = await _run_one(client, item, args.timeout)
            if chat is not None and result.get("report"):
                result["judge"] = await _judge(chat, item, result["report"], result["sources"])
            payload["items"].append(result)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            j = result.get("judge") or {}
            print(
                f"[e2e] [{i}/{len(items)}] {item['id']} {result['status']} "
                f"{result['elapsed_sec']}s report={result['report_chars']}字 "
                f"sources={len(result['sources'])} "
                f"judge={j.get('correctness', '-')}/{j.get('groundedness', '-')}",
                flush=True,
            )

    summary = _summary(payload["items"])
    md = _md(payload["items"], summary, payload["meta"])
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "e2e_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "e2e_latest.md").write_text(md, encoding="utf-8")

    print("\n[e2e] 汇总:", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[e2e] 结果: {json_path}", flush=True)
    print(f"[e2e] 报告: {md_path}", flush=True)
    print("E2E_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
