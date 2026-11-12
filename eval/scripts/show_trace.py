"""本地 trace 查看器：按 task 汇总节点耗时与 LLM token。

用法：
    python eval/scripts/show_trace.py --date 20261110
    python eval/scripts/show_trace.py --task <task_id>
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parents[1] / "results" / "traces"


def load(date: str | None, task: str | None) -> list[dict]:
    files = sorted(TRACE_DIR.glob(f"{date or '*'}.jsonl"))
    rows = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if task and row.get("task_id") != task:
                continue
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYYMMDD；不填则读全部")
    parser.add_argument("--task", default=None)
    args = parser.parse_args()

    rows = load(args.date, args.task)
    if not rows:
        print(f"没有 trace：{TRACE_DIR}")
        return 1

    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_task[row.get("task_id", "unknown")].append(row)

    for task_id, events in by_task.items():
        task_events = [e for e in events if e.get("event") == "task"]
        llm_events = [e for e in events if e.get("event") == "llm"]
        node_events = [e for e in events if e.get("event") == "node"]
        total_in = sum(int(e.get("input_tokens") or 0) for e in llm_events)
        total_out = sum(int(e.get("output_tokens") or 0) for e in llm_events)
        print("=" * 92)
        print(f"task {task_id}")
        if task_events:
            t = task_events[-1]
            print(f"  query: {t.get('query')} | status={t.get('status')} | {t.get('duration_ms')}ms")
        print(f"  llm: {len(llm_events)} calls | tokens in/out = {total_in}/{total_out}")

        if node_events:
            print("  NODE")
            for e in node_events:
                hits = (e.get("meta") or {}).get("hits")
                print(
                    f"    {e.get('node'):<16} {e.get('status'):<8} "
                    f"{str(e.get('duration_ms')):>8}ms hits={hits}"
                )
        if llm_events:
            print("  LLM")
            for e in llm_events:
                print(
                    f"    {e.get('kind'):<8} {e.get('model'):<18} "
                    f"{str(e.get('duration_ms')):>8}ms "
                    f"tok={e.get('input_tokens')}/{e.get('output_tokens')}"
                    f"{' ERROR=' + str(e.get('error')) if e.get('error') else ''}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
