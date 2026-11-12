"""单源故障注入 + 降级成功率评测。

不启动后端、不跑 LLM，直接测适配器：
- 对 arxiv / web / cnki 分别注入 timeout / rate_limit / auth / error / empty；
- 检查是否返回结构化 SourceResult（而不是抛异常/返回空列表）；
- 用两个健康 stub 模拟其他源，检查“一个源故障时其余源仍能产出”的降级成功率。

输出：eval/results/source_resilience_<时间>.json / .md
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "api"))

from app.core.config import get_settings
from app.integrations.adapters import get_adapter
from app.integrations.source_result import SourceResult, SourceStatus

OUT_DIR = Path(__file__).resolve().parents[1] / "results"

MODES = {
    "timeout": SourceStatus.TIMEOUT,
    "rate_limit": SourceStatus.RATE_LIMITED,
    "auth": SourceStatus.AUTH_FAILED,
    "error": SourceStatus.ERROR,
    "empty": SourceStatus.NO_HITS,
}


class _StubAdapter:
    def __init__(self, name: str):
        self.name = name

    async def search(self, query: str, max_results: int = 5) -> SourceResult:
        return SourceResult(self.name, SourceStatus.OK, items=[{"title": f"{self.name}-stub"}])


async def run_case(source: str, mode: str, expected: SourceStatus, reps: int) -> dict:
    settings = get_settings()
    settings.fault_inject_source = source
    settings.fault_inject_mode = mode
    settings.fault_inject_rate = 1.0

    adapter = get_adapter(source)
    other = [_StubAdapter("stub-a"), _StubAdapter("stub-b")]
    structured = 0
    degraded_task = 0
    latencies: list[int] = []
    last_status = ""

    for _ in range(reps):
        started = time.perf_counter()
        try:
            bad = await adapter.search("RAG retrieval", max_results=3)
            latencies.append(int((time.perf_counter() - started) * 1000))
            last_status = bad.status.value
            if bad.status == expected:
                structured += 1
            # 一个源故障时，其他源仍能产出；不把故障源的 items 计入
            healthy_items = []
            for a in other:
                healthy_items.extend((await a.search("RAG retrieval")).items)
            if bad.status != SourceStatus.OK and len(healthy_items) >= 2:
                degraded_task += 1
        except Exception as exc:  # noqa: BLE001
            last_status = f"exception:{type(exc).__name__}"
            print(f"[resilience] {source}/{mode} exception: {exc}", flush=True)

    return {
        "source": source,
        "mode": mode,
        "expected": expected.value,
        "reps": reps,
        "structured_errors": structured,
        "degraded_task_success": degraded_task,
        "structured_rate": structured / reps,
        "degradation_rate": degraded_task / reps,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "last_status": last_status,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=10)
    args = parser.parse_args()

    rows = []
    for source in ("arxiv", "web", "cnki"):
        for mode, expected in MODES.items():
            row = await run_case(source, mode, expected, max(1, args.reps))
            rows.append(row)
            print(
                f"[resilience] {source}/{mode} status={row['last_status']} "
                f"structured={row['structured_rate']:.2f} degraded={row['degradation_rate']:.2f}",
                flush=True,
            )

    settings = get_settings()
    settings.fault_inject_source = ""

    ts = time.strftime("%Y%m%d_%H%M")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"ts": ts, "reps": args.reps, "method": "adapter_fault_injection"}, "items": rows}
    (OUT_DIR / f"source_resilience_{ts}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 单源故障注入与降级成功率",
        "",
        f"> 时间 {ts} | 每种 source×mode 重复 {args.reps} 次 | 适配器级",
        "",
        "| 源 | 故障模式 | 期望状态 | 结构化错误率 | 降级成功率 | 平均耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['source']} | {r['mode']} | {r['expected']} | "
            f"{r['structured_rate']:.0%} | {r['degradation_rate']:.0%} | {r['avg_latency_ms']}ms |"
        )
    lines += [
        "",
        "> 降级成功率口径：注入一个源故障后，调用方仍能用另外两个健康源产出 ≥2 条结果，"
        "且故障源返回结构化非 OK 状态、不抛异常。",
    ]
    (OUT_DIR / f"source_resilience_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[resilience] saved {OUT_DIR}/source_resilience_{ts}.json/.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
