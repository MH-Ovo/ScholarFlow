"""把本地 trace JSONL 上报到 Langfuse。

前置环境变量：
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_HOST=https://cloud.langfuse.com   # 自托管填自己的地址

用法：
    python eval/scripts/export_langfuse.py --date 20261110
    python eval/scripts/export_langfuse.py --task <task_id>
"""
import argparse
import json
import os
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parents[1] / "results" / "traces"


def load(date: str | None, task: str | None) -> list[dict]:
    rows = []
    for path in sorted(TRACE_DIR.glob(f"{date or '*'}.jsonl")):
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
    parser.add_argument("--date", default=None)
    parser.add_argument("--task", default=None)
    args = parser.parse_args()

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    # 兼容 Langfuse 官方常用的 LANGFUSE_HOST 与用户配置里的 LANGFUSE_BASE_URL
    host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    ).strip()
    if not public_key or not secret_key:
        print("缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，无法上报。")
        print("本地 trace 仍可用：python eval/scripts/show_trace.py --date <YYYYMMDD>")
        return 2

    from langfuse import Langfuse

    rows = load(args.date, args.task)
    if not rows:
        print(f"没有 trace：{TRACE_DIR}")
        return 1

    lf = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    count = 0
    for row in rows:
        task_id = str(row.get("task_id") or "unknown")
        trace_id = lf.create_trace_id(seed=task_id)
        event = row.get("event")
        name = f"{event}.{row.get('node') or row.get('kind') or 'event'}"
        metadata = {
            k: v
            for k, v in row.items()
            if k not in ("ts", "task_id", "event", "node", "kind")
        }
        level = "ERROR" if row.get("status") == "failed" or row.get("error") else "DEFAULT"
        lf.create_event(
            trace_context={"trace_id": trace_id},
            name=name,
            metadata=metadata,
            level=level,
        )
        count += 1
    lf.flush()
    print(f"已上报 {count} 条事件到 Langfuse：{host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
